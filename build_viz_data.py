# -*- coding: utf-8 -*-
"""重算「釋票視覺化報告.html」內嵌的 DATA 物件與文案數字。

當初（2026-07-29）那份視覺化報告是一次性分析、沒留腳本。這支補上，
讓報告可隨 transitions.csv 重跑。輸出一份 JSON 到 stdout（給人／給編輯用），
不直接改 HTML。

用法：python build_viz_data.py   （寫入 _scratch/viz_data.json，UTF-8）

方法論（沿用原報告 caption 的定義）：
- 售完事件 = new_status == 'Full'
- 釋出事件 = old_status == 'Full' 且 new_status in ('Available','Limited')
- 偵測視窗 = detected_taipei - prev_snapshot_taipei；時段類圖表只收 <= 20 分鐘的高解析事件
- 夜間 feed 凍結窗約 23:50–05:10，靠「偵測視窗 <= 20 分」自然濾掉（凌晨取樣稀疏＝視窗大）
"""
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "transitions.csv"

HIRES_MIN = 20          # 偵測視窗門檻（分鐘）
WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def load():
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def is_sellout(r):
    return r["new_status"] == "Full"


def is_release(r):
    return r["old_status"] == "Full" and r["new_status"] in ("Available", "Limited")


def dt(s):
    return datetime.fromisoformat(s)


def window_min(r):
    """偵測視窗（分鐘）。缺 prev_snapshot 時回傳大值＝視為低解析。"""
    prev = r.get("prev_snapshot_taipei") or ""
    if not prev:
        return 9999.0
    try:
        return (dt(r["detected_taipei"]) - dt(prev)).total_seconds() / 60
    except ValueError:
        return 9999.0


def bucket(minutes, edges, labels):
    for edge, label in zip(edges, labels):
        if minutes < edge:
            return label
    return labels[-1]


