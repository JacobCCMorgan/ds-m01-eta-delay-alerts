# DS-M01 M0：共用程式與實體識別碼稽核

稽核結論：可以沿用實體 ID、命名 seed 規則與少量通用工具；DA-E01 的 2025 油耗產生器、資料契約與分析流程不能整套搬入 ETA 專案。本次 **10 組窄驗證通過**，來源檔案未改動；這不是 M1 完整流程或模型驗收結果。

## 來源與實際檢查

- 來源：`DA-E01_v1_source.zip`，Drive 檔案 ID `1L4nXY0KHzD6aaXMkwtzVg9PDtwYQ5xLS`。
- 獨立計算 ZIP SHA256：`c6f890c1bd0724bcf3f133dde50957a259502280c05f9f4eadd2f75daacc1e0a`。
- 抽出來源共 165 個檔案，檢查前後逐檔 SHA256 相同。
- 來源樹摘要：`5eb5c3cda0d3b5ef1f4273a31fd2121b205b292fadc2936fc2517566d9ab04fa`。算法及選定檔案雜湊見同名 JSON。
- Python 3.12.14；停用 bytecode 寫入。未完整驗證依賴鎖定檔的所有版本。
- 稽核執行時間：`2026-10-27T00:51:53.392378+00:00`（來源 manifest helper 加入後重新稽核）。

本次 DS-M01 `common.py` SHA256：`1aa015f6f39c1ca0a7f0a4496bec6ad185f0521644c2bf7b2125a17d60b64b94`。新增 `write_source_manifest`／`verify_source_manifest` 前的程式位元組摘要為 `2b24ce3bedab5ef6dc70d2896d92f6b303802d232d58d1b35e6282efaf140a6c`，與前次完整 helper 檔案摘要相同，確認既有 seed、digest、時間與序列化 helper 未修改。

新增 manifest 函式本次只檢閱程式：寫入端核對已落盤筆數並記錄檔案 SHA256；驗證端在設定啟用時供 pipeline／warehouse 核對來源檔案。其整合測試由主流程另行記錄，本次 M0 更新沒有讀寫 DS-M01 專案資料，也不將這些函式解讀成全專案交易保證。

## 可沿用的範圍

| 項目 | 決定與相容性 |
|---|---|
| 實體 ID | 沿用 3 租戶、30 車輛、36 裝置、45 駕駛、12 站點、12 路線的識別規則；2026 的有效期間、設備指派與路線版本重新產生。 |
| 命名 seed | 原 `sub_seed` 與 DS-M01 `stable_seed` 的 3 組測試向量相同；新資料使用 `ds-m01-2026-v1` namespace。 |
| SHA256 與 JSON | 檔案摘要相容；DS-M01 明確使用 UTF-8、排序欄位及 `allow_nan=False`。JSONL 寫入僅檢閱程式，未在此稽核執行。 |
| 時間 | DS-M01 明確拒絕無時區時間並轉為 UTC；保留語意，不要求與舊函式的字串精度完全相同。 |
| Cursor／重試／隔離／來源快照 | 作為設計參考；只重驗 cursor 純函式，尚未在本稽核重跑服務整合。 |

原 `common.ident` 是 SQL 識別字引用；DS-M01 的同名函式產生雜湊記錄 ID，**不是同一 API**。舊 `contract()`、fuel 表格 allowlist、Postgres 的 `FUEL_*` 設定及油耗單位轉換不直接沿用。

## 實體與 seed 契約

| 實體 | ID 範圍 |
|---|---|
| 租戶 | `T01`–`T03` |
| 車輛 | 各租戶 `Txx-V01`–`Txx-V10` |
| 裝置 | 各租戶 `Txx-D01`–`Txx-D12` |
| 駕駛 | 各租戶 `Txx-R01`–`Txx-R15` |
| 站點／路線 | `SITE-01`–`SITE-12`／`ROUTE-01`–`ROUTE-12` |

來源 ZIP 沒有生成後的 `data/raw` 實體登錄。本表是從實際 `simulation.py` 的迴圈及記憶體中的 `build_plan()` 重建，不宣稱讀到不存在的登錄檔。完整 ID 清單存於 JSON。

Seed 規則為：將 `seed` 及 tokens 轉為字串，以 `\x1f` 連接，UTF-8 編碼後取 SHA256 前 16 bytes，以 unsigned big-endian 解讀。主 seed 為 `20261021`。建議呼叫：

```python
stable_seed(20261021, "ds-m01-2026-v1", purpose, entity_id)
```

例如 `purpose="trip"`、`entity_id="T01-V01-TR001"` 的結果是 `22982877921785618888433484590010977744`。JSON 以十進位字串儲存完整 128-bit 值。

## 已重跑與尚未重跑

| 檢查 | 實際範圍與結果 |
|---|---|
| M0-01 | 直接執行既有 stable-ID／時間區間 test method，通過；未呼叫 unittest setUpClass 或 generate()。 |
| M0-02 | 3 組獨立 SHA256 seed 向量，通過。 |
| M0-03 | DST 前後明確 offset 的 UTC 正規化，以及拒絕無時區輸入，通過。 |
| M0-04 | cursor roundtrip 與錯誤 snapshot 拒絕，通過。 |
| M0-05 | digest、排序 JSON、SQL identifier 引用，通過。 |
| M0-06 | truth-prefix 防護及其侷限驗證，通過；`arrival_time` 不會被舊前綴規則攔截。 |
| M0-07 | 來源計畫重建出 1,800 趟、3 租戶、30 車、45 駕駛，通過。 |
| M0-08 | DS-M01 現有 helper 的 seed／digest 相容性及無時區拒絕，通過。 |
| M0-09 | 所有來源檔案檢查前後內容不變，通過。 |
| M0-10 | 新增 manifest helper 前的既有程式位元組與前次稽核完全相同，通過；新函式僅作來源檢閱。 |

本次 M0 稽核未重跑（M1 另有獨立報告）：DA-E01 完整測試套件及資料產生、Postgres、HTTP／重試／故障注入、DuckDB 全流程、DS-M01 M1 全流程、模型訓練／校正／測試。既有封裝報告的通過記錄不計入本次重跑結果。

## 對 2026 ETA 實作的限制

1. 來源 2025 路線是站點環狀連接的抽象合成路線，原行程距離與 90–180 分鐘時長各自產生。站點座標和舊行程時長不能當成真實路網 ETA 標籤。
2. 同一車輛等實體可延續 ID，但 2026 行程不是 2025 同一業務事件。跨專案資料串接必須帶 project／dataset 身分，防止同格式 trip ID 誤連。
3. 2026 主期延長為 9 個月但維持 1,800 趟，月均密度降低；不能宣稱與舊 6 個月設計有相同月別精度。
4. ETA 使用明確欄位 allowlist；真實到達、延誤標籤及模擬真值不得進入預測特徵。舊前綴檢查不足以提供這項保證。
5. 日期、版本有效區間、設備指派與可用時間重新建立。後續由 DS-M01 的契約、時間可用性與洩漏檢查驗證；本稽核不取代它們。

DA-E01 來源維持不變。本次未建立 Git commit，也未 push。
