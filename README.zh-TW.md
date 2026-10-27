# DS-M01 到站時間區間與延誤預警

[English](README.md)

**M2 完整資料里程碑，程式版本 0.2.0。** 本專案使用原創的 2026 年合成車隊情境，建立可重現的資料流程、當時可知的特徵與獨立到場標籤。這是獨立作品集練習，不是真實 Rollingtrans 營運資料；尚未訓練模型或驗證預警績效。

## 本輪結果

2026 年 1–9 月共 1,800 趟，每月 200 趟；197,303 筆 GPS、14,349 張快照及同數量標籤、2,536,045 筆歷史參照、2,775,752 筆來源追溯紀錄。28 張表結構均已建立，其中 17 張有資料、11 張模型與通知相關表明確保留空白，等待後續階段。

| 分組 | 行程 | 有合格標籤行程 | 合格快照 |
|---|---:|---:|---:|
| 訓練 | 992 | 805 | 3,670 |
| 校準 | 171 | 130 | 596 |
| 測試 | 400 | 328 | 1,578 |
| 排除 | 237 | 195 | 1,122 |

全部快照中，合格標籤 6,966、尚未成熟 4,272、取消 164、實際已到場但當時系統尚未知悉 2,947。合格標籤含排除組 1,122 張，不能全部拿來訓練或評估。定位有效 6,394、陳舊 7,096、缺失 859。情境比例是工程測試設計，不能解讀為真實車隊發生率。

49 項程式測試、27 項資料庫檢查及 22 項 M2 準備度檢查通過。M1 的 120 趟業務資料及 937 組快照／標籤完整保留；串流優化在原 M1 設定下，7 個衍生 JSONL 與舊版本逐位元相同。實際 HTTP 接收 1,441 頁，刻意注入的 503 成功重試，重播 137 筆未造成重複資料。完整重建的實際結果記錄於 `reports/rebuild_report.json`。

## 可以接續 M3 的條件與限制

時間切分與保留車輛／路線維持凍結。僅未見路線的測試組有 29 趟合格行程；車輛與路線皆未見的組別只有 4 趟，後續必須揭露小樣本限制。訓練組 97 張合格快照缺少歷史池，基準模型須明確回退或回報無法提供。不要用測試組結果調整模型、校準或門檻，也不能把同一行程的多張快照當成獨立行程。

## 重跑方式

Python 3.11 以上；本輪實測 Linux／Python 3.12.14，尚未驗證 Mac。下載原始碼包可重建資料；完整包則已包含 JSONL、Parquet、DuckDB 與獨立情境真值。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m ds_m01.cli run --root . --config config/m2.json
python -m ds_m01.cli http-check --root . --config config/m2.json
python scripts/check_m2_readiness.py --root .
python -m unittest discover -s tests -v
python scripts/check_rebuild.py --root . --config config/m2.json
```

預設使用 `config/m2.json`，建議明確傳入設定。`run` 會替換目標資料夾內產生的資料；M1 重跑請使用另一資料夾。完整資料解壓約 12.6 GB，本次觀察的程序記憶體峰值約 7.23 GiB；完整乾淨目錄重建與 HTTP 對帳請預留約 30 GB 磁碟空間，實機需求可能不同。

## 交付與尚待完成

`reports/` 保存本輪實測證據；`reports/m1_reference/` 保存 M1 舊里程碑證據。`data/source_manifest.json` 檢查 13 張原始表必須完整、唯一且筆數／SHA256 一致。`validation/` 僅供工程驗證，不可當成特徵。11 張空表只完成欄位型別與空值規則，後續主鍵約束、填值及模型／通知行為仍須驗證。

原生 PostgreSQL 服務尚未實測：目前沒有相容服務或 DSN；指令與阻礙見 `docs/postgres_runtime_note.md`。M3 將完成基準及候選模型、區間與機率校準、凍結最終評估；預警通知、採用及行動追蹤仍屬後續工作。

GitHub 基準為你已簽署且 Verified 的 M1 commit `ff9ad674434064366c789b5d7de944bfbdb5b0d4`。本輪沒有代替你 commit 或 push；保留現有 repository 的 `.git` 與歷史，以 M2 原始碼更新後由你建立下一筆簽署提交。DA-E01 未變更，仍待獨立驗收。
