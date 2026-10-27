# DS-M01 M1 範圍與證據

本文件說明本次可驗收的工程成果。主期為 2026-01-01 至 2026-09-30，全部為原創合成情境；資料、技術測試與模型成效分開認定。

## 已實作

- **M0 共用來源稽核**：核對 DA-E01 原始封裝與檔案雜湊，保留實體識別規則與 deterministic seed 語意，DA-E01 檔案未修改。證據為 `shared_runtime_audit.json`／`.md`。
- **M1 產生器**：固定 120 趟子集合，涵蓋 3 租戶、30 車、12 路線及 11 種情境；使用新 DS-M01 資料命名空間。沿用實體 ID 不等於沿用 2025 行程或真實資料。
- **時間可知性流程**：依事件發生時間與資料可取得時間、承諾版本、設備關係版本和已知狀態建立快照；特徵完成後才加入標籤。
- **獨立標籤與歷史參照**：到場由 gate／sign-in 型合成觀測建立，與最後 GPS 點、服務開始、交付完成分開；歷史只使用合格訓練行程及當時已知的成熟觀測。
- **實際 HTTP 接收**：本機分頁、503 重試、cursor 檢查、重播去重與內容雜湊比對；接收結果可供相同特徵流程使用。
- **DuckDB／Parquet**：型別化建立 17 張標準表，執行時間可知性、標籤、來源追溯、保留實體及結構檢查。

## 目前資料規模

`reports/m1_summary.json` 與 `reports/quality_report.json` 是本節的權威來源。

| 項目 | 數量 |
|---|---:|
| 行程／定位記錄 | 120／12,834 |
| 快照／標籤／歷史參照 | 937／937／11,594 |
| 標籤：eligible／not_mature／cancelled／already_arrived | 457／288／11／181 |
| 定位：valid／low_confidence／no_position | 413／465／59 |
| 行程切分：train／excluded／calibration／test | 65／16／13／26 |

這些是試跑資料數量。保留未知、未成熟及取消資料是預期行為；它們不被填為零秒或準時。實際已到場、但到場狀態尚未取得時產生的快照會保留，並排除於剩餘時間迴歸標籤。

候選快照最長追蹤出發後 12 小時；定位超過 900 秒標示低可信度。第一個快照由取得出發事件觸發，之後每 30 分鐘建立。本合成來源在出發後 20 秒收到事件，因此可保留 25 分鐘即取消行程的首個可知快照。

## 表格與階段

| 分組 | 本次啟用表 |
|---|---|
| 共用與追溯 | `core_tenant`、`core_vehicle`、`core_device`、`core_vehicle_device_assignment`、`provenance_record` |
| ETA 來源 | `eta_trip`、`eta_facility_version`、`eta_route_version`、`eta_trip_plan_version`、`eta_promise_version`、`eta_trip_event`、`eta_trip_outcome` |
| 定位來源 | `telemetry_event` |
| M1 衍生 | `eta_snapshot`、`eta_snapshot_label`、`eta_split_assignment`、`eta_history_reference` |

原始 28 張必要表與 110 項需求維持原範圍。本次啟用 17 張；其餘 11 張為 `eta_model_version`、`eta_evaluation_membership`、`eta_prediction`、`eta_alert_rule_version`、`eta_output_release`、`eta_alert_episode`、`eta_alert`、`eta_notification_delivery`、`eta_adoption`、`eta_action`、`eta_action_followup`，待相應模型與預警階段實作。`eta_forecast_version`、`eta_routing_response` 兩張選用表尚未啟用。

## 驗證證據

| 證據 | 已確認內容與限制 |
|---|---|
| `reports/quality_report.json` | 937 快照版本的 24 項檢查、17 張表輸出；以最新 `passed` 與逐項違反筆數判定，不是 28 張表全案驗收。 |
| `tests/`；`reports/unittest_results.txt` | 現有 33 個測試通過：20 個時間可知性、4 個建表不變條件、8 個 HTTP、1 個來源發布防護測試；封裝測試紀錄應對應最後一次執行。 |
| `reports/last_http-check.json` | 實際 94 頁、1 次重試、137 筆重播、0 筆新增重複事件；12,834 筆接收內容與標準化來源一致，服務已關閉。 |
| `reports/rebuild_report.json` | 重建腳本的輸出位置；以實際檔案的 `status` 與逐檔比對判定，不以文件代替執行。涵蓋新目錄重建及 HTTP 輸入特徵一致性。 |
| `reports/generator_validation.json` | 可選產生器擴量檢查的實際結果；以 `status`／`failure` 判定。暫存 1,800 趟原件通過也不等於 M2 全流程交付。 |
| `docs/postgres_runtime_note.md` | 原生 PostgreSQL 往返尚未執行，環境缺乏可用服務；提供程式／DDL 不算服務驗證通過。 |

HTTP 的原檔位元組可能因排序不同而與輸出不同；驗證比較完整事件身分及內容，再確認輸出與標準排序 JSONL 的 SHA256 相同。事件去重鍵包含租戶、事件及 revision；相同鍵的不同內容會被拒絕。

## 重現與尚待完成

執行方式見專案 README。相同設定保留來源取得紀錄 `generated_at`，實際執行時間另記於 `reports/last_*.json`。新資料版本應使用新的來源取得時間與 dataset namespace，不能把重播時間假裝成過去上線時間。

M2 仍需完成保留的 1,800 趟端到端資料、擴量後品質與樣本量檢查；9 個月共 1,800 趟的月均密度低於舊 6 個月設計。後續還包含模型選擇、區間與機率校正、最終測試、警報門檻、預警與行動追蹤，以及完整 O01–O03 成果。現階段沒有模型準確度、覆蓋率、預警 precision／recall 或營運效益結論。

PostgreSQL 可在使用者日後選擇的本機環境另行驗證；M1 snapshot／HTTP 路徑不需要資料庫憑證。所有 Git 簽署 commit 與 push 由使用者執行。
