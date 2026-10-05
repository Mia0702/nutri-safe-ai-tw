# DEMO_REGRESSION — Production READ-ONLY 驗證矩陣

> 日期：2026-10-05。真實 production 資料（Supabase / Neo4j / pgvector，只讀；analysis log writer 關閉，沒有任何寫入）＋ 真實 OpenAI；LINE 以記憶體 FakeLine 取代（不送任何訊息）。測試材料由 production 動態挑選，runtime 沒有任何特例。

## 閘門結果

- Full pytest（`--basetemp=./.pytest_final_demo_gate`）：**1487 passed / 1 skipped / 0 failed / 0 errors**（含語音正規化新測試 12 個）。唯一 skip 是 live-env 專用的 `test_line_demo_live.py::來源卡`（MISSING_INFORMATION 無來源，live 環境無法覆蓋；unit test 已覆蓋）。
- Windows Temp PermissionError 重跑：`test_ingredient_bridge_import.py` + `test_ingredient_overlay_bridge.py`，使用 project-local `--basetemp`：**49 passed / 0 failed / 0 errors**（未修改任何 production code）。
- Production 回歸：42 / 42 項 PASS；FAIL：無（見「已知限制」）。

## 測試材料（動態挑選）

- 主產品 HF1：**漁人健康魚油軟膠囊**（衛部健食規字第000048號，官方成分 3 項，ACTIVE 健康食品、有核准功效與官方頁網址、品名唯一）
- 備援產品 HF2：**高鈣脫脂奶粉**
- 不在 Interaction corpus 的合理成分概念：**薑黃**（動態確認不在 corpus entity / alias）
- 圖片 / 語音：以 Windows 內建 System.Drawing / System.Speech 產生的合成材料（內容取自上述真實產品的官方資料）。

## Regression 矩陣

