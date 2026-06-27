# -*- coding: utf-8 -*-
"""彙整 data/transitions.csv，產出釋票/售完統計報告 data/report.md。

用法：python analyze.py
"""
import argparse
import csv
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRANSITIONS_CSV = ROOT / "data" / "transitions.csv"
REPORT_MD = ROOT / "data" / "report.md"

STATUS_RANK = {"Full": 0, "Limited": 1, "Available": 2}
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

# future 模式一天只有 3 個快照（TDX 於 10:00/16:00/22:00 更新），
# 偵測到變化只代表「發生在上一個快照之後」，所以用時間窗呈現
FUTURE_WINDOWS = {
    10: "22時～隔日10時",
    11: "22時～隔日10時",
    16: "10時～16時",
    17: "10時～16時",
    22: "16時～22時",
    23: "16時～22時",
}


def load_rows():
    if not TRANSITIONS_CSV.exists():
        return []
    with TRANSITIONS_CSV.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def classify(row):
    old, new = row["old_status"], row["new_status"]
    if old == "NEW":
        return "new_train"
    if old in STATUS_RANK and new in STATUS_RANK:
        if STATUS_RANK[new] > STATUS_RANK[old]:
            return "release"   # 釋票（Full→Limited/Available、Limited→Available）
        return "sellout"       # 變難買（→Limited/Full）
    return "other"


def days_bucket(value: str) -> str:
    try:
        d = int(value)
    except ValueError:
        return "未知"
    if d <= 0:
        return "當天"
    if d <= 3:
        return f"前{d}天"
    if d <= 7:
        return "前4–7天"
    if d <= 14:
        return "前8–14天"
    return "前15–27天"


def counter_table(counter: Counter, header=("項目", "次數"), sort_keys=False) -> list:
    lines = [f"| {header[0]} | {header[1]} |", "|---|---|"]
    items = sorted(counter.items()) if sort_keys else counter.most_common()
    for key, count in items:
        lines.append(f"| {key} | {count} |")
    return lines


