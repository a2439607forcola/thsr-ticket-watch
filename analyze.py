# -*- coding: utf-8 -*-
"""彙整 data/transitions.csv，產出釋票/售完統計報告 data/report.md。

用法：python analyze.py
"""
import csv
from collections import Counter
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


def main():
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

    report = "\n".join(lines)
    REPORT_MD.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n已寫入 {REPORT_MD}")


if __name__ == "__main__":
    main()
