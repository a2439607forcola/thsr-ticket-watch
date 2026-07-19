"""排程迴圈：在單一長跑的 GitHub Actions job 內執行五層分級監測。

GitHub 的 cron 排程是盡力而為，高頻排程實測會被大量跳過（設計每 10 分鐘、
實際約每小時一輪，且遲到 40~50 分鐘）。因此改成：cron 只負責「把 job 叫起來」
（每 30 分排一次、靠 concurrency group 串成近乎連續的接力），分鐘級的節奏
由本腳本在 job 內自己看錶決定，cron 遲到只影響接力邊界、不影響取樣精度。

五層頻率（台灣時間，還原自 2026-06/27~07/15 本機排程器的實測節奏）：
- today    當日班次：00 時與 06~23 時每 10 分；01~04 時每 20 分
- dawn     當日班次凌晨加密：04:55~05:50 每 1 分鐘（抓解凍確切時刻）
- hot      未來 27 天的週五+週日：05~23 時每 10 分（尾數 7）
- week     D+1~D+7 全星期：每小時 :15，並重建 report
- mid      D+8~D+14：每日 10:40 / 14:40 / 22:40
- far      D+15~D+27：每日 11:50 / 23:50

用法：
  python run_loop.py --max-minutes 58   # Actions 上的正常模式
  python run_loop.py --dry-run          # 只印出會執行什麼，不真的抓、不碰 git
  python run_loop.py --selftest         # 模擬走完一整天，驗證各層觸發次數
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ_TAIPEI = ZoneInfo("Asia/Taipei")

TASK_COMMANDS = {
    "today": [sys.executable, "fetch_snapshot.py", "--today"],
    "dawn":  [sys.executable, "fetch_snapshot.py", "--today"],
    "hot":   [sys.executable, "fetch_snapshot.py", "--future", "--weekdays", "4,6"],
    "week":  [sys.executable, "fetch_snapshot.py", "--future", "--days-from", "1", "--days-to", "7"],
    "mid":   [sys.executable, "fetch_snapshot.py", "--future", "--days-from", "8", "--days-to", "14"],
    "far":   [sys.executable, "fetch_snapshot.py", "--future", "--days-from", "15", "--days-to", "27"],
}

GIT_IDENTITY = ["-c", "user.name=thsr-watch-bot",
                "-c", "user.email=github-actions[bot]@users.noreply.github.com"]


def task_slot(kind: str, now: datetime) -> str | None:
    """回傳此刻該任務所屬的「排程格」識別字串；不在活躍時段回 None。

    同一格只會觸發一次（由呼叫端記錄上次觸發的格），所以就算迴圈某一輪
    因為抓取耗時而晚了幾十秒醒來，也會補觸發當前格、不會重複觸發。
    """
    d, h, m = now.date(), now.hour, now.minute
    if kind == "dawn":
        # 04:55~05:50 每分鐘
        if (h == 4 and m >= 55) or (h == 5 and m <= 50):
            return f"{d}|{h:02d}:{m:02d}"
        return None
    if kind == "today":
        if (h == 4 and m >= 55) or h == 5:
            return None  # 讓給 dawn
        if 1 <= h <= 4:
            return f"{d}|{h:02d}:{m // 20 * 20:02d}"  # 每 20 分
        return f"{d}|{h:02d}:{m // 10 * 10:02d}"      # 每 10 分
    if kind == "hot":
        # 05~23 時，每 10 分的尾數 7 之後觸發
        if 5 <= h <= 23 and m % 10 >= 7:
            return f"{d}|{h:02d}:{m // 10}"
        return None
    if kind == "week":
        # 每小時 :15 之後觸發
        if m >= 15:
            return f"{d}|{h:02d}"
        return None
    if kind == "mid":
        if h in (10, 14, 22) and m >= 40:
            return f"{d}|{h:02d}"
        return None
    if kind == "far":
        if h in (11, 23) and m >= 50:
            return f"{d}|{h:02d}"
        return None
    raise ValueError(kind)


def due_tasks(now: datetime, fired: dict[str, str]) -> list[str]:
    """回傳此刻應觸發的任務（依優先序），並更新 fired 記錄。"""
    due = []
    for kind in ("today", "dawn", "hot", "week", "mid", "far"):
        slot = task_slot(kind, now)
        if slot is not None and fired.get(kind) != slot:
            fired[kind] = slot
            due.append(kind)
    return due


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def git_commit_push(kinds: list[str], now: datetime) -> None:
    """有變化就 commit 並推送；推送衝突時 CSV 靠 union merge、其餘取本輪新值。"""
    run(["git", "add", "data"])
    if run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    msg = f"snapshot({'+'.join(kinds)}): {now:%Y-%m-%d %H:%M}"
    run(["git", *GIT_IDENTITY, "commit", "-m", msg])
    for _ in range(3):
        if run(["git", "push"]).returncode == 0:
            return
        if run(["git", "pull", "--rebase"]).returncode != 0:
            run(["git", "checkout", "--theirs", "--", "data"])
            run(["git", "add", "data"])
            if run(["git", *GIT_IDENTITY, "-c", "core.editor=true", "rebase", "--continue"]).returncode != 0:
                run(["git", "rebase", "--abort"])
    run(["git", "push"])  # 最後再試一次；仍失敗就留給下一輪 commit 一起推


def main_loop(max_minutes: float, dry_run: bool) -> None:
    start = datetime.now(TZ_TAIPEI)
    deadline = start + timedelta(minutes=max_minutes)
    fired: dict[str, str] = {}
    n_runs = 0
    print(f"[run_loop] start {start:%Y-%m-%d %H:%M:%S} (max {max_minutes} min)", flush=True)
    while True:
        now = datetime.now(TZ_TAIPEI)
        if now >= deadline:
            break
        due = due_tasks(now, fired)
        if due:
            for kind in due:
                print(f"[run_loop] {now:%H:%M:%S} fire {kind}", flush=True)
                if dry_run:
                    continue
                result = run(TASK_COMMANDS[kind])
                if result.returncode != 0:
                    print(f"[run_loop] WARN {kind} exited {result.returncode}（本輪跳過，下一格再試）", flush=True)
                elif kind == "week":
                    run([sys.executable, "analyze.py"])
            n_runs += len(due)
            if not dry_run:
                git_commit_push(due, now)
        time.sleep(10)
    print(f"[run_loop] done, {n_runs} task runs in {(datetime.now(TZ_TAIPEI) - start).seconds // 60} min", flush=True)


def selftest() -> None:
    """模擬時鐘走完一整天（10 秒步進），驗證各層觸發次數符合設計。"""
    fired: dict[str, str] = {}
    counts: dict[str, int] = {}
    t = datetime(2026, 7, 20, 0, 0, 0, tzinfo=TZ_TAIPEI)
    end = t + timedelta(days=1)
    while t < end:
        for kind in due_tasks(t, fired):
            counts[kind] = counts.get(kind, 0) + 1
        t += timedelta(seconds=10)
    expected = {"today": 6 + 3 * 3 + 3 + 18 * 6, "dawn": 56, "hot": 19 * 6, "week": 24, "mid": 3, "far": 2}
    ok = True
    for kind, want in expected.items():
        got = counts.get(kind, 0)
        mark = "OK" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {kind:6s} {got:4d} / 預期 {want:4d}  {mark}")
    total = sum(counts.values())
    print(f"  合計 {total} 次/日")
    sys.exit(0 if ok else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-minutes", type=float, default=58, help="迴圈最長分鐘數（預設 58）")
    parser.add_argument("--dry-run", action="store_true", help="只印出觸發決策，不執行抓取與 git")
    parser.add_argument("--selftest", action="store_true", help="模擬一整天驗證觸發次數")
    args = parser.parse_args()
    if args.selftest:
        selftest()
    else:
        main_loop(args.max_minutes, args.dry_run)


if __name__ == "__main__":
    main()
