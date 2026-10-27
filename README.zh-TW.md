# DS-M01：ETA 區間與延誤預警

[English](README.md)

本次交付完成 **M0 共用來源稽核與 M1 資料試跑**：包含原創的 2026 年虛構物流情境、依當時可知資料建立的特徵、獨立到場觀測標籤、本機 HTTP 接收驗證，以及 DuckDB／Parquet 資料倉儲。資料不是 Rollingtrans 真實營運資料；目前尚未訓練 ETA 模型，以下數字也不是模型成效。

## 目前成果

| 項目 | M1 結果 |
|---|---:|
| 2026 年 1–9 月試跑行程 | 120 趟 |
| GNSS 定位記錄 | 12,834 筆 |
| 預測快照／對應標籤 | 各 937 筆 |
| 已啟用標準表／原始必要表 | 17／28 張 |
| 可用／未成熟／取消／已到場標籤 | 457／288／11／181 筆 |
| 有效／低可信度／無定位快照 | 413／465／59 筆 |
| 訓練／排除／校正／測試行程 | 65／16／13／26 趟 |

目前 33 個單元測試通過；資料倉儲另執行 24 項檢查，最新結果記錄於 `reports/quality_report.json`。實際本機 HTTP 驗證讀取 94 頁、重試一次刻意注入的 503，並重播第一頁 137 筆；去重後沒有增加重複事件，接收內容與標準化原件一致。詳細範圍與證據位置見 [M1 範圍](docs/m1_scope.md)。

## 在 Mac 重現

需要 Python 3.11 以上版本。本次執行環境是 Linux／Python 3.12.14，尚未宣稱完成 macOS 實機驗證。解壓縮後，在 Terminal 進入本專案資料夾：

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m ds_m01.cli run --root .
```

`run` 會依設定重新產生 120 趟資料，建立快照、標籤與歷史參照，再驗證 DuckDB 與 Parquet。它會更新專案中的產出；需要保留原始交付版本時，請保留下載的壓縮檔。

接著驗證真正的本機 HTTP 接收，再用接收結果重建：

```bash
python -m ds_m01.cli http-check --root .
python -m ds_m01.cli build --root . --telemetry-mode http_snapshot
python -m ds_m01.cli validate --root .
python -m unittest discover -s tests -v
python scripts/check_rebuild.py --root .
```

先確認 `reports/last_http-check.json` 的狀態為 `passed`，再執行 `http_snapshot`。HTTP 服務只使用 `127.0.0.1` 的臨時連接埠，檢查結束就關閉；若環境不允許啟動，報告會標示 `blocked`。以上步驟不需要外部 API、帳號憑證或 PostgreSQL。

可選擇再跑產生器擴量與穩定性檢查：

```bash
python scripts/check_generator_stability.py --root .
```

它會在暫存目錄建立 120 趟與 1,800 趟原始資料，檢查結構、子集合一致性、排程及設備關係，通常需數十秒，實際時間依電腦而異。結果存於 `reports/generator_validation.json`；這不代表 M2 的完整流程已交付。

## 閱讀順序

| 路徑 | 內容 |
|---|---|
| `config/m1.json` | seed、資料命名空間、期間、截止時間、保留實體與設定 |
| `contracts/` | 原始需求、資料表清單與欄位契約 |
| `data/raw/` | 13 張來源表 JSONL |
| `data/ingested/` | 經實際 HTTP 接收的定位資料 |
| `data/processed/` | 4 張衍生表與合併後來源追溯資料 |
| `data/parquet/`、`data/ds_m01_m1.duckdb` | 17 張標準表及型別化資料庫 |
| `reports/` | 統計、品質檢查、執行時間與重建證據 |
| `validation/` | 隔離的合成真值，供產生器檢查，不作預測特徵 |
| `tests/` | 獨立的時間可知性、建表與傳輸測試 |

設定中的 `generated_at` 保留本次合成來源取得時的紀錄，以便重播時比對完全相同的檔案內容；每次指令實際完成時間另記錄於報告。若要產生新的資料版本，應另建設定，使用實際取得時間及新的資料命名空間。2026 情境日期不代表模型曾於該時間上線。

## 後續與交接

M2 還需要保留完整 1,800 趟端到端資料並完成驗證。模型訓練、區間／機率校正、最終測試、預警事件、行動追蹤與完整 O01–O03 成果仍待後續階段；另 11 張必要表保留到相應階段實作，2 張選用表維持停用。

PostgreSQL 連接程式與 DDL 已提供，但此環境**尚未執行原生 PostgreSQL 往返測試**。日後需要驗證時，依 [PostgreSQL 執行說明](docs/postgres_runtime_note.md) 在適合的本機環境執行即可；目前不需要提供資料庫憑證。

本次沒有建立 Git commit 或 push。所有簽署 commit、Verified 認證及 GitHub 上傳由使用者處理。DA-E01 維持原樣，仍走原本的獨立驗收。
