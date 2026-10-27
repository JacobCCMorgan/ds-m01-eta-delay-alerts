# DS-M01 M0／M1 變更紀錄

日期：2026-10-27 UTC（洛杉磯 2026-10-26）。本紀錄描述此次實作與測試發現，不代表 Git commit 歷史。

## 新增交付

- 完成 DA-E01 共用來源稽核、實體 ID 與 seed 相容性確認；維持第一案來源不變。
- 建立原創 2026 年 120 趟 M1 子集合、獨立觀測來源與隔離真值，保留完整 1,800 趟設計的擴量入口。
- 建立 point-in-time 快照、獨立到場標籤、行程切分、受限歷史參照及來源父節點追溯。
- 建立實際本機 HTTP 分頁與接收、重試、重播去重、內容衝突拒絕及後續輸入模式。
- 建立 17 張型別化 DuckDB／Parquet 表、24 項品質檢查與 33 個獨立單元測試（含來源發布防護）。
- 提供重建與產生器穩定性腳本、PostgreSQL 連接檢查／DDL，以及中英文執行說明。

## 測試發現後的修正

| 發現 | 現在行為／覆蓋方式 |
|---|---|
| 出發事件稍晚取得；若從出發時刻開始每 30 分鐘掃描，25 分鐘取消行程會沒有任何快照。 | 首次快照改由出發事件可取得時間觸發；本來源為出發後 20 秒。獨立建表測試確認短取消行程仍保留首個快照。 |
| 過去曾出現 arrived 狀態，不代表後來已知的更正仍應被忽略。 | 以當時最新已知狀態判定；後續更正可恢復建立快照，尚未取得的更正不得影響過去。相關狀態亦納入來源父節點。 |
| 設備有效關係與取得時間不同；關係未知或未解決時，不能以事後知識補齊。 | 同時檢查有效區間與已知區間，排除未解決關係，保留缺失定位。測試涵蓋 revision、半開區間與未知情況。 |
| 歷史時長若依派車計畫或過晚才取得的資料計算，會污染參照。 | 使用獨立出發觀測與成熟到場觀測；要求當時已知、精度合格、非自身行程及訓練範圍，並保留完整來源父節點。 |
| 延後取得的到場狀態可能使實際已到場後仍出現快照。 | 保留當時可知流程的結果，標為 `already_arrived` 並移出剩餘時間迴歸標籤，不用未來真值提前停止快照。 |

修正後目前結果為 937 個快照，包含 11 個取消標籤及 59 個無定位快照。完整分布以 `reports/m1_summary.json` 為準；資料數量變更不是模型成效提升。

## 來源發布與完整性防護

在覆寫原始資料並執行完整流程時，曾觀察到實際落盤筆數與產生器回傳筆數不一致，並造成後續關聯檢查失敗。使用隔離的既有目錄重跑時，原問題未能重現；**目前沒有證據證明單一根因**，不將推測寫成已確認原因。

產生器更新為 `ds-m01-synthetic-generator-1.0.1`：先在同一檔案系統的暫存目錄產生資料，逐檔 flush／fsync，再檢查筆數與 SHA256；以逐檔 atomic replace 發布，發布後讀回比對，最後發布 scenario registry。這是**逐檔原子替換**，不是跨所有專案檔案的單一交易；完整執行應依產生、接收、建表、驗證的順序進行。

新增 source manifest 記錄已核對的來源筆數與檔案雜湊；在設定啟用時，pipeline 與 warehouse 開始前驗證來源雜湊，來源內容若在生成後改變就停止，不繼續處理混合版本。新增的獨立來源發布防護測試確認這項拒絕行為。最終重建與產生器擴量驗證仍以設定凍結後的實際報告為準。

## 尚未宣稱完成

本次不是全案或最終使用者驗收。完整 1,800 趟 M2 流程、其餘 11 張必要表、模型與校正、預警及行動追蹤、O01–O03 完整成果、原生 PostgreSQL 服務往返仍有後續工作。產生器擴量與重建各次執行的結果只依相應 JSON 報告判定。

沒有建立任何 Git commit 或執行 push；簽署、Verified 認證及 GitHub 發布由使用者負責。


## 2026-10-27 M2 0.2.0

- Retained all 1,800 synthetic trips and complete as-of output/history rather than only a temporary generator expansion.
- Indexed trip inputs and streamed history/provenance with staged per-file publication; all seven original M1 derived files remain byte-identical under M1 settings.
- Added exhaustive 13-source manifest coverage, count and SHA checks; malformed/truncated rows fail closed.
- Created 28 typed schemas/Parquet exports, with 11 future model/alert tables explicitly empty.
- Replaced repeated full-snapshot scans and Python provenance retention with indexed/SQL validation.
- Added M2 maturity, cohort, history support and M1 continuity audit.
- Added pipeline staging/build outputs to Git/archive exclusions.
- Unit tests: 49 pass; warehouse: 27 pass; readiness: 22 pass. Full fresh rebuild receipt is delivered separately.
- The first profiling launcher could not start because /usr/bin/time was absent; Python resource profiling was used successfully. No data-stage failure was hidden.
- No Git commit/push, model training, fake deployment date or PostgreSQL success claim.