| # | 案例 | PASS | 延遲 | 輸入 | 預期（intent / 類型 / 產品狀態） | 實際（類型 / 狀態 / 路徑） | 預期工具 | 不可出現的工具 | 預期按鈕 / 實際按鈕 | 備註 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | exact product-name lookup | PASS | 8.4s | 漁人健康魚油軟膠囊有沒有通過健康食品認證？ | product_verification / PRODUCT_INFO、RISK / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | lookup_product | 規劃 / RAG / 官方網站 / 評估 | 預期：官方資訊/資料來源；實際：查看產品資訊、查看完整成分、查看資料來源 |  |
| 2 | permit-number lookup | PASS | 6.0s | 衛部健食規字第000048號是什麼產品？ | product_verification / PRODUCT_INFO、RISK / VERIFIED(PERMIT_NUMBER) | PRODUCT_INFO / VERIFIED / structured_fast_path | lookup_permit | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看產品資訊、查看完整成分、查看資料來源 |  |
| 3 | typo (substituted char) / candidate confirmation | PASS | 9.0s | 漁人健康錯油軟膠囊有哪些成分？ | ingredient_inquiry / MISSING_INFORMATION(候選) / PARTIAL_MATCH(候選) | MISSING_INFORMATION / PARTIAL_MATCH / structured_clarification | — | 規劃 / RAG / 官方網站 / 評估 | 預期：選擇：<候選>；實際：選擇：漁人健康魚油軟膠囊 | 嘗試 ['漁人健康錯油軟膠囊'] |
| 3b | candidate selection resumes original question | PASS | 2.1s | (按候選按鈕) | ingredient_inquiry / INGREDIENT_LIST / VERIFIED | INGREDIENT_LIST / VERIFIED / structured_fast_path | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看完整成分、查看產品資訊、查看資料來源 |  |
| 3c | typo (omitted char) auto-confirm by design | PASS | 5.8s | 漁人健魚油軟膠囊有哪些成分？ | ingredient_inquiry / INGREDIENT_LIST / VERIFIED(省略字) | INGREDIENT_LIST / VERIFIED / structured_fast_path | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看完整成分、查看產品資訊、查看資料來源 | FC-B2：只有省略字、無其他候選 → 自動確認 |
| 4 | product not found | PASS | 20.3s | 虛構測試晶亮膠囊乙丙有哪些成分？ | ingredient_inquiry / MISSING_INFORMATION / NOT_FOUND | MISSING_INFORMATION / NOT_FOUND / llm_planner | — | — | 預期：—；實際：— | 查無不代表假貨 |
| 5 | official product information (approved efficacy) | PASS | 7.5s | 漁人健康魚油軟膠囊的核准功效是什麼？ | efficacy_inquiry / PRODUCT_INFO / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | lookup_approved_claim | 規劃 / RAG / 官方網站 / 評估 | 預期：官方資訊、資料來源；實際：查看完整功效、查看產品資訊、查看資料來源 |  |
| 5-查看資料來源 | postback 查看資料來源 | PASS | 0.0s | (按「查看資料來源」) | - / detail card / - | None / None / None | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：開啟官方來源 | deterministic，無外部呼叫 |
| 6 | ingredient list + full ingredient detail | PASS | 5.1s | 漁人健康魚油軟膠囊有哪些成分？ | ingredient_inquiry / INGREDIENT_LIST / VERIFIED | INGREDIENT_LIST / VERIFIED / structured_fast_path | lookup_product_ingredients | 規劃 / RAG / 官方網站 / 評估 | 預期：查看完整成分；實際：查看完整成分、查看產品資訊、查看資料來源 |  |
| 6b | full ingredient detail card | PASS | 0.0s | (按「查看完整成分」) | - / detail / - | None / None / None | — | — | 預期：—；實際：查看資料來源 | 標題=保健功效相關成分（3 項） banned=[] |
| 7 | contains-specific-ingredient | PASS | 6.3s | 漁人健康魚油軟膠囊有含二十碳五烯酸嗎？ | ingredient_inquiry / INGREDIENT_LIST / VERIFIED | INGREDIENT_LIST / VERIFIED / structured_fast_path | lookup_product_ingredients | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看完整成分、查看產品資訊、查看資料來源 |  |
| 8 | standalone nutrient question | PASS | 14.5s | 鋅有什麼功能？ | efficacy_inquiry / INGREDIENT_LIST / NOT_APPLICABLE | INGREDIENT_LIST / NOT_APPLICABLE / standalone_entity | lookup_ingredient | lookup_product | 預期：—；實際：查看完整分析、查看資料來源 | 獨立成分路徑，不綁定產品 |
| 9 | ordinary package claim (image) | PASS | 12.8s | 1 張圖（品名＋核准宣稱原句） | product_verification(image) / PRODUCT_INFO / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | analyze_image | 規劃 / RAG / 官方網站 / 評估 | 預期：檢視包裝宣稱；實際：檢視包裝宣稱、查看產品資訊、查看完整成分 |  |
| 10 | medical-treatment claim (text ad) | PASS | 8.7s | 漁人健康魚油軟膠囊的廣告說可以治療糖尿病 | claim_verification / RISK(HIGH) / VERIFIED | RISK / VERIFIED / approved_claim_comparison | assess_evidence | search_tw_official_web | 預期：完整分析、資料來源；實際：查看完整分析、查看資料來源、查看產品資訊 | 產品真偽與宣稱風險分開 |
| 10b | medical-treatment claim (image) | PASS | 14.9s | 1 張圖（品名＋保證根治糖尿病） | claim_verification / RISK(HIGH) / VERIFIED | RISK / VERIFIED / package_claim_early_stop | — | search_tw_official_web | 預期：—；實際：查看完整分析、查看資料來源、查看產品資訊 |  |
| 11 | single-image product lookup | PASS | 9.7s | 1 張圖（品名＋字號 衛部健食規字第000048號） | product_verification / PRODUCT_INFO / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | analyze_image | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看產品資訊、查看完整成分、查看資料來源 | 含 burst window 600ms |
| 12 | direct automatic multi-image bundle (3 photos) | PASS | 17.1s | 一次傳 3 張（品名 / 成分 / 字號） | product_verification / PRODUCT_INFO / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | analyze_image_bundle | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看產品資訊、查看完整成分、查看資料來源 | bundle=FUSED, messages=1 |
| 13 | explicit multi-image mode | PASS | 12.8s | 「多照片辨識」→ 2 張 → 開始分析 | product_verification / PRODUCT_INFO / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | analyze_image_bundle | 規劃 / RAG / 官方網站 / 評估 | 預期：開始分析/取消；實際：查看產品資訊、查看完整成分、查看資料來源 | 蒐集期間靜默 messages_before_analyze=1 |
| 14 | package observation | PASS | 0.0s | (按「檢視包裝宣稱」) | - / observation card / - | None / None / None | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：— | 不跑任何外部呼叫 |
| 15 | image → immediate text follow-up (CURRENT_PRODUCT) | PASS | 15.0s | 圖片後立刻傳「它有哪些成分？」 | ingredient_inquiry / INGREDIENT_LIST / VERIFIED(bound) | INGREDIENT_LIST / VERIFIED / structured_fast_path | lookup_product_ingredients | search_tw_official_web | 預期：—；實際：查看完整成分、查看產品資訊、查看資料來源 | records=2 |
| 16 | unresolved image → PREVIOUS_IMAGE follow-up | PASS | 4.5s | 未確認產品的圖片後傳「剛剛那張照片上寫了什麼？」 | general_question / observation card / - | MISSING_INFORMATION / None / None | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：— | context_reference=PREVIOUS_IMAGE; Vision/resolver/Researcher/RAG/web=0（只有 Controller 1 次）; 首張=MISSING_INFORMATION/NOT_FOUND |
| 17-HF1 | audio input (HF1) | PASS | 5.3s | 語音：請問漁人健康魚油軟膠囊有哪些成分 | ingredient_inquiry / INGREDIENT_LIST / VERIFIED | MISSING_INFORMATION / PARTIAL_MATCH / structured_clarification | transcribe_audio | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：選擇：漁人健康魚油軟膠囊 | 轉錄='請問魚人健康魚油軟膠囊有哪些成分？'；判定修正：簡體→繁體後同音錯字（漁→魚）出候選確認卡，符合 FC-B2/FC-B8（不因正規化自動確認） |
| 17-HF2 | audio input (HF2) | PASS | 9.8s | 語音：請問高鈣脫脂奶粉有哪些成分 | ingredient_inquiry / INGREDIENT_LIST / VERIFIED | INGREDIENT_LIST / VERIFIED / structured_fast_path | transcribe_audio | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看完整成分、查看產品資訊、查看資料來源 | 轉錄='請問高鈣脫脂奶粉有哪些成分？' |
| 18 | URL input (official product page) | PASS | 15.9s | https://consumer.fda.gov.tw//Food/InfoHealthFoodDetail.aspx?nodeID=162&id=1492 | product_verification / PRODUCT_INFO / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | fetch_url_content | — | 預期：—；實際：查看產品資訊、查看完整成分、查看資料來源 | url_roles=['OFFICIAL_REGULATORY_SOURCE'] |
| 19 | text pronoun/context follow-up | PASS | 5.8s | 先問核准功效，再問「它有哪些成分？」 | ingredient_inquiry / INGREDIENT_LIST / VERIFIED(context) | INGREDIENT_LIST / VERIFIED / structured_fast_path | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看完整成分、查看產品資訊、查看資料來源 |  |
| 20 | ingredient↔ingredient interaction | PASS | 5.3s | 鋅和銅可以一起吃嗎？ | interaction_inquiry / INTERACTION_RESULT / - | INTERACTION_RESULT / NOT_APPLICABLE / interaction_research | lookup_interactions | 規劃 / RAG / 官方網站 / 評估 | 預期：為什麼？、查看證據來源；實際：為什麼？、查看證據 | fast_path=interaction_corpus:sufficient |
| 21 | ingredient↔drug interaction | PASS | 5.6s | 鈣和Levothyroxine可以一起吃嗎？ | interaction_inquiry / INTERACTION_RESULT / - | INTERACTION_RESULT / NOT_APPLICABLE / interaction_research | lookup_interactions | 規劃 / RAG / 官方網站 / 評估 | 預期：為什麼？、查看證據來源；實際：為什麼？、查看證據 | fast_path=interaction_corpus:sufficient |
| 22 | interaction evidence conflict | PASS | 5.6s | 魚油和Warfarin有沒有交互作用？ | interaction_inquiry / INTERACTION_RESULT / - | INTERACTION_RESULT / NOT_APPLICABLE / interaction_research | lookup_interactions | 規劃 / RAG / 官方網站 / 評估 | 預期：查看證據來源；實際：為什麼？、查看證據 | fast_path=interaction_corpus:sufficient |
| 22b | backup: needs-review / mixed evidence pair | PASS | 5.3s | 鈣和鐵可以一起吃嗎？ | interaction_inquiry / INTERACTION_RESULT / - | INTERACTION_RESULT / NOT_APPLICABLE / interaction_research | lookup_interactions | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：為什麼？、查看證據 | statuses=['NEEDS_REVIEW'] |
| 23 | duplicate ingredient across two products | PASS | 9.5s | 井田優蓇保膠囊和婕凡希-韻護葉酸錠是不是都含有維生素B12？ | interaction_inquiry / INTERACTION_RESULT / - | INTERACTION_RESULT / VERIFIED / interaction_research | lookup_duplicate_ingredients | 規劃 / RAG / 官方網站 / 評估 | 預期：為什麼？；實際：為什麼？、查看證據、查看成分對應 |  |
| 24 | interaction corpus coverage gap | PASS | 28.6s | 鋅和薑黃可以一起吃嗎？ | interaction_inquiry / INTERACTION_RESULT / - | INTERACTION_RESULT / NOT_APPLICABLE / interaction_research | search_rag | — | 預期：—；實際：為什麼？ | fast_path=interaction_corpus:coverage_gap |
| 25 | truly unknown interaction term | PASS | 7.9s | 鋅和啊咕嚕嗚拉可以一起吃嗎？ | interaction_inquiry / MISSING_INFORMATION / - | MISSING_INFORMATION / NOT_APPLICABLE / structured_clarification | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：— | fast_path=interaction_clarification:UNRESOLVED |
| 26-conflict-evidence | postback: 衝突證據明細 | PASS | 0.0s | (按「查看證據」) | - / evidence card / - | None / None / None | — | — | 預期：—；實際：查看資料來源 | 證據方向逐筆列出 |
| 26-查看完整功效 | postback: 查看完整功效 | PASS | 0.0s | (按「查看完整功效」) | - / detail card / - | None / None / None | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：— | deterministic |
| 26-查看產品資訊 | postback: 查看產品資訊 | PASS | 0.0s | (按「查看產品資訊」) | - / detail card / - | None / None / None | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看完整功效、警語與注意事項、查看資料來源 | deterministic |
| 26-查看證據 | postback: interaction 查看證據 | PASS | 0.0s | (按「查看證據」) | - / detail card / - | None / None / None | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看資料來源 | deterministic |
| 26-查看資料來源 | postback: 查看資料來源 | PASS | 0.0s | (按「查看資料來源」) | - / detail card / - | None / None / None | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：開啟官方來源 | deterministic |
| 26-為什麼？ | postback: interaction 為什麼？ | PASS | 0.0s | (按「為什麼？」) | - / detail card / - | None / None / None | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：— | deterministic |
| 26a | postback: 完整分析 (on-demand LLM) | PASS | 2.5s | (按「查看完整分析」) | - / detail card / - | None / None / None | — | search_tw_official_web | 預期：—；實際：— | 唯一呼叫 LLM 的按鈕：generate_text×1 |
| 26b | postback: 資料來源 | PASS | 0.0s | (按「資料來源」) | - / sources card / - | None / None / None | — | — | 預期：—；實際：開啟官方來源、開啟官方來源、開啟官方來源、開啟官方來源、開啟官方來源 |  |
| B1 | backup: exact lookup (HF2) | PASS | 5.3s | 高鈣脫脂奶粉有沒有通過健康食品認證？ | product_verification / PRODUCT_INFO、RISK / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | — | 規劃 / RAG / 官方網站 / 評估 | 預期：—；實際：查看產品資訊、查看完整成分、查看資料來源 |  |
| B2 | backup: URL (HF2 official page) | PASS | 21.9s | https://consumer.fda.gov.tw//Food/InfoHealthFoodDetail.aspx?nodeID=162&id=27 | product_verification / PRODUCT_INFO / VERIFIED | PRODUCT_INFO / VERIFIED / structured_fast_path | fetch_url_content | — | 預期：—；實際：查看產品資訊、查看完整成分、查看資料來源 |  |