def heatmap_lines(releases: list) -> list:
    """當日票釋票的「小時 × 星期」熱力表（只用 today，有小時解析度）。
    星期取偵測當下的星期，回答「週幾的幾點最容易撿到釋票」。"""
    grid = {}  # (hour, weekday) -> count
    for r in releases:
        if r["run_kind"] != "today":
            continue
        dt = datetime.fromisoformat(r["detected_taipei"])
        grid[(dt.hour, dt.weekday())] = grid.get((dt.hour, dt.weekday()), 0) + 1
    if not grid:
        return []

    lines = [
        "## 釋票熱力表（當日票：小時 × 星期）",
        "",
        "> 數字＝該時段偵測到的釋票次數，`·`＝0；只列有資料的小時。",
        "> 註：受抓取排程影響——凌晨 01–05 點 feed 凍結、抓得稀疏，該段偏冷屬正常。",
        "",
        "| 時＼週 | 一 | 二 | 三 | 四 | 五 | 六 | 日 | 小計 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    col_tot = [0] * 7
    for h in range(24):
        cells, row_tot = [], 0
        for wd in range(7):
            c = grid.get((h, wd), 0)
            cells.append(str(c) if c else "·")
            row_tot += c
            col_tot[wd] += c
        if row_tot == 0:
            continue  # 跳過整列皆 0 的小時，省版面
        lines.append(f"| {h:02d}時 | " + " | ".join(cells) + f" | {row_tot} |")
    lines.append("| **小計** | " + " | ".join(f"**{t}**" for t in col_tot) +
                 f" | **{sum(col_tot)}** |")
    return lines + [""]


def _daytime(t: datetime) -> bool:
    """是否落在白天可觀測窗 05:10–23:50（避開夜間 feed 凍結與 05:05 解凍假象）。"""
    mins = t.hour * 60 + t.minute
    return 5 * 60 + 10 <= mins <= 23 * 60 + 50


def _fmt_minutes(m: float) -> str:
    return f"{int(round(m))} 分" if m < 120 else f"{m / 60:.1f} 小時"


def sellout_release_lines(rows: list, min_full_minutes: int = 30) -> list:
    """售完→釋出時間差（把 Full 當「存續時間」做生存分析，只用當日票）。

    對每個 (車次×日期×車廂) 依時序重建狀態，用狀態機抓「進入 Full → 離開 Full」
    區間，時間差＝離開−進入。三道過濾：
      1) 沒看到進入時刻（解凍暴衝/左設限）的離開 → 跳過
      2) 兩端須同日且都在白天窗（避開凍結窗）
      3) Full 持續 < min_full_minutes → 視為門檻抖動丟棄
    滿到序列結束仍未釋出者計為右設限，單獨報「未釋出比例」。
    """
    groups = defaultdict(list)
    for r in rows:
        if r["run_kind"] != "today":
            continue
        try:
            if int(r["days_before"]) > 0:
                continue
        except ValueError:
            continue
        groups[(r["direction"], r["train_date"], r["train_no"], r["seat_class"])].append(r)

    durations, released, censored, jitter = [], 0, 0, 0
    for items in groups.values():
        t_enter = None
        for r in sorted(items, key=lambda x: x["detected_taipei"]):
            t = datetime.fromisoformat(r["detected_taipei"])
            if r["new_status"] == "Full":
                t_enter = t
            elif r["old_status"] == "Full":          # 離開 Full ＝ 釋出
                if t_enter is None:                   # 沒看到何時進入（解凍/左設限）
                    continue
                if not (_daytime(t_enter) and _daytime(t) and t_enter.date() == t.date()):
                    t_enter = None                    # 區間碰到凍結窗，剔除
                    continue
                mins = (t - t_enter).total_seconds() / 60
                if mins >= min_full_minutes:
                    durations.append(mins)
                    released += 1
                else:
                    jitter += 1
                t_enter = None
        if t_enter is not None and _daytime(t_enter):  # 滿到最後沒等到釋出
            censored += 1

    if not durations and not censored:
        return []

    durations.sort()
    pct = lambda p: durations[min(int(p / 100 * len(durations)), len(durations) - 1)]
    lines = [
        "## 售完 → 釋出時間差（當日票）",
        "",
        f"> 「滿了之後多久出現第一次釋出」。只算當日票、Full 持續 ≥ {min_full_minutes} 分"
        "（濾門檻抖動）、避開夜間凍結窗（05:10–23:50 外剔除）。",
        "> 解析度 ±10 分；`Full` 是三態標籤，非真正售罄。",
        "",
    ]
    if durations:
        lines += [
            f"- 有效「滿→釋出」樣本：**{released}** 段",
            f"- **中位數 {_fmt_minutes(pct(50))}**（一半的 Full 在此時間內等到釋出）",
            f"- 四分位：P25 {_fmt_minutes(pct(25))} ／ P75 {_fmt_minutes(pct(75))}",
            f"- 最短 / 最長：{_fmt_minutes(durations[0])} / {_fmt_minutes(durations[-1])}",
            "",
        ]
    total = released + censored
    note = f"（未釋出比例 {censored / total * 100:.0f}%）" if total else ""
    lines += [
        f"- 滿到觀測結束仍未釋出（右設限）：{censored} 段{note}",
        f"- 被門檻濾掉的短抖動：{jitter} 段",
        "",
    ]
    return lines


def main():
    parser = argparse.ArgumentParser(description="彙整 transitions.csv 產出 report.md")
    parser.add_argument("--min-full-minutes", type=int, default=30,
                        help="售完→釋出分析的最短 Full 持續門檻（分鐘），濾掉門檻抖動，預設 30")
    args = parser.parse_args()

    rows = load_rows()
    if not rows:
        print("data/transitions.csv 還沒有資料，先讓 fetch_snapshot.py 跑幾輪吧。")
        return

    releases = [r for r in rows if classify(r) == "release"]
    sellouts = [r for r in rows if classify(r) == "sellout"]
    new_trains = {  # 同一班車的 standard/business 會各記一筆，去重
        (r["direction"], r["train_date"], r["train_no"])
        for r in rows if classify(r) == "new_train"
    }

    lines = [
        "# 高鐵釋票觀測報告（台北⇄左營）",
        "",
        f"- 產出時間：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 累計紀錄：釋票 {len(releases)} 筆、售完/變少 {len(sellouts)} 筆、"
        f"新出現車次 {len(new_trains)} 班",
        "",
        "> 注意：未來日期(D+1~D+27)的資料 TDX 一天只更新 3 次（10/16/22 時），",
        "> 這類釋票只能定位到「時間窗」；當日資料每 10 分鐘更新，可定位到小時。",
        "",
    ]

    # --- 釋票時段 ---
    win_today = Counter()
    win_future = Counter()
    for r in releases:
        hour = datetime.fromisoformat(r["detected_taipei"]).hour
        if r["run_kind"] == "today":
            win_today[f"{hour:02d}時"] += 1
        else:
            win_future[FUTURE_WINDOWS.get(hour, f"{hour:02d}時(非預期)")] += 1

    lines += ["## 釋票發生時段", ""]
    if win_today:
        lines += ["### 當日票（解析度 10 分鐘，依偵測小時統計）", ""]
        lines += counter_table(win_today, ("時段", "釋票次數"), sort_keys=True) + [""]
    if win_future:
        lines += ["### 預售票（解析度＝TDX 更新窗）", ""]
        lines += counter_table(win_future, ("時間窗", "釋票次數")) + [""]

    # --- 釋票熱力表（小時 × 星期）---
    lines += heatmap_lines(releases)

    # --- 距乘車日 ---
    lines += ["## 釋票 vs 距乘車日", ""]
    lines += counter_table(
        Counter(days_bucket(r["days_before"]) for r in releases), ("距乘車日", "釋票次數")
    ) + [""]

    # --- 乘車日星期 ---
    by_weekday = Counter()
    for r in releases:
        wd = date.fromisoformat(r["train_date"]).weekday()
        by_weekday[f"週{WEEKDAY_ZH[wd]}"] += 1
    lines += ["## 釋票 vs 乘車日星期", ""]
    lines += counter_table(by_weekday, ("乘車日星期", "釋票次數")) + [""]

    # --- 方向 / 車廂 ---
    lines += ["## 方向與車廂", ""]
    lines += counter_table(Counter(r["direction"] for r in releases), ("方向", "釋票次數")) + [""]
    lines += counter_table(
        Counter("標準廂" if r["seat_class"] == "standard" else "商務廂" for r in releases),
        ("車廂", "釋票次數"),
    ) + [""]

    # --- 最常釋票的車次 ---
    top = Counter(
        f"{r['direction']} 車次{r['train_no']}({r['departure_time']})" for r in releases
    )
    lines += ["## 最常出現釋票的車次（前 15）", ""]
    lines += [f"| 車次 | 次數 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in top.most_common(15)] + [""]

    # --- 售完統計 ---
    lines += ["## 售完/變難買 vs 距乘車日", ""]
    lines += counter_table(
        Counter(days_bucket(r["days_before"]) for r in sellouts), ("距乘車日", "次數")
    ) + [""]

    # --- 售完→釋出時間差（生存分析）---
    lines += sellout_release_lines(rows, args.min_full_minutes)

    report = "\n".join(lines)
    REPORT_MD.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n已寫入 {REPORT_MD}")


if __name__ == "__main__":
    main()
