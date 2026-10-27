# Product / Brand Alias 審核資料集 v1.0

> 由 `python -m scripts.build_product_alias_overlay build` 產生（builder 1.0）。**離線審核資料，尚未接入 runtime，沒有任何 runtime-active 別名。**
> 範圍：Taiwan Phase 1 only；證據只來自台灣 TFDA 開放資料中「同一筆官方紀錄」的中文品名 ↔ 英文品名，不翻譯。

## 檔案

| 檔案 | 內容 |
|---|---|
| `candidates.csv` | 官方英文品名候選（每個產品一列）：UNIQUE / COLLIDING / INVALID_OR_GENERIC |
| `brand_candidates.csv` | 英文品牌詞 ↔ 中文品名前綴候選（全部 CANDIDATE_ONLY） |
| `reviewed_aliases.csv` | 人工審核後的結果；目前只有表頭。**只有這個檔案的 ACCEPT 列才可能在日後 D3 被讀取** |
| `overlay_manifest.json` | 版本、來源雜湊、檔案雜湊、計數、validator 版本 |

## 計數（產生當下）

- 官方紀錄 9112；沒有英文品名 919
- 可用官方英文品名 8172；不同正規化名稱 7490；涵蓋產品 8172
- UNIQUE_OFFICIAL_ENGLISH：6947 列（`EXACT_REVIEW_REQUIRED`；其中去標點後仍與他品相同 155 列，需特別看 `conflict_check`）
- COLLIDING_OFFICIAL_ENGLISH：543 組 / 1225 產品（`CANDIDATE_ONLY`，永遠不得自動確認）
- INVALID_OR_GENERIC：21 列（`NEVER_RUNTIME`）
- 品牌候選 245，通過規則 51（其中有中文前綴 39）
- 審核決定：ACCEPT 0 / REJECT 0 / NEEDS_REVIEW 8438

## 審核方式

1. 依 `review_batch`（每批約 200 列；品牌每批 50 列）逐批審核，先審 `UNIQUE-*`。
2. 在 `decision` 填 `ACCEPT` / `REJECT` / `NEEDS_REVIEW`，並填 `decision_reason`、`reviewer`、`reviewed_at`。**只有這三個值；不可自訂狀態。**
3. 對照 `evidence_quote`（官方原文）與 `source_url` / `source_record_id`；不可用購物網站、品牌官網、社群貼文、AI 翻譯當證據。
4. 把審核完的列複製到 `reviewed_aliases.csv`（保留原 `review_id`），並升版（新目錄或新 `overlay_version`）；不要原地改動候選檔。
5. 執行 validator；有 ERROR 不得採用。

## 不可違反

- UNIQUE 不等於 runtime-active；ACCEPT 也只代表「日後可進入 reviewed exact retrieval」，runtime 證據等級日後由證據欄位推導，不手填。
- COLLIDING 與品牌永遠是候選；品牌不產生所有權（OWNS_BRAND）或官方品牌關係。
- 來源 sha256 改變必須升版（validator 會檢查）；validator 不會自動修改任何列。