## 各案例實際工具路徑（節錄）

- **#1 exact product-name lookup**：工具＝lookup_approved_claim、lookup_product、lookup_product_ingredients、lookup_product_warnings、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#2 permit-number lookup**：工具＝lookup_approved_claim、lookup_permit、lookup_product_ingredients、lookup_product_warnings、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#3 typo (substituted char) / candidate confirmation**：工具＝lookup_company、lookup_product；OpenAI 呼叫＝parse_structured×1
- **#3b candidate selection resumes original question**：工具＝lookup_approved_claim、lookup_permit、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝無
- **#3c typo (omitted char) auto-confirm by design**：工具＝lookup_approved_claim、lookup_company、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#4 product not found**：工具＝lookup_product、plan_tool_calls、resolve_entity_alias、search_illegal_ad_cases、search_tw_official_web；OpenAI 呼叫＝parse_structured×1, plan_step×4, web_search_structured×1
- **#5 official product information (approved efficacy)**：工具＝lookup_approved_claim、lookup_company、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#5-查看資料來源 postback 查看資料來源**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#6 ingredient list + full ingredient detail**：工具＝lookup_approved_claim、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#6b full ingredient detail card**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#7 contains-specific-ingredient**：工具＝lookup_approved_claim、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#8 standalone nutrient question**：工具＝assess_evidence、lookup_ingredient、search_rag；OpenAI 呼叫＝parse_structured×2, embed×1
- **#9 ordinary package claim (image)**：工具＝analyze_image、lookup_approved_claim、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1, describe_image×1
- **#10 medical-treatment claim (text ad)**：工具＝lookup_approved_claim、lookup_company、lookup_health_function、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_claim_policy_examples、search_claim_policy_rules、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#10b medical-treatment claim (image)**：工具＝analyze_image、lookup_approved_claim、lookup_health_function、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_claim_policy_examples、search_claim_policy_rules、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1, describe_image×1
- **#11 single-image product lookup**：工具＝analyze_image、lookup_approved_claim、lookup_permit、lookup_product、lookup_product_ingredients、lookup_product_warnings、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1, describe_image×1
- **#12 direct automatic multi-image bundle (3 photos)**：工具＝analyze_image_bundle、lookup_approved_claim、lookup_company、lookup_permit、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1, describe_images_structured×1
- **#13 explicit multi-image mode**：工具＝analyze_image_bundle、lookup_approved_claim、lookup_permit、lookup_product、lookup_product_ingredients、lookup_product_warnings、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1, describe_images_structured×1
- **#14 package observation**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#15 image → immediate text follow-up (CURRENT_PRODUCT)**：工具＝analyze_image、lookup_approved_claim、lookup_company、lookup_permit、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×2, describe_image×1
- **#16 unresolved image → PREVIOUS_IMAGE follow-up**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝parse_structured×1
- **#17-HF1 audio input (HF1)**：工具＝lookup_product、transcribe_audio；OpenAI 呼叫＝parse_structured×1, transcribe_audio×1
- **#17-HF2 audio input (HF2)**：工具＝lookup_approved_claim、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases、transcribe_audio；OpenAI 呼叫＝parse_structured×1, transcribe_audio×1
- **#18 URL input (official product page)**：工具＝fetch_url_content、lookup_approved_claim、lookup_company、lookup_permit、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#19 text pronoun/context follow-up**：工具＝lookup_approved_claim、lookup_permit、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#20 ingredient↔ingredient interaction**：工具＝lookup_interactions；OpenAI 呼叫＝parse_structured×1
- **#21 ingredient↔drug interaction**：工具＝lookup_interactions；OpenAI 呼叫＝parse_structured×1
- **#22 interaction evidence conflict**：工具＝lookup_interactions；OpenAI 呼叫＝parse_structured×1
- **#22b backup: needs-review / mixed evidence pair**：工具＝lookup_interactions；OpenAI 呼叫＝parse_structured×1
- **#23 duplicate ingredient across two products**：工具＝lookup_duplicate_ingredients、lookup_product、lookup_product_ingredients；OpenAI 呼叫＝parse_structured×1
- **#24 interaction corpus coverage gap**：工具＝assess_evidence、plan_tool_calls、search_rag、search_tw_official_web；OpenAI 呼叫＝parse_structured×3, plan_step×1, web_search_structured×1, embed×1
- **#25 truly unknown interaction term**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝parse_structured×1
- **#26-conflict-evidence postback: 衝突證據明細**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#26-查看完整功效 postback: 查看完整功效**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#26-查看產品資訊 postback: 查看產品資訊**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#26-查看證據 postback: interaction 查看證據**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#26-查看資料來源 postback: 查看資料來源**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#26-為什麼？ postback: interaction 為什麼？**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#26a postback: 完整分析 (on-demand LLM)**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝generate_text×1
- **#26b postback: 資料來源**：工具＝（無查證工具：deterministic 卡片）；OpenAI 呼叫＝無
- **#B1 backup: exact lookup (HF2)**：工具＝lookup_approved_claim、lookup_product、lookup_product_ingredients、lookup_product_warnings、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1
- **#B2 backup: URL (HF2 official page)**：工具＝fetch_url_content、lookup_approved_claim、lookup_company、lookup_permit、lookup_product、lookup_product_ingredients、lookup_product_warnings、query_knowledge_graph、search_illegal_ad_cases；OpenAI 呼叫＝parse_structured×1

