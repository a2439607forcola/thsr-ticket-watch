# -*- coding: utf-8 -*-
"""TDX 高鐵剩餘座位監控。

抓取台北⇄左營各班次的對號座剩餘狀態快照，與前次快照比對，
把狀態轉變（釋票 / 售完 / 新出現車次）寫入 data/transitions.csv。

用法：
    python fetch_snapshot.py --today     # 只抓今天（搭配每 10 分鐘排程）
    python fetch_snapshot.py --future    # 抓 D+1 ~ D+27（搭配每日 3 次排程）
    python fetch_snapshot.py --probe     # 印出一次 API 原始回傳，檢查格式用
    python fetch_snapshot.py --stations  # 列出高鐵車站代碼

需要環境變數 TDX_CLIENT_ID / TDX_CLIENT_SECRET。
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Windows 主控台預設 cp950，遇到站名以外的字元會炸掉
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://tdx.transportdata.tw/api/basic/v2"
AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TAIPEI = ZoneInfo("Asia/Taipei")

# 官方代碼（ptx rail_thsr_codes.xsd）：台北 1000、台中 1040、左營 1070
OD_PAIRS = [
    {"origin": "1000", "dest": "1070", "label": "台北→左營"},
    {"origin": "1070", "dest": "1000", "label": "左營→台北"},
    {"origin": "1000", "dest": "1040", "label": "台北→台中"},
    {"origin": "1040", "dest": "1000", "label": "台中→台北"},
    {"origin": "1070", "dest": "1040", "label": "左營→台中"},
    {"origin": "1040", "dest": "1070", "label": "台中→左營"},
]

STATUS_RANK = {"Full": 0, "Limited": 1, "Available": 2}
STATUS_ALIAS = {"O": "Available", "L": "Limited", "X": "Full"}  # 舊版代碼容錯

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "state"
TRANSITIONS_CSV = DATA_DIR / "transitions.csv"
CSV_FIELDS = [
    "detected_utc", "detected_taipei", "prev_snapshot_taipei", "run_kind",
    "direction", "train_date", "train_no", "departure_time",
    "seat_class", "old_status", "new_status", "days_before",
]
# feed 新鮮度紀錄：每次當日抓取記下來源端 SrcUpdateTime，事後可檢驗夜間
# 凍結/解凍時刻與空窗（零額外 API 成本，重用已抓的 payload）
FRESHNESS_CSV = DATA_DIR / "feed_freshness.csv"
FRESHNESS_FIELDS = ["detected_utc", "detected_taipei", "src_update_time", "n_seats"]


def _request_with_retry(method: str, url: str, **kwargs):
    """發送 HTTP 請求，對逾時／連線錯誤（ReadTimeout、ConnectTimeout、連線重置等）重試。
    最多重試 3 次，每次間隔 15 秒（單純 sleep，不做指數退避——這類錯誤多半是
    暫時性網路問題，跟 429 限流的成因不同，兩者互不干擾）。3 次都失敗才把例外往上拋。"""
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            return requests.request(method, url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt >= max_retries:
                raise
            print(f"  網路逾時/連線錯誤（{type(e).__name__}），15s 後重試（{attempt + 1}/{max_retries}）...")
            time.sleep(15)


def get_token() -> str:
    cid = os.environ.get("TDX_CLIENT_ID")
    secret = os.environ.get("TDX_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("錯誤：請先設定環境變數 TDX_CLIENT_ID / TDX_CLIENT_SECRET")
    r = _request_with_retry(
        "POST",
        AUTH_URL,
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def api_get(token: str, path: str, extra_params: dict | None = None):
    url = f"{BASE_URL}/{path}"
    params = {"$format": "JSON"}
    if extra_params:
        params.update(extra_params)
    backoffs = [5, 10, 20, 40, 60, 90]
    for attempt, backoff in enumerate(backoffs + [0]):
        r = _request_with_retry(
            "GET",
            url,
            headers={"authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if r.status_code == 429:  # TDX 免費方案限流頗敏感，耐心退避
            if attempt >= len(backoffs):
                break
            wait = backoff
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = max(wait, int(retry_after))
            print(f"  429 限流，等 {wait}s 後重試（{attempt + 1}/{len(backoffs)}）...")
            time.sleep(wait)
            continue
        if r.status_code == 404:  # 尚未開放訂票的日期會查無資料
            return None
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"重試後仍被限流：{url}")


def fetch_od(token: str, origin: str, dest: str, train_date: str):
    """單一 OD 的剩餘座位（保留給 --probe 快速檢查用）。"""
    return api_get(token, f"Rail/THSR/AvailableSeatStatus/Train/OD/{origin}/to/{dest}/TrainDate/{train_date}")


def fetch_all_od(token: str, train_date: str):
    """取回某日期「我們監測的那幾條線」的剩餘座位。
    $select 砍欄位 + $filter 只回 OD_PAIRS 的線（全網 ~2500 筆篩到 ~130 筆），
    單次計量約少 9 成。加站只需改 OD_PAIRS，$filter 會自動跟著變。"""
    flt = " or ".join(
        f"(OriginStationID eq '{od['origin']}' and DestinationStationID eq '{od['dest']}')"
        for od in OD_PAIRS
    )
    return api_get(
        token,
        f"Rail/THSR/AvailableSeatStatus/Train/OD/TrainDate/{train_date}",
        {
            "$select": "TrainNo,OriginStationID,DestinationStationID,"
                       "StandardSeatStatus,BusinessSeatStatus",
            "$filter": flt,
        },
    )


def fetch_timetable_all(token: str, train_date: str) -> dict:
    """一次取回整天所有車次時刻表，建 (TrainNo, StationID)→DepartureTime。
    同車次自不同站發車時間不同，故以 (車次, 起站) 為鍵。"""
    payload = api_get(token, f"Rail/THSR/DailyTimetable/TrainDate/{train_date}")
    mapping = {}
    for item in payload or []:
        info = item.get("DailyTrainInfo") or {}
        train_no = str(info.get("TrainNo", "")).strip()
        if not train_no:
            continue
        for stop in item.get("StopTimes") or []:
            sid = str(stop.get("StationID", "")).strip()
            if sid:
                mapping[(train_no, sid)] = stop.get("DepartureTime", "")
    return mapping


def group_all_od(payload) -> dict:
    """把 fetch_all_od 的回傳依 (起站, 訖站) 分組 → {train_no: {standard, business}}。"""
    seats = payload.get("AvailableSeats") if isinstance(payload, dict) else (payload or [])
    result: dict = {}
    for item in seats or []:
        if not isinstance(item, dict):
            continue
        train_no = str(item.get("TrainNo", "")).strip()
        if not train_no:
            continue
        key = (str(item.get("OriginStationID", "")).strip(),
               str(item.get("DestinationStationID", "")).strip())
        result.setdefault(key, {})[train_no] = {
            "standard": normalize_status(item.get("StandardSeatStatus", "")),
            "business": normalize_status(item.get("BusinessSeatStatus", "")),
        }
    return result


def normalize_status(value: str) -> str:
    if value in STATUS_RANK:
        return value
    return STATUS_ALIAS.get(value, value or "")


def extract_trains(payload, timetable: dict | None = None) -> dict:
    """容錯解析：外層可能是 list，也可能是含 AvailableSeats 的 dict。"""
    if payload is None:
        return {}
    timetable = timetable or {}
    candidates = []
    if isinstance(payload, dict):
        candidates = payload.get("AvailableSeats") or payload.get("Trains") or []
    elif isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "AvailableSeats" in payload[0]:
            for block in payload:
                candidates.extend(block.get("AvailableSeats") or [])
        else:
            candidates = payload

    trains = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        train_no = str(item.get("TrainNo", "")).strip()
        if not train_no:
            continue
        trains[train_no] = {
            "dep": item.get("DepartureTime") or timetable.get(train_no, ""),
            "standard": normalize_status(item.get("StandardSeatStatus", "")),
            "business": normalize_status(item.get("BusinessSeatStatus", "")),
        }
    return trains


def state_path(od: dict, train_date: str) -> Path:
    return STATE_DIR / f"{od['origin']}-{od['dest']}_{train_date}.json"


def append_transitions(rows: list) -> None:
    if not rows:
        return
    is_new = not TRANSITIONS_CSV.exists()
    with TRANSITIONS_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def record_freshness(now_utc, now_tpe, payload) -> None:
    """記錄來源端 SrcUpdateTime（重用已抓 payload，不額外打 API）。
    隔天看凌晨那幾筆 src_update_time 有沒有前進，就知道 feed 是否夜間凍結。"""
    src = payload.get("SrcUpdateTime", "") if isinstance(payload, dict) else ""
    seats = payload.get("AvailableSeats") if isinstance(payload, dict) else None
    is_new = not FRESHNESS_CSV.exists()
    with FRESHNESS_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FRESHNESS_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "detected_utc": now_utc.isoformat(timespec="seconds"),
            "detected_taipei": now_tpe.isoformat(timespec="seconds"),
            "src_update_time": src,
            "n_seats": len(seats) if seats else 0,
        })


def cleanup_old_state(today_tpe: date) -> int:
    """乘車日已過的 state 檔不再需要，刪掉避免堆積。"""
    removed = 0
    for path in STATE_DIR.glob("*.json"):
        try:
            d = date.fromisoformat(path.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if d < today_tpe:
            path.unlink()
            removed += 1
    return removed


def run(run_kind: str, dates: list) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STATE_DIR.mkdir(exist_ok=True)
    token = get_token()
    now_utc = datetime.now(timezone.utc)
    now_tpe = now_utc.astimezone(TAIPEI)

    # 時刻表一天內近乎不變，抓第一個日期一次即可，建 (車次, 起站)→發車時間
    timetable = fetch_timetable_all(token, dates[0])

    new_rows = []
    fetched = 0
    for train_date in dates:
        payload = fetch_all_od(token, train_date)  # 一次拿整天所有 OD
        by_od = group_all_od(payload)
        if run_kind == "today":
            record_freshness(now_utc, now_tpe, payload)  # 記 feed 新鮮度（零額外 API）
        fetched += 1
        days_before = (date.fromisoformat(train_date) - now_tpe.date()).days
        for od in OD_PAIRS:
            trains = by_od.get((od["origin"], od["dest"]))
            if not trains:
                continue
            spath = state_path(od, train_date)
            old = json.loads(spath.read_text(encoding="utf-8")) if spath.exists() else None

            if old:
                prev_tpe = old.get("snapshot_taipei", "")
                for train_no, cur in sorted(trains.items()):
                    prev = old.get("trains", {}).get(train_no)
                    for seat_class in ("standard", "business"):
                        old_status = prev[seat_class] if prev else "NEW"
                        if old_status == cur[seat_class]:
                            continue
                        new_rows.append({
                            "detected_utc": now_utc.isoformat(timespec="seconds"),
                            "detected_taipei": now_tpe.isoformat(timespec="seconds"),
                            "prev_snapshot_taipei": prev_tpe,
                            "run_kind": run_kind,
                            "direction": od["label"],
                            "train_date": train_date,
                            "train_no": train_no,
                            "departure_time": timetable.get((train_no, od["origin"]), ""),
                            "seat_class": seat_class,
                            "old_status": old_status,
                            "new_status": cur[seat_class],
                            "days_before": days_before,
                        })

            spath.write_text(
                json.dumps(
                    {
                        "snapshot_utc": now_utc.isoformat(timespec="seconds"),
                        "snapshot_taipei": now_tpe.isoformat(timespec="seconds"),
                        "trains": trains,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        time.sleep(1.5)  # 每抓完一個日期的全量後對 TDX 客氣一點

    removed = cleanup_old_state(now_tpe.date())
    append_transitions(new_rows)
    print(
        f"[{now_tpe:%Y-%m-%d %H:%M}] {run_kind}: 查詢 {fetched} 個日期（各 1 次全 OD），"
        f"記錄 {len(new_rows)} 筆狀態轉變，清除 {removed} 個過期 state"
    )
    for row in new_rows:
        print(
            f"  {row['direction']} {row['train_date']} 車次{row['train_no']} "
            f"({row['departure_time']}) {row['seat_class']}: "
            f"{row['old_status']} → {row['new_status']}"
        )


def probe() -> None:
    token = get_token()
    today = datetime.now(TAIPEI).strftime("%Y-%m-%d")
    od = OD_PAIRS[0]
    payload = fetch_od(token, od["origin"], od["dest"], today)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text[:4000])
    print(f"\n--- 解析結果（共 {len(extract_trains(payload))} 班）---")
    for tn, info in sorted(extract_trains(payload).items())[:10]:
        print(f"  車次 {tn} {info['dep']} 標準:{info['standard']} 商務:{info['business']}")


def list_stations() -> None:
    token = get_token()
    for s in api_get(token, "Rail/THSR/Station") or []:
        name = s.get("StationName", {})
        print(f"  {s.get('StationID')}  {name.get('Zh_tw', '')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--today", action="store_true", help="只抓今天")
    group.add_argument("--future", action="store_true",
                       help="抓未來票（預設 D+1~D+27，可用 --days-from/--days-to 限範圍）")
    group.add_argument("--probe", action="store_true", help="印出 API 原始回傳")
    group.add_argument("--stations", action="store_true", help="列出車站代碼")
    parser.add_argument("--days-from", type=int, default=1, help="future 起始天數（含），預設 1")
    parser.add_argument("--days-to", type=int, default=27, help="future 結束天數（含），預設 27")
    parser.add_argument("--weekdays",
                        help="只抓這些星期的乘車日（0=一…6=日，逗號分隔），如 4,6=週五週日")
    args = parser.parse_args()

    if args.probe:
        probe()
        return
    if args.stations:
        list_stations()
        return

    today_tpe = datetime.now(TAIPEI).date()
    if args.today:
        run("today", [today_tpe.isoformat()])
    else:
        dates = [(today_tpe + timedelta(days=i)).isoformat()
                 for i in range(args.days_from, args.days_to + 1)]
        if args.weekdays:
            keep = {int(x) for x in args.weekdays.split(",")}
            dates = [d for d in dates if date.fromisoformat(d).weekday() in keep]
        if not dates:
            print("指定範圍內沒有符合的乘車日，跳過。")
            return
        run("future", dates)


if __name__ == "__main__":
    main()