def main():
    rows = load()
    n_total = len(rows)

    releases = [r for r in rows if is_release(r)]
    sellouts = [r for r in rows if is_sellout(r)]

    today_rel = [r for r in releases if r["run_kind"] == "today"]
    hires_rel = [r for r in today_rel if window_min(r) <= HIRES_MIN]
    lowres_rel_today = len(today_rel) - len(hires_rel)

    # ── 1. hourly：24 小時釋出量（今日票、高解析）──────────────
    hourly = [0] * 24
    for r in hires_rel:
        hourly[dt(r["detected_taipei"]).hour] += 1

    # ── 2. heat：星期 × 小時（今日票、高解析）────────────────
    heat = [[0] * 24 for _ in range(7)]
    for r in hires_rel:
        t = dt(r["detected_taipei"])
        heat[t.weekday()][t.hour] += 1

    # ── 3. dowDays：各星期的觀測日數（有 today 資料的日子）─────
    today_dates = {dt(r["detected_taipei"]).date() for r in rows if r["run_kind"] == "today"}
    dow_days = [0] * 7
    for d in today_dates:
        dow_days[d.weekday()] += 1

    # ── 4. wait：當日售完 → 當日釋出，高解析配對 ───────────────
    #   逐 (方向×乘車日×車次×車廂) 重建狀態，兩端都須高解析、同日
    groups = defaultdict(list)
    for r in rows:
        if r["run_kind"] != "today":
            continue
        try:
            if int(r["days_before"]) > 0:
                continue
        except ValueError:
            continue
        key = (r["direction"], r["train_date"], r["train_no"], r["seat_class"])
        groups[key].append(r)

    wait_edges = [10, 30, 60, 120, 240, 480]
    wait_labels = ["<10分", "10–30分", "30–60分", "1–2時", "2–4時", "4–8時", "8–24時"]
    wait_ctr = Counter()
    wait_gaps = []
    for items in groups.values():
        t_enter = None
        for r in sorted(items, key=lambda x: x["detected_taipei"]):
            t = dt(r["detected_taipei"])
            hi = window_min(r) <= HIRES_MIN
            if r["new_status"] == "Full":
                t_enter = t if hi else None
            elif r["old_status"] == "Full":
                if t_enter is None or not hi:
                    t_enter = None
                    continue
                if t_enter.date() != t.date():
                    t_enter = None
                    continue
                gap = (t - t_enter).total_seconds() / 60
                if 0 <= gap < 1440:
                    wait_ctr[bucket(gap, wait_edges, wait_labels)] += 1
                    wait_gaps.append(gap)
                t_enter = None
    wait = [{"label": l, "n": wait_ctr.get(l, 0)} for l in wait_labels]
    wait_n = len(wait_gaps)
    wait_gaps.sort()

    def _pct(data, p):
        if not data:
            return 0
        return round(data[min(int(p / 100 * len(data)), len(data) - 1)])

    # ── 5. dep：釋出時刻距發車多久（今日票、高解析、發車前）─────
    dep_edges = [15, 30, 60, 120, 240, 480]
    dep_labels = ["<15分", "15–30分", "30–60分", "1–2時", "2–4時", "4–8時", ">8時"]
    dep_ctr = Counter()
    dep_n = 0
    after_departure = 0
    for r in hires_rel:
        try:
            dep_dt = dt(f"{r['train_date']}T{r['departure_time']}:00+08:00")
        except ValueError:
            continue
        lead = (dep_dt - dt(r["detected_taipei"])).total_seconds() / 60
        if lead <= 0:
            after_departure += 1
            continue
        dep_ctr[bucket(lead, dep_edges, dep_labels)] += 1
        dep_n += 1
    dep = [{"label": l, "n": dep_ctr.get(l, 0)} for l in dep_labels]

    # ── 6. dirs：各方向 售完 vs 釋出（全體，不濾視窗）────────────
    so_by_dir = Counter(r["direction"] for r in sellouts)
    re_by_dir = Counter(r["direction"] for r in releases)
    dirs = [
        {"d": d, "so": so_by_dir[d], "re": re_by_dir[d]}
        for d, _ in so_by_dir.most_common()
    ]

    # ── 7. tl：每日狀態轉變筆數（補 0 空窗）─────────────────────
    per_day = Counter(dt(r["detected_taipei"]).date() for r in rows)
    d0, d1 = min(per_day), max(per_day)
    tl = []
    cur = d0
    while cur <= d1:
        tl.append([cur.strftime("%m-%d"), per_day.get(cur, 0)])
        cur += timedelta(days=1)
    gap_days = [d for d, n in tl if n == 0]

    # ── 8. regimes：取樣制度分段（index 對應 tl 陣列）────────────
    def idx_of(mmdd):
        for i, (d, _) in enumerate(tl):
            if d == mmdd:
                return i
        return None

    n_tl = len(tl)
    regimes = [
        {"from": 0,               "to": idx_of("06-20"), "label": "舊頻率",      "tint": True},
        {"from": idx_of("06-20"), "to": idx_of("06-27"), "label": "空窗",        "tint": False},
        {"from": idx_of("06-27"), "to": idx_of("07-16"), "label": "本機五層頻率", "tint": True},
        {"from": idx_of("07-16"), "to": idx_of("07-18"), "label": "空窗",        "tint": False},
        {"from": idx_of("07-18"), "to": idx_of("07-20"), "label": "六 workflow", "tint": True},
        {"from": idx_of("07-20"), "to": n_tl,            "label": "watch-loop",  "tint": True},
    ]

    # ── 配對存活分析：售完後從未釋出比例 ───────────────────────
    all_groups = defaultdict(list)
    for r in rows:
        key = (r["direction"], r["train_date"], r["train_no"], r["seat_class"])
        all_groups[key].append(r)
    released_pairs = stuck_pairs = 0
    for items in all_groups.values():
        pending = False
        for r in sorted(items, key=lambda x: x["detected_taipei"]):
            if r["new_status"] == "Full":
                pending = True
            elif r["old_status"] == "Full" and pending:
                released_pairs += 1
                pending = False
        if pending:
            stuck_pairs += 1
    pair_total = released_pairs + stuck_pairs
    never_pct = round(stuck_pairs / pair_total * 100) if pair_total else 0

    # ── 文案數字 ─────────────────────────────────────────────
    order = sorted(range(24), key=lambda h: hourly[h], reverse=True)
    peak_h, second_h = order[0], order[1]
    rates = {d["d"]: d["re"] / d["so"] for d in dirs if d["so"]}
    hi_dir = max(rates, key=rates.get)
    lo_dir = min(rates, key=rates.get)
    wait_flat = []
    for it in wait:
        wait_flat += [it["label"]] * it["n"]  # 只為找眾數區間，非精確中位數

    prose = {
        "span_days": len(tl),
        "gap_day_count": len(gap_days),
        "gap_days": gap_days,
        "n_total_rows": n_total,
        "sellout_events": sum(d["so"] for d in dirs),
        "release_events": sum(d["re"] for d in dirs),
        "never_released_pct": never_pct,
        "never_released_pairs": stuck_pairs,
        "pair_total": pair_total,
        "hourly_n": sum(hourly),
        "hourly_peak_hour": peak_h,
        "hourly_peak_n": hourly[peak_h],
        "hourly_second_hour": second_h,
        "hourly_second_n": hourly[second_h],
        "sun05_total": heat[6][5],
        "sun05_per_day": round(heat[6][5] / dow_days[6], 1) if dow_days[6] else 0,
        "heat_obs_days": sum(dow_days),
        "dow_days": dict(zip(WEEKDAYS, dow_days)),
        "wait_n": wait_n,
        "wait_median_min": _pct(wait_gaps, 50),
        "wait_p25_min": _pct(wait_gaps, 25),
        "wait_p75_min": _pct(wait_gaps, 75),
        "dir_rates": {d["d"]: round(d["re"] / d["so"] * 100, 1) for d in dirs if d["so"]},
        "dep_n": dep_n,
        "dep_after_departure": after_departure,
        "lowres_rel_today": lowres_rel_today,
        "dir_rate_hi": (hi_dir, round(rates[hi_dir] * 100, 1)),
        "dir_rate_lo": (lo_dir, round(rates[lo_dir] * 100, 1)),
        "dir_rate_min_pct": round(min(rates.values()) * 100),
        "dir_rate_max_pct": round(max(rates.values()) * 100),
        "date_first": min(dt(r["detected_taipei"]) for r in rows).strftime("%Y-%m-%d %H:%M"),
        "date_last": max(dt(r["detected_taipei"]) for r in rows).strftime("%Y-%m-%d %H:%M"),
    }

    out = {
        "DATA": {
            "hourly": hourly,
            "heat": heat,
            "dowDays": dow_days,
            "dowNames": WEEKDAYS,
            "wait": wait,
            "dep": dep,
            "dirs": dirs,
            "tl": tl,
            "regimes": regimes,
        },
        "prose": prose,
    }
    out_path = ROOT / "_scratch" / "viz_data.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已寫入 {out_path}  (rows={n_total})")


if __name__ == "__main__":
    main()