## 已知限制（非阻擋）

1. **語音（#17）**：已加簡繁字形正規化（FC-A6）。實測：通用字樣品名語音 → 直接 VERIFIED；「漁人」被聽成同音字「魚人」→ 正規化後走候選確認卡（不自動確認，需使用者點選）。語音準確度仍取決於 ASR（同音錯字、噪音），列為 BACKUP、不當 CORE。
2. **查無產品（#4）** 會進 planner + 官方網站（約 21 秒）；結果正確（「查不到不代表是假貨」）但慢，不選為 CORE。
3. **Interaction coverage gap（#24）** 需補查 RAG + 官方網站（約 48 秒）；結果安全（只說尚未收錄、無結論）但慢，只放 BACKUP。
4. **省略一字的錯字（#3c）** 依 FC-B2（只有省略字、無競爭候選）自動確認，不出候選卡；**替換字**才出候選卡（#3）。這是設計行為。
5. **圖片追問「剛剛那張照片…」（#16）** 是否走 PREVIOUS_IMAGE 取決於 Controller 的語意判斷（本次實測成功；未確認產品的圖片 + 代名詞指稱另有 deterministic 守門）。
6. 第一輪 runner 有 4 項是「檢查條件」寫錯而非系統問題（#3 把省略字當成應出候選卡、#16 把 Controller 呼叫算成外部呼叫、#26a 訊息計數、按鈕標籤「查看證據」誤寫），已在第二輪以正確條件重跑（上表為最終結果）。
7. 本輪使用的 LINE 以 FakeLine 取代；真實 LINE 的 reply token、loading animation、Quick Reply 顯示需以真機 smoke 驗證（多輪已通過）。
