# 高鐵釋票觀測站（台北 ⇄ 左營）

用 TDX 官方 API 監控高鐵對號座剩餘狀態，長期統計「售完的班次什麼時候會釋出票」。
跑在 GitHub Actions 上，公開 repo 完全免費。

## 運作原理

- TDX 端點：`/v2/Rail/THSR/AvailableSeatStatus/Train/OD/{起站}/to/{迄站}/TrainDate/{日期}`
- 回傳每班車的 `StandardSeatStatus` / `BusinessSeatStatus`，值為三段式：
  `Available`（尚有座位）/ `Limited`（數量有限）/ `Full`（已售完）
- 每次抓快照與上次比對，狀態「變好」（如 `Full → Available`）就記為一次釋票，
  寫入 `data/transitions.csv`；`analyze.py` 彙整成 `data/report.md`

## 資料更新頻率限制（重要）

| 查詢對象 | TDX 更新頻率 | 本工具排程 | 釋票時間解析度 |
|---|---|---|---|
| 當日班次 | 每 10 分鐘 | 每 10 分鐘（營運時段） | 約 10–20 分鐘 |
| 未來 1–27 天 | 每日 3 次（10/16/22 時） | 每日 3 次（10:20/16:20/22:20） | 只能定位到時間窗 |

## 部署步驟

### 1. 申請 TDX 金鑰（免費）

1. 到 <https://tdx.transportdata.tw/> 註冊會員（一般會員免費，Email 驗證即可）
2. 登入後：右上角會員中心 →「資料服務」→「API 金鑰」→ 新增金鑰
3. 記下 `Client Id` 與 `Client Secret`

### 2. 本機測試（可選）

```powershell
pip install -r requirements.txt
$env:TDX_CLIENT_ID = "你的ClientId"
$env:TDX_CLIENT_SECRET = "你的ClientSecret"
python fetch_snapshot.py --probe    # 看 API 原始回傳，確認串接正常
python fetch_snapshot.py --today    # 抓一次今天的快照
```

### 3. 推上 GitHub

建一個**公開** repo（公開才有無限免費 Actions 分鐘數；資料只是座位狀態，無隱私問題）：

```powershell
gh repo create thsr-ticket-watch --public --source . --push
```

或在 GitHub 網頁建 repo 後 `git remote add origin ... && git push -u origin main`。

### 4. 設定 Secrets

GitHub repo → Settings → Secrets and variables → Actions → New repository secret：

- `TDX_CLIENT_ID`
- `TDX_CLIENT_SECRET`

### 5. 啟動

- repo → Actions 頁籤 → 如有提示就按「Enable workflows」
- 手動觸發一次驗證：選 `watch-future` → Run workflow
- 之後就全自動。累積幾天後看 `data/report.md`，或本機 `python analyze.py`

## 產出檔案

| 檔案 | 內容 |
|---|---|
| `data/transitions.csv` | 每筆狀態轉變：偵測時間、方向、乘車日、車次、車廂、舊→新狀態、距乘車日天數 |
| `data/report.md` | 統計報告：釋票時段分布、距乘車日分布、星期分布、熱門釋票車次 |
| `data/state/*.json` | 各方向×日期的最新快照（比對基準，過期自動清除） |

## 注意事項

- `old_status = NEW` 表示車次首次出現（多半是高鐵公告加開班次）
- GitHub Actions 的 cron 會有幾分鐘漂移，尖峰時段偶爾跳過一輪，對長期統計影響不大
- 排程 workflow 若 repo 60 天無活動會被自動停用，但本工具每天都在 commit 資料，不會觸發
- 想換監控站點：改 `fetch_snapshot.py` 的 `OD_PAIRS`（站碼可用 `python fetch_snapshot.py --stations` 查）
