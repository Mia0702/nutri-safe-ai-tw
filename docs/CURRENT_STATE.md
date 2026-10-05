# CURRENT_STATE — nutri_safe_ai（呷安心）

> 跨 Claude session 的最新狀態摘要。只記目前有效狀態，不記歷史流水帳。
> 規則以 `CLAUDE.md` 為準。

## 專案

- Project: nutri_safe_ai（呷安心）
- 範圍：Taiwan Phase 1 only（僅 TW 資料與功能）

## 資料歸屬

- Supabase = canonical source of truth（官方結構化資料、raw records、logs）
- Neo4j = graph relationships（知識關聯與 graph traversal，非第二套主資料庫）
- RAG = Supabase PostgreSQL + pgvector

## 核心角色

- Agent A（Multimodal Intake）：Input Tools（analyze_image / transcribe_audio / fetch_url_content）把非文字輸入轉成文字，
  再走同一段語意理解 → NormalizedUserRequest。文字輸入不需要 Tool。
- Agent B（Evidence / Verification）：只用 Evidence Tools（lookup_* / search_illegal_ad_cases / query_knowledge_graph /
  search_rag / search_tw_official_web，`app/agents/evidence_tools.py`）蒐集證據 → EvidenceBundle。
  Supabase / Neo4j / pgvector / 官方網站是 Data Services / Sources，不是 Agent。
- Risk Engine：deterministic 規則，合成 Product Verification / Claim / Evidence Strength 為 LOW / MEDIUM / HIGH。
- Agent C（Reporter）：只根據 EvidenceBundle 與 RiskAssessment 輸出結構化 ResponsePayload（不輸出 Flex），不重新搜尋。
- AdaptiveLineRenderer：ResponsePayload / DetailPayload → 一則 LINE Flex Message。

## 目前狀態

- 新 Supabase / Neo4j 已連線；migration 0002 已確認套用（`app_schema_meta.migration_0002_brand_relationship_semantics = applied`）。
- **2026-10-03 正式 `--all` seed 已由使用者手動執行；`check_data_integrity --expect-products 565` Gate PASS（blockers 0）**：
  Product 565 / Company 245 / Permit 565 / Ingredient 272 / Claim 14 / Warning 975 / Brand 26，Supabase ↔ Neo4j canonical ID 一致；
  RAG 1 doc / 9 chunks。DATA FREEZE + Feature Freeze 中。
- **Real 565-data E2E Verification Gate（2026-10-03）**：真實 webhook 簽章 → queue → pipeline（OpenAI / Supabase / Neo4j / RAG）
  → LINE validate API。產品由 DB 動態挑選（seed 隨機，ACTIVE + 有核准功效 + 有成分 + 有 source_url），不 hard-code。
  驗證腳本只在 session scratchpad，未進 repo；未修改 production code。
  - 產品查證 / 成分 / 核准功效 / 來源 / 查無產品 / 缺產品指稱：全部查到正式資料或正確顯示資料不足；查無產品只說
    「不代表是假貨或違法」。response_type 依 `select_response_type` 設計：產品查證 → RISK 卡（2026-10-04 起：沒有命題且無風險發現時改為 PRODUCT_INFO）、已確認產品的功效問題 →
    PRODUCT_INFO（含核准功效）、SOURCE_SUMMARY 只出現在不指定產品的一般問題。
  - Claim role：使用者問「可以治療糖尿病嗎」→ USER_QUESTION / CLAIM_RESULT（當時為 CONFLICT_WITH_OFFICIAL、無風險燈；
    2026-10-04 起命題判為 MEDICAL_TREATMENT_CLAIM、claim_risk HIGH 🔴，產品風險不變，見 Claim verdict consistency）；
    廣告寫「可以治療糖尿病」→ ADVERTISEMENT_CLAIM / MEDICAL_TREATMENT_CLAIM / RISK HIGH。
  - Multimodal：text / image（analyze_image）/ audio（transcribe_audio，品名語音誤聽仍正確對到產品）/ URL（fetch_url_content）皆 VERIFIED。
  - Context / Queue：「那它…」RESOLVED_FROM_CONTEXT；開始新問題後 context 清除；FIFO A 先回；舊卡 sources 指向舊 request_id。
  - 延遲：多數 12–48 秒；>60 秒只有 URL（貼產品官方頁）112 秒（Agent A 27s、Agent B 84s，含 2 次 assess_evidence 與
    official web fallback）。official web fallback 只在查無產品與 URL 案例被呼叫。
  - ~~已知問題：貼產品自己的 TFDA 官方頁被判「流傳說法」MEDIUM~~ → 已由 URL Verification Stabilization 修正（見下方）。
- Data closeout（A1 / A2）已完成：
  - A1：「保健功效」空白但「保健功效宣稱」有原文 → 掛到固定的未分類 approved_claim
    （`claim_kind = HEALTH_FOOD_CLAIM_UNCLASSIFIED`，function_name「未分類（官方資料未列功效類別）」，不推測類別），
    product_claims 仍是 APPROVED_OFFICIAL；`ApprovedClaimEvidence.function_classified = False`，Agent B / Risk 照常當官方核准證據。
    無 schema 變更（`claim_kind` 欄位 0001 已有）。565 筆中影響 8 筆（預期 approved_claims 14、product_claims 652）。
  - A2：`graph_sync` 的 Supabase `in_` 查詢按 ID 分批（預設 100）、Neo4j 寫入分批（預設 500），
    seed 可用 `--graph-id-batch-size` / `--graph-write-batch-size` 調整；批次失敗拋 `GraphSyncBatchError`（stage / 第幾批）。
    全部 MERGE，重跑不重複。tests：`tests/unit/test_data_closeout.py`。
  - `scripts/reset_dev_seed.py`：需 `--confirm-project nutri_safe_ai` + 兩邊 marker 皆正確 + Neo4j 無外來 label 才清除；有 `--dry-run`。
  - `scripts/check_data_integrity.py`：唯讀 Real DB Integrity Gate（canonical ID 對齊、node / relationship counts、
    duplicate、orphan、inferred brand、raw traceability、RAG），有 blocker 時 exit 1。
- Company normalization 已完成（`app/ingestion/company_name.py`）：
  - 只移除尾端、有強證據（日期＋行政關鍵詞，或 ≥2 關鍵詞）的行政附註。
  - 正式名稱括號（如「(台灣)」）保留；無法可靠判斷時保留原文。
  - 565 筆 dry-run：修正 1 家公司（2 筆紀錄），unique company 245 → 245，無法判斷 0。
- Ingredient parsing quality gate 已完成（`app/ingestion/ingredient_parser.py`）：
  - 狀態：PARSED / PARTIALLY_PARSED / UNPARSED / NO_SOURCE_INGREDIENT（存於 bundle，未改 DB schema）。
  - 565 筆 dry-run：PARSED 558、PARTIAL 2、UNPARSED 5、NO_SOURCE 0；名稱驗證後不再建立含量／份量／規格類假成分。
- Ingredient canonicalization / alias quality gate 已完成（`app/ingestion/ingredient_canonical.py`）：
  - 自動合併只限格式差異（大小寫／空白／標點／希臘字母拼寫）與同欄位「中文English」相連。
  - 跨紀錄中英配對、異體字（醣/糖、蔘/參）、類別 vs 成員（HMG-CoA 抑制劑 vs Monacolin K）只列 candidate，不合併。
  - 565 筆 dry-run：unique 281 → canonical 272；alias 63（OFFICIAL_RECORD）；inferred candidate 6；review candidate 49。
  - candidate 目前只在 dry-run 回報，尚未有人工審核流程（MANUAL_REVIEWED 介面已預留）。
  - seed script 改為先以完整資料集決定 canonical 名稱，再取 `--limit` 子集。
- Brand inference quality gate 已完成（`app/ingestion/alias_builder.py`）：
  - 候選分 ACCEPTED / REVIEW_CANDIDATE / REJECTED；只有 ACCEPTED（或人工 MANUAL_REVIEWED）寫入 brands / product_brands / BRAND alias。
  - 規則：以「不同品名」計數（同名重複紀錄不算）、前綴本身或尾段為跨公司一般描述詞 → REJECTED；
    品牌邊界不明、被多家公司共用、confidence < 0.65 → REVIEW_CANDIDATE。無品牌黑／白名單。
  - TFDA 無品牌欄位 → 推導品牌一律 INFERRED（`official_brand_field: false`），不會是 OFFICIAL_RECORD。
  - 一家公司 0..N 個品牌：互為前綴的候選（X、XY）視為同一品牌的邊界，取最佳者；不同前綴各自通過 gate，
    可同時採用（gate 門檻未放寬）。「每家公司最多 1 個品牌」不是 domain rule。
  - 565 筆 dry-run：ACCEPTED 26 / REVIEW 18 / REJECTED 15；brands 26（全 INFERRED）、product_brands 115；
    1 家公司有 2 個採用品牌（產品集合不重疊）；沒有任何採用品牌連到多家公司。
  - Company → Brand 關係（`brands.company_relation`，Neo4j 同名關係）：
    `OWNS_BRAND` 只限 OFFICIAL_RECORD / MANUAL_REVIEWED 且有 `ownership_evidence`（DB check constraint）；
    其他一律 `ASSOCIATED_WITH_BRAND`。目前 26 個品牌全為 ASSOCIATED_WITH_BRAND。
    Product → Brand（`HAS_BRAND` / product_brands）保留 evidence_status / confidence / inference_basis。
  - Reporter：推測關聯若被寫成「官方確認」或「擁有／旗下／品牌屬於」→ 改用固定模板。
  - review / rejected 候選只存在 `AliasBuild.brand_candidates`（dry-run 回報），尚無 DB 表與人工審核介面。
- 565 筆 Full Data Dry-run Gate 已完成（只分析，未寫 Supabase / Neo4j）：
  - 方法：production normalize / canonical / alias builder → production `load_health_food` / `load_aliases`
    寫入本機暫存 PostgreSQL（0001+0002）驗證 FK / CHECK：0 errors。分析腳本只在 session scratchpad，未進 repo。
  - 預期 rows：raw 565、companies 245、permits 565、products 565、ingredients 272、product_ingredients 770、
    approved_claims 13、product_claims 644、warnings 975、product_warnings 2571、brands 26、product_brands 115、
    entity_aliases 333（COMPANY 244 / INGREDIENT 63 / BRAND 26 / PRODUCT 0）。
  - 預期 Neo4j：Source 1、Product 565、Company 245、Permit 565、Ingredient 272、Claim 13、Warning 975、Brand 26；
    SUPPORTED_BY 565+13、OWNS_OR_APPLIES_FOR 565、REGISTERED_AS 565、CONTAINS 770、HAS_APPROVED_CLAIM 644、
    HAS_WARNING 2571、HAS_BRAND 115、ASSOCIATED_WITH_BRAND 26、OWNS_BRAND 0。
  - RAG：1 document → 9 chunks（預估 9 次 embedding，未呼叫）。
  - Integrity：dropped 0、duplicate permit / canonical ID / 跨表 UUID collision 0、FK orphan 0、
    INFERRED 誤標 official 0、7 筆未完整解析成分仍保留 raw source。
  - 已知 warning（未修改，待決定）：
    1. ~~8 筆「保健功效」空白但有「保健功效宣稱」~~ → 已改為未分類官方宣稱（見上方 A1）。
    2. 2 筆無意義 warning（「無。」「警語:」）。
    3. 別名「大醫生技」同時指向 2 家不同公司（有限公司 / 股份有限公司），屬正確的 ambiguous。
    4. 5 筆無 source_url、1 筆核可日期空白（官方原始資料如此）。
    5. ~~`graph_sync` 單一 URL 帶 565 個 ID~~ → 已改為分批（見上方 A2）。
- Multi-Agent Tool Responsibility + Adaptive LINE Response 架構已定稿（contract / 介面 / tests；尚無 LINE webhook）：
  - Input Tools：`app/tools/input_tools.py`（fetch_url_content 有 SSRF 防護：只允許 http(s) + 公開 IP，每次 redirect 重新檢查）。
    `RawUserInput.media / urls`；`NormalizedUserRequest.input_tools_called`。debug API 接受 text / url。
  - Primary response：每個 request 只有一則 Flex；Agent C `compose()` 為 deterministic（不呼叫 LLM）。
    response_type：RISK / PRODUCT_INFO / INGREDIENT_LIST / CLAIM_RESULT / SOURCE_SUMMARY / MISSING_INFORMATION / OUT_OF_SCOPE / ERROR；
    只有 RISK 有 risk_level（🟢 / 🟠 / 🔴）；HIGH 或包裝 / 廣告宣稱一律 RISK。
  - On-demand：detail / sources / ingredients / official_info / why / retry；只有「查看完整分析」呼叫 LLM（既有 report() 與推測關聯 guard）。
  - Request-scoped：postback `action=<a>&request_id=<uuid>`；依 request_id + owner 取回該題（`app/response/store.py`，in-memory 24h）。
  - 逾時：`AdaptiveResponseService` 立即回 ERROR card（重新查詢）；晚到結果丟棄、不補發、不覆蓋紀錄。
  - `AnalysisResult.response` 為 primary payload；`final_answer` 只是其純文字版（debug / log）。
- **LINE 最終整合（2026-10-03）**：`POST /line/webhook`（`app/api/line_webhook.py`、`app/line/`）。
  - 簽章驗證（HMAC-SHA256）→ 解析事件 → per-user FIFO queue → 立即回 200；webhookEventId 去重（LINE 重送不重答）。
  - Text → Agent A；文字中的網址 → `RawUserInput.urls`（URL / MIXED）；Image / Audio → 下載 LINE content → `RawUserInput.media`。
    全部走同一條 `AdaptiveResponseService.respond()`（Agent A → B → Risk → C → ResponsePayload → AdaptiveLineRenderer）。
  - 每個 request 只送一則訊息；Quick Reply「開始新問題 / 結束本次查證」（postback `session=new|end`）附在訊息上，不在 Flex 卡片內。
  - 送出：reply token 只跟著該 job，< 50 秒才用 reply；過期或 reply 回 400 → push（`X-Line-Retry-Key = request_id`）。
    處理中只顯示 loading animation，不送「查詢中」文字。
  - Queue（`app/line/request_queue.py`）：同一 user 同時只有一個 PROCESSING，其餘 QUEUED 依 FIFO；不同 user 平行；單一 job 失敗不卡後續。
  - 任務型短期上下文（`TaskContextStore`，`app/services/context_store.py`）：per user，current_product_id / name、
    recent_confirmed_entities、last_intent、recent_request_ids；最近 5 題、TTL 30 分鐘、可 reset / close。
    只有準時完成且 session 未被重設的結果才寫回 context（逾時晚到結果、「開始新問題」之前的結果都丟棄）。
    無法可靠解析指稱 → MISSING_INFORMATION（要求補產品資訊，不猜）。debug API 仍用 InMemoryConversationStore。
  - 逾時：`RESPONSE_TIMEOUT_SECONDS`（預設 120 秒；live pipeline 實測約 25–75 秒）→ 一張 ERROR card，晚到結果不補發、不覆蓋、不寫 context。
    官方網站 fallback（`search_tw_official_web`）單次上限 `OPENAI_WEB_SEARCH_TIMEOUT_SECONDS` 預設 40 秒且不重試
    （原 120 秒 × 重試 1 次，最壞 ~240 秒，會讓宣稱類問題（如語音 E2E）整題逾時）；逾時只讓該 tool 記錄 TIMEOUT，回覆照常產生。
  - Agent C 按鈕優先序調整：RISK = 完整分析 / 資料來源 / 官方資訊 / 為什麼；CLAIM_RESULT = 完整分析 / 資料來源 / 為什麼（卡片最多 3 個）。
  - Request result storage：沿用 `InMemoryRequestStore`（24h）；analysis log 照舊寫 Supabase `analysis_requests`。
  - tests：`tests/unit/test_line_webhook.py`（fake，涵蓋 E2E 1–10）；demo smoke：`tests/integration/test_line_demo_live.py`
    （真實簽章 webhook + 真實 pipeline，訊息送 LINE `validate/reply` API 驗證；合成圖片 / 語音由 Windows 內建產生）。
- **Real Phone Demo Stabilization（2026-10-03，P0；DATA FREEZE 未動）**：
  - 核准功效詢問：Agent A 一般化規則——efficacy_inquiry 中沒有疾病 / 生理功能目標的 USER_QUESTION（「核准功效是什麼？」）
    不是可查證命題，丟棄（`_is_open_efficacy_question`，prompt 同步說明）；回 PRODUCT_INFO 並逐條列出官方核准功效。
    「可以治療糖尿病嗎」仍為 USER_QUESTION / CLAIM_RESULT（已確認產品時附上官方核准功效）；廣告宣稱仍為 RISK HIGH。
  - Agent B structured fast path（`planner_mode = structured_fast_path`）：intent ∈ 產品查證 / 成分 / 功效、無任何命題、
    有產品識別、非外國產品 → 先 deterministic 查結構化資料；產品 VERIFIED + 所問的資料存在（功效→核准功效、成分→成分）
    + 無工具失敗才停止（違規廣告紀錄照查），不呼叫 planner / RAG / assess_evidence / 官方網站；否則回到完整流程。
    安全性問題、claim_verification、任何命題、NOT_FOUND / PARTIAL / CONFLICT 一律完整流程。
  - LINE 可靠性：content API 回 202（語音轉檔中）改輪詢 transcoding 狀態，下載總時限 20 秒（`CONTENT_DEADLINE_SECONDS`），
    空內容 / httpx 錯誤 / 逾時一律視為下載失敗 → 一張 ERROR card（無重新查詢）。語音轉文字單次 30 秒、不重試
    （`OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS`）。URL：整體 20 秒上限（含 redirect / 慢速傳輸）、DNS 5 秒上限。
  - Queue：job 在獨立 thread 執行，watchdog（下載 20 + `RESPONSE_TIMEOUT_SECONDS` + 30 秒）逾時送一張逾時卡；
    finally 必定釋放（含 BaseException）；handler 任何例外都回一張 ERROR card；每個 request_id 只送一次（晚到結果丟棄）。
  - Stage log（`line_stage`，帶 request_id）：request_received / queue_started / media_download_* / transcription_* /
    url_fetch_* / pipeline_started / pipeline_done|timeout / delivery / late_result_discarded / job_timeout / queue_released。
  - Live（真實 OpenAI / Supabase / Neo4j / RAG，LINE validate API，產品由 DB 動態挑選）：功效 9.7s、成分 7.1s、登記 5.3s、
    語音功效 8.7s（皆 fast path、reply）；URL 97s（完整流程 + official web，push 送達）。原本 12–48 秒、URL 112 秒。
  - 仍存在：URL / 廣告宣稱類 > 50 秒走 push、> 60 秒 loading 動畫消失（使用者看起來像沒回應）；TFDA 官方頁 URL 語意問題未修。
  - tests：`tests/unit/test_demo_stabilization.py`（28）；`test_agents.py` 中 planner 機制測試改用 safety_inquiry（不走 fast path）。
- **Real Phone Demo correctness（2026-10-03，P0-1/2/3；DATA FREEZE 未動，無 schema / seed / ingestion 變更）**：
  - Root cause：使用者寫的品名同時對到「同一基本品名 + 不同尾端括號規格」的多個官方品項（分數相同 < 0.9），
    舊規則只看分數 → 靜默 PARTIAL_MATCH 並掉進 planner / official web；偶爾 VERIFIED 是因 planner 拿工具結果中的
    許可證字號再查一次，任意挑中一個品項（等於猜）。另：VERIFIED 後使用者原始寫法被另存成無 ID 的 context 實體，
    下一題「它」會解析回原始寫法而重新比對。
  - Deterministic product resolver（`resolve_product_match`，`app/risk/assessment_rules.py`）在任何 LLM planning 前執行：
    使用者提供的許可證字號 → 官方品名 / 官方產品別名（OFFICIAL_RECORD / MANUAL_REVIEWED）完全相同 → 基本品名相同
    （`base_product_name` 去尾端括號）→ 名稱相似度（與第一名差距 ≤ 0.05 的都算合理候選）。
    VERIFIED 或真正 ambiguous 的結果鎖定（`PlannerState.locked_product_match`），planner 不可改寫；
    planner 抄來的字號不能確認產品。`ProductMatch` 新增 match_type / match_confidence / ambiguity_reason / candidate_count。
  - Ambiguous + 資訊類問題 → `planner_mode = structured_clarification`：不呼叫 planner / RAG / web / assess_evidence，
    回 MISSING_INFORMATION 列出所有官方候選（品名＋字號），請使用者回覆完整品名或字號；不把任一品項當答案。
  - Fast path 不變但記錄 `fast_path_eligible` / `fast_path_reason`；log 白名單新增 resolver / fast path / parity 欄位（不含原文）。
  - Modality parity：語音轉錄＝使用者自己的話，送進 Agent A 語意理解的 payload 與直接打字逐字相同
    （無【語音內容】標示、input_type=text、轉錄附註只留在 request.adapter_notes）；圖片 / 網頁仍保留來源標示。
    intake log：semantic_input_type / parity_path_used / transcription_completed / claim_roles。
  - Claim-role：開放式功效詢問即使 LLM 把「功效 / 核准功效」等泛稱填進 target_function 也丟棄（`GENERIC_EFFICACY_TERMS`）。
  - Live（產品由 DB 隨機挑選、問句由程式組出、不寫 log）：功效 / 成分 / 登記 text 與 audio 皆 VERIFIED（CANONICAL_EXACT）
    + fast path、無 web / assess_evidence，5–8 秒；text ↔ audio intent / claim role 全部一致；疾病是非題 USER_QUESTION →
    CLAIM_RESULT（35–40s）；廣告宣稱 ADVERTISEMENT_CLAIM → MEDICAL_TREATMENT_CLAIM / HIGH（42s）。
    565 筆中只有 1 組同基本品名多品項：基本品名 → clarification 5.1s；完整品項名 → VERIFIED fast path 6.8s。
  - tests：`tests/unit/test_resolver_parity.py`（31）；更新 3 個舊測試（fixture 品名與分數不一致、planner 不再先查產品、語音標示）。
  - P1（未做）：URL / 廣告宣稱類完整流程延遲（35–100 秒）；clarification 後使用者只回「規格名稱」時能否接上前題未保證。
- **Real Phone Demo P1 stabilization（2026-10-03；DATA FREEZE 未動）**：
  - Fast-path root cause（語音 trace）：語音誤聽的品名在 deterministic 階段只到 0.73（唯一候選但未達 0.9）→ LOW_MATCH 未鎖定
    → 完整流程；最後的 VERIFIED / 1.0 是 planner 看過查詢結果後用完整官方品名再查得到的（循環佐證，不是獨立證據）。
  - Resolver：相似度層級一律以「使用者原始寫法」計分，planner 查詢名稱不能抬高分數或確認產品。
    Agent A 新增 `corrected_product_candidates`（錯字 / 同音修正，查 DB 前只依使用者輸入產生），deterministic lookup 一併查；
    修正寫法與官方品名相同、且原始寫法的合理候選中它是第一名、margin 內無競爭者 → VERIFIED（`CORRECTED_NAME_CORROBORATED`）。
    修正寫法不能在多品項中替使用者選一個。實測 LLM 不一定會給修正寫法（同一誤聽句 3/3 次未修正）。
  - 資訊類問題 + 只有低信心候選（`ProductMatch.needs_confirmation`）→ 同樣走 `structured_clarification`：
    不跑 planner / RAG / web / assess，回「最接近的官方紀錄是…」＋「都不是可能是查無紀錄（不代表假貨）」。
    候選選項標示字號，非有效許可證另標狀態（同名不同字號的紀錄可分辨）。
  - Approved claim 去重（`dedupe_approved_claims`，response 組裝層）：先依 canonical claim ID、再依正規化官方敘述合併；
    同敘述只顯示一次、功效類別合併列出；字面不同一律不合併；EvidenceBundle / sources / evidence_ids 不變。
    套用於 PRODUCT_INFO、CLAIM_RESULT、官方資訊明細、完整分析 LLM 輸入。DB 中 69 項產品有同敘述多類別的情形。
  - Live（DB 動態挑選、唯讀）：unique 產品功效 / 成分 / 登記 text+audio 全部 VERIFIED fast path 3.8–7.8s、無 planner/RAG/web/assess、
    claim 不重複；多品項基本品名 → clarification 5–10s；字號 → VERIFIED fast path；同名雙字號（一有效一失效）→ clarification。
    手機誤聽語音 transcript 重播：原 30–38s 完整流程 → 7.2s 確認卡。
  - tests：`tests/unit/test_p1_fastpath_dedup.py`（26）；修正 2 個 fixture 分數與品名不一致的舊測試。
- **多候選產品確認 → LINE 直接選擇（2026-10-03；resolver / 門檻 / Agent 架構 / DATA FREEZE 未動）**：
  - Agent C：確認卡（MULTIPLE_* 或低信心）的 `ResponsePayload.product_choices` 只取自該 request 的 ProductMatch.candidates；
    label = 去掉候選品名共同前綴後的規格 / 口味（一般性規則）；同名不同字號時改用許可證代碼；非有效許可證標
    「⚠️ 許可證已失效（仍可查詢）」。
  - Renderer：候選 ≤ 3 → 同一張卡片內每個候選（短名稱、完整品名、字號 / 狀態、「選擇：…」按鈕）；> 3 → 一則 carousel。
    仍是一則 primary message；session controls 只在 Quick Reply。
  - Postback：`action=select_product&request_id=<原題>&candidate_id=<canonical product id>`（其他 action 帶 candidate_id 一律拒絕）。
  - `AdaptiveResponseService.select_product`：request 必須屬於同一 LINE user、candidate 必須在那一題的 product_choices 與
    evidence 候選中，否則回提示不猜。通過後以 `request_for_selected_product`（原題 NormalizedUserRequest，產品識別換成所選
    官方品名＋許可證字號）呼叫 `AnalysisPipeline.resume_with_request`：不重跑 Agent A，走既有 resolver（PERMIT_NUMBER）→ fast path；
    結果必須確實是所選產品才回答；所選產品寫入 task context。舊卡片依其 request_id 的候選處理，不看最新對話狀態。
  - 限制：選擇後的結果卡按「重新查詢」會重跑原始問題（再次出現確認卡）。
  - Live（DB 兩組多品項，真實 pipeline，Flex 經 LINE validate API）：確認卡 7–9s → 點選 0.9–2.0s 取得原題結果
    （功效 → PRODUCT_INFO、成分 → INGREDIENT_LIST），VERIFIED / PERMIT_NUMBER / fast path、Agent A 未重跑、context 已設定；
    含失效字號品項。DB 沒有 > 3 品項的家族：carousel 以虛構候選通過 LINE validate API。
  - tests：`tests/unit/test_product_selection.py`（16）。
- **URL Verification Stabilization（2026-10-03；DATA FREEZE / resolver / 多候選 UX / text-audio fast path / Risk Engine 未動）**：
  - Root cause：(1) URL 內容一律標【網頁內容】，官方頁上的文字被 Agent A 抽成 OFFICIAL_CLAIM 命題 → Risk Engine 視為「流傳說法」，
    並拿官方資料驗證官方頁自己（EXCEEDS → 誇大）。(2) 商城頁標題整串（通路標示 / 賣點 / 包裝規格）被當產品名 → NOT_FOUND
    → 多輪 planner + 2 次 assess + official web。(3) web 只找回已知頁面也會重跑 assess。
  - URL source role（`app/tools/url_source.py`，只看網域 + 既有 `TW_OFFICIAL_DOMAINS`，以重新導向後的網址判斷）：
    OFFICIAL_REGULATORY_SOURCE / NON_OFFICIAL_EXTERNAL_SOURCE / UNKNOWN_EXTERNAL_SOURCE（IP、非 allowlist 的 gov 網域、無法解析）。
  - Agent A：`NormalizedUserRequest.url_sources`（source_url / requested_url / domain / role / title / retrieved_at / excerpt）；
    官方頁段落標【官方網頁內容】；`ExtractedClaim.content_source`（LLM 標記）＋ `source_role` / `source_url`（系統填，不在 LLM schema）。
    `apply_url_source_roles`：官方頁的命題 → `official_source_statements`（OFFICIAL_CLAIM，不進 claims、不查 web）；
    非官方 / UNKNOWN 頁的命題仍是外部宣稱，頁面自稱官方的 OFFICIAL_CLAIM → THIRD_PARTY_CLAIM；多網址角色不同取最保守。
    Prompt：網頁 / 圖片抽產品名只取品牌＋品名（去通路標示、促銷賣點、包裝規格）。
  - Agent B：官方頁本身成為 official evidence（`url:` id、provenance 完整；能對應已確認產品的字號 / 品名才是 PRODUCT_SPECIFIC，
    否則 GENERAL_BACKGROUND）。`url_evidence_early_stop`：來源非 UNKNOWN + 產品 VERIFIED + 無工具失敗時——
    官方頁且對應產品、無使用者命題 → 直接回；外部宣稱 + 有核准功效 / 警語 → deterministic 查證 + 一次 assess，
    只有 evidence conflict 或命題沒有內部證據可對照（被說出的醫療宣稱由規則決定，不算）才查 web。其餘回完整流程。
    assess 去重：已評估證據不重評、無新證據不重跑；web 結果是使用者已給的網址 / 已知證據不算新增。
  - Stage log：`fetch_ms / semantic_extraction_ms / product_resolution_ms / db_kg_ms / rag_ms / planner_ms / assessment_ms /
    official_web_ms / duration_ms`、`url_source_roles`、`assessment_count`（log 白名單已加）。
  - Live（唯讀、不寫 log；官方頁由 DB 隨機挑 ACTIVE＋有核准功效的產品）：官方頁 19–23s（fast path、6 句官方陳述未進 claims、LOW、
    無 planner / assess / web，原 112s MEDIUM 誤判）；非官方商城頁 44–68s（early-stop：planner 1 輪（僅 web 查詢）、web 1 次、
    assess 2 次（web 有新證據），VERIFIED CANONICAL_EXACT、ADVERTISEMENT_CLAIM → MEDIUM），修正前 63–98s（NOT_FOUND、planner 4–5 輪）。
    Agent A 語意抽取本身 13–33s（OpenAI 波動）是剩下的主要延遲。
  - tests：`tests/unit/test_url_source_role.py`（37）；targeted 10 檔 263 passed。
- **Demo UX stabilization（2026-10-03；Agent / Risk / Resolver / DATA 未動，只改 response / LINE 層）**：
  - Image provenance：`app/response/query_context.py`。圖片 request 的 `RequestRecord.query_label`：已確認產品或 Agent A 從圖片讀出的品名
    →「圖片辨識｜<名稱>」，否則「圖片查詢｜<MM/DD HH:MM:SS（台灣時間，LINE event timestamp）>」；`received_at` 一併保存。
    primary / service 逾時 / queue watchdog 逾時 / 下載失敗 / 所有 postback（detail、來源、選擇產品、重新查詢沿用原收到時間）
    都只用原 request 的 label，不看最新對話狀態。不存圖片、不建圖片 URL、無縮圖。
  - Source links：「查看資料來源」仍為 request-scoped postback（同 user + 同 request_id），來源卡改由 response 層依該題保存的
    evidence `all_sources()` 與使用者提供的 `url_sources` 產生（`app/response/sources.py`）；有合格網址才有 LINE `uri` 按鈕
    （官方「開啟官方來源」／外部「開啟原始頁面」），顯示標題、網域、官方 / 外部來源。`safe_source_url`：只收 http/https、有主機、
    無帳密、可見 ASCII、≤1000 字元，renderer 再驗一次；不合格只顯示文字。primary card 不放網址。
  - tests：`tests/unit/test_image_context_sources.py`（30）＋ LINE / response / selection 相關 98 passed；
    新 Flex（圖片結果 ×3、下載失敗、service 逾時、watchdog 逾時、來源卡 ×2、舊卡 detail）皆通過 LINE validate API。
- **Confirmed-product context binding ＋ Flex 字體無障礙（2026-10-03；DATA / resolver 門檻 / Risk Engine / Agent 架構未動）**：
  - Root cause：task context 只把產品「名稱」當前文實體交給 Agent A，是否沿用由 LLM 決定；前文問句仍含基本品名，
    LLM 把基本品名帶回 product_candidates → resolver 重新比對 → 再出現候選卡。canonical product_id / 字號從未傳到 Agent B。
  - `TaskContext.confirmed_product`（`ConfirmedProduct`：product_id / permit_id / permit_number / official_product_name /
    confirmation_source（USER_SELECTED | RESOLVER_VERIFIED）/ confirmed_at / confirmed_request_id）。select_product（文字 / 圖片 /
    同名字號候選共用同一路徑）→ USER_SELECTED；一般 VERIFIED 結果 → RESOLVER_VERIFIED；明確改問其他產品但未確認 → 清除。
  - `app/services/context_binding.py`：pipeline 在 Agent A 之後、Agent B 之前 deterministic 綁定——本次訊息內容（original_text）
    沒有出現新的品名 / 字號，且為代名詞指稱或沒有任何實體的產品 follow-up（產品 / 成分 / 功效 / 安全 / 說法查證）→
    product_candidates = 官方品名、permit_number_candidates = 字號（resolver 以 PERMIT_NUMBER 確認，不用基本品名）。
    只提到已確認產品官方品名中包含的名稱（例如基本品名）視為同一產品；其他明確品名 / 字號一律優先。一般成分問題不綁定。
  - 清除：開始新問題 / 結束本次查證 / TTL 30 分鐘 → 沒有 confirmed product，「它有哪些成分？」回 MISSING_INFORMATION。
  - Flex：`TEXT_SIZES`（section sm / title xl / body md / note sm）＋ `BUTTON_HEIGHT = md`；所有 text `wrap` + `scaling`，
    所有 button `scaling` + `adjustMode: shrink-to-fit`（label 仍維持 ≤ 20 字）；目前沒有使用 icon。卡片內容與結構不變。
  - tests：`tests/unit/test_context_binding_fonts.py`（19）＋ 相關 targeted 共 179 passed；17 種 Flex 皆通過 LINE validate API。
- **Image-only routing ＋ image latency（2026-10-03；DATA / resolver 門檻 / Text-Audio / URL / session context / 候選選擇 /
  Risk Engine / Flex typography / Agent 架構未動）**：
  - Root cause：Vision 讀出的包裝宣稱全部成為 PRODUCT_LABEL_CLAIM 命題（LLM 常把只傳圖片判成 claim_verification）
    → 不走 fast path → 多輪 planner + RAG + assess + 官方網站（60–120 秒）；查無產品時也用 planner / web 硬找。
  - Agent A `apply_image_routing`（deterministic）：圖片中的宣稱預設存為 `observed_package_claims`，不自動查證。
    例外 A：使用者自己的文字明確要求查驗（有使用者文字且 intent = claim_verification）；例外 B：被宣稱的醫療效能
    （`is_asserted_medical_claim`，與 assess_claim 判 MEDICAL_TREATMENT_CLAIM 的條件相同）→ 只有這些留在 claims，
    `claims_from_package = True`。只傳圖片且無高風險宣稱 → intent = product_verification、`image_identification = True`。
  - Agent B：VERIFIED → 既有 structured fast path；多候選 / 低信心 → 既有確認卡；其餘（查無、無法辨識、查詢失敗）→
    `image_identification_stop`（違規廣告照查，不跑 planner / RAG / web）。包裝宣稱查驗：產品 VERIFIED + 有核准功效 / 警語 →
    `package_claim_early_stop`（一次 assess；只有衝突或宣稱無內部證據才查 web 並最多再評估一次）；否則完整流程。
  - Agent C：只傳圖片 VERIFIED + LOW → PRODUCT_INFO（非 LOW 仍為 RISK）；查無 / 無法辨識 → MISSING_INFORMATION
    （列出辨識到的名稱、「查不到不代表是假貨或違法」、請補字號或清楚正面照）。有 observed claims 的 PRODUCT_INFO →
    「查驗包裝宣稱」（`ResponseAction.CHECK_PACKAGE_CLAIMS`，request-scoped postback）。
  - 按鈕：`AdaptiveResponseService.check_package_claims` 以原 request 保存的 observed claims 與該題已確認產品（字號）
    組 request（`request_for_package_claims`），`resume_with_request`，不重跑 Vision / Agent A；沿用原圖片 query label；
    已確認產品不變。候選卡選擇後 observed claims 保留。
  - Stage log 新增 `vision_ms` / `transcription_ms` / `claim_count_observed`（media download 已在 line_stage）。
  - Live（合成包裝圖，產品由 DB 隨機挑選、包裝文字取自該產品官方資料；唯讀、不寫 log）：一般包裝 18.1s（Vision 6.6s、
    語意抽取 ~8.6s、Agent B 2.9s；PERMIT_NUMBER VERIFIED、fast path、PRODUCT_INFO + 查驗包裝宣稱）；
    高風險療效宣稱 18.5s（package_claim_early_stop、assess 1 次、無 planner / web、MEDICAL_TREATMENT_CLAIM → HIGH）。
    兩張 Flex 皆通過 LINE validate API。
  - tests：`tests/unit/test_image_routing.py`（13）；相關 targeted 11 檔 254 passed。
- **Adaptive Response UX cleanup（2026-10-03；DATA / Risk Engine 規則 / resolver 門檻 / Agent 架構 / context / queue /
  perception tools 未動，只改 Agent C 回覆選擇與 renderer）**：
  - `identity_unresolved`：指稱無法解析、多 / 低信心候選、只有相似名稱或只對應公司（PARTIAL_MATCH）、只傳圖片但未確認、
    問產品合法性卻沒有任何產品資訊 → MISSING_INFORMATION（無風險燈號）。Risk Engine 結果不變（risk 物件仍有 MEDIUM），
    只是不以風險卡呈現。已有明確對象但證據不完整（許可證失效、使用者指名的產品查無紀錄、查詢失敗）仍可為 MEDIUM 風險卡。
  - MISSING_INFORMATION 不放完整分析 / 為什麼 / 資料來源；有候選時只有候選按鈕，資料來源失敗時才有「重新查詢」。
  - 圖片未確認產品：`ResponsePayload.recognized_content`「圖片辨識到：」最多 4 項（產品名稱 / 品牌 / 許可證字號 / 公司，
    取自 Agent A 抽取結果），候選卡與補充資訊卡都顯示；不是整張 OCR。候選沿用既有 inline / carousel 與選擇後 resume。
  - 官方資訊卡改摘要式：官方品名、許可證字號、許可狀態、申請商、核准功效類別＋第一條敘述（≤ 40 字）；
    下一層按鈕（`DetailPayload.available_actions`，依資料存在顯示）：查看完整功效（`FULL_EFFICACY`，去重、每條
    【功效類別】＋完整原文）、警語與注意事項（`WARNINGS`，依句號 / 分號分點）、查看資料來源（既有可點擊來源卡）。
    權限：`reachable_actions` = primary card 的 actions ＋ 其下一層（`DETAIL_FOLLOW_UPS`），仍需同 user + 同 request_id。
  - tests：`tests/unit/test_response_ux_cleanup.py`（15）；更新 2 個舊測試（完整功效原文改在下一層、辨識名稱改在辨識區塊）；
    相關 targeted 10 檔 219 passed；8 種新 / 改動 Flex 皆通過 LINE validate API。
- **Claim Policy / Advertisement Claim Verification（2026-10-03，Feature Freeze 前 correctness gate；DATA / schema / seed /
  resolver 門檻 / 候選選擇 / context / queue / Flex UI / typography / routing / perception 未動，Risk Engine 規則未動）**：
  - Root cause：Agent A prompt 把「改善／調理」一律歸 medical_efficacy，`assess_claim` 又在比對核准功效「之前」就以
    medical 判 MEDICAL_TREATMENT_CLAIM → 已確認產品的核准功效原文（調節血糖、胃腸功能改善等）被當醫療宣稱 → HIGH。
  - Claim policy（`app/risk/claim_policy.py`，deterministic、每步 rule_id）：`ClaimPolicyResult` = APPROVED_MATCH /
    APPROVED_SCOPE_CONSISTENT / EXCEEDS_APPROVED_SCOPE / MEDICAL_TREATMENT_CLAIM / UNSUPPORTED_CLAIM / INSUFFICIENT_EVIDENCE；
    `ClaimPolicyType`（PRODUCT_SPECIFIC_APPROVED_CLAIM / STATUTORY_APPROVED_SCOPE_LIMIT / STATUTORY_MEDICAL_EFFICACY_PROHIBITION /
    NO_POLICY_EVIDENCE）。順序：明確治療 / 診斷 / 取代醫療 → 該產品核准功效比對（正規化文字相同、或比對步驟 equivalent /
    supported / exceeds）→ 無對應時疾病宣稱 = MEDICAL、其餘 UNSUPPORTED → 產品未確認 / 比對失敗 / 來源失敗 = INSUFFICIENT。
    比對步驟失敗時「沒有比對結果」不當成「不在核准範圍」。無任何「看到某字就 HIGH」規則。
  - `ClaimAssessment.policy`（`ClaimPolicyDecision`）：policy_result / type / rule_id / reason（固定模板）/ matched approved claim
    （id、功效類別、原文）/ match_method / difference（比對 rationale）/ sources（核准功效紀錄或已通過 Relevance Gate 的法規文件）/
    product_identity_unresolved。status 對應：APPROVED_* → CONSISTENT、EXCEEDS → POTENTIALLY_EXAGGERATED、MEDICAL → MEDICAL、
    INSUFFICIENT → UNKNOWN（不 HIGH）。USER_QUESTION 仍走原規則，只標註核准功效對應。
  - Agent A 契約：`ExtractedClaim.medical_assertion`（none / treats_or_cures / diagnoses / replaces_medical_care /
    prevents_or_improves_disease）、`confidence`、`claim_role` / `claim_target` property；`certification_marks`、
    `other_visible_text`、`detected_product`。`separate_identity_text`：與品名 / 品牌 / 公司 / 字號 / 標章相同或只是其片段的
    「宣稱」移到 other_visible_text。Prompt：每句宣稱一筆、「改善／調節」本身不代表醫療效能。比對 relation 新增 `equivalent`。
  - Agent B：已確認產品 + 核准功效 → `approved_claim_comparison`（不跑 planner；全部文字相同時不做語意比對；最多一次
    assess；web 只在非規則決定的衝突 / 無證據）。產品多 / 低信心候選 + 宣稱 → `claims_pending_product_choice`（不跑 planner /
    web；只有疾病宣稱才查一次法規）。被說出的宣稱查 RAG 改用一般性模板 `CLAIM_POLICY_RAG_TEMPLATE`（原文查詢相似度 < 0.3 查不到法規）。
    規則已決定的疾病宣稱被法規「反駁」不算 evidence conflict。
  - Agent C：RISK / CLAIM_RESULT / 為什麼 逐條列「這項文字與官方核准功效一致（官方核准：…）」/「官方核准的是「X」，但這項宣稱
    擴大了範圍（差異）」/「涉及疾病治療…超出保健食品可宣稱範圍」；產品未確認 + 宣稱 HIGH → 摘要說明 HIGH 只針對宣稱、不是產品真偽；
    產品未確認 + 非 HIGH 宣稱 → MISSING_INFORMATION（候選按鈕 + 「宣稱要先確認產品才能比對」）。完整分析 LLM 指令只能用 policy 欄位。
  - 官方 policy 來源現況：只有 RAG 健康食品管理法全文（§2 保健功效定義、§4 表達方式、§14 不得超過許可範圍 / 不得涉及醫療效能）
    與各產品核准功效。缺：食品及相關產品標示宣傳廣告涉及不實誇張易生誤解或醫療效能認定準則（可用 / 不可用詞句）、
    健康食品保健功效項目公告與評估方法、違規廣告案例資料（`illegal_ad_cases` 目前 0 筆）。未自行建立詞句清單。
  - Live（DB 隨機挑產品、文字 / 合成包裝圖由程式組出、唯讀不寫 log）：核准原文廣告 → APPROVED_MATCH / LOW（14–21s，assess 1、
    planner 0、web 0）；核准 + 治療糖尿病 / 停藥 → 核准句 APPROVED_MATCH、療效句 MEDICAL（附法規來源）→ HIGH（19–38s，web 0）；
    核准 + 其他功效類別 → EXCEEDS / MEDIUM；多品項 + 一般宣稱 → MISSING_INFORMATION 9s（原 66s）；多品項 + 治療癌症 →
    claim-level HIGH 21s（原 56s）；合成包裝圖：品名 / 規格 / 宣稱分離，療效句自動查證 HIGH，核准句保留為 observed。
  - 已知（非本輪範圍）：Agent A 偶爾把品名中的修飾字當賣點去掉或改寫異體字（苺→莓）→ resolver 退為候選；Vision 偶有讀錯字 → 候選卡。
  - tests：`tests/unit/test_claim_policy.py`（23）；targeted 18 檔 420 passed（未跑 full suite）。
- **Pending Clarification Context（2026-10-03；DATA / Risk Engine / resolver 門檻 / UI / Agent 架構未動）**：
  - Root cause：系統詢問「圖片產品是不是這一項官方候選」後，task context 沒有保存「正在等使用者確認」的狀態；
    下一句「是 / 對」交給 Agent A 時沒有可確認的對象 → intent=unclear，失去上一題 context。
  - `TaskContext.pending_clarification`（`PendingClarification`：clarification_type（PRODUCT_CANDIDATE_CONFIRMATION =
    唯一低信心候選 / PRODUCT_IDENTITY_CONFLICT = 字號對應官方品名與看到的品名不一致）、candidate_product_id / name、
    permit_number、previous_request_id、unresolved_reason、discrepancy、created_at）。多候選（確認卡按鈕）不建立 pending。
    每一題寫回 context 都會取代 / 清除 pending（只對緊接的下一題有效）。
  - `app/services/clarification.py`（deterministic）：純文字訊息正規化後「完全」由肯定 / 否定詞＋指稱 / 語助詞組成才算
    CONFIRM / REJECT；同時肯定又否定（「對不對」「是不是」）、只有指稱詞、含其他內容 → OTHER；含另一個許可證字號 → NEW_ENTITY。
    - CONFIRM → `request_for_confirmed_target`：沿用上一題 NormalizedUserRequest＋候選字號，`resume_with_request`
      （不重跑 Agent A / planner）；保留原品名讓 resolver 照原規則判定（名稱衝突仍是 CONFLICT，不變 VERIFIED）。
      `ConfirmedProduct.confirmation_source = USER_CONFIRMED_TARGET`＋discrepancy；`user_confirmed_target` 由 Agent C 列為第一點
      「你確認是同一產品，但…不一致；這是你的確認，不是官方比對結果。」後續「它…」綁定時差異說明仍保留。
    - REJECT → 清除 pending，回提示請補品名 / 字號 / 包裝照（不跑 pipeline、不設 confirmed product）。
    - NEW_ENTITY / OTHER → 正常 pipeline（新產品優先）。上一題紀錄取不回時也退回正常流程。
  - 清除：開始新問題 / 結束本次查證 / task TTL 30 分鐘 / pending TTL 10 分鐘（`PENDING_CLARIFICATION_TTL`）。
  - tests：`tests/unit/test_pending_clarification.py`（30，虛構資料）；相關 targeted 8 檔 175 passed（未跑 full suite）。
- **Image Identity Robustness（2026-10-03；DATA / Risk Engine 規則 / UI / Agent 架構未動）**：
  - Root cause：resolver 把圖片讀到的品名與使用者打字的品名同等看待；字號已唯一命中時，Vision 誤讀品名（分數 < 0.5）
    仍判 CONFLICT → Risk `RISK_HIGH_PRODUCT_CONFLICT` HIGH。標章只有文字欄位，弧形小字誤讀還可能被當成品名。
  - 證據優先序（`resolve_product_match`）：字號 exact > 官方品名 / 別名 exact > 品名＋公司 / 品牌 > 名稱相似 > 標章 > 標章文字。
    新增 `NAME_COMPANY_CORROBORATED`：分數接近的多個候選中只有一項對應到本次看到的公司 / 品牌 → 以它為準
    （≥ 0.9 VERIFIED，否則單一候選請使用者確認）；門檻未改。
  - 圖片品名（`names_from_image`）＋字號唯一命中：只有圖片上「清楚完整的官方品名 / 別名 exact」指向另一項產品才是 CONFLICT；
    其他差異 → VERIFIED＋`ProductMatch.identity_flags`（`OCR_UNCERTAIN`；分數 < 0.5 再加 `IMAGE_TEXT_CONFLICT`），
    只讀到官方品名的一部分不算差異。Agent C 第一點：「圖片文字辨識與官方名稱有差異…產品是依許可證字號確認」
    （使用者確認的目標改用 pending clarification 的說明）。使用者自己打字的字號＋不同品名維持原 CONFLICT 規則。
  - 標章：`CertificationMarkObservation`（mark_type / visually_detected / confidence / text_readable / text），
    `certification_mark_observations` 在 IntakeExtraction 與 NormalizedUserRequest；文字只在清楚可讀時保留；
    與標章文字相同的品名 / 品牌從產品識別移除（標章不能單獨確認產品）。Vision 指令：標章小字看不清楚寫「無法辨識」。
  - 局部二次 Vision（`reinspect_image_identity` Input Tool，Agent A `reinspect_identity`，pipeline Agent B 之後）：
    只在有圖片、身份未確認（NOT_FOUND / PARTIAL / NOT_APPLICABLE）、match_type 不是 PERMIT_NUMBER、查詢有完成時做一次；
    只重看字號 / 品名區域（JSON 輸出，readable=false 的欄位不採用）；讀到新字號 / 品名才重跑 Agent B，否則沿用第一次結果。
    「局部」是指令層面的聚焦（沒有裁切影像、未加 OCR / local model）。
  - tests：`tests/unit/test_image_identity_robustness.py`（14，虛構資料）；`test_tool_boundaries` 更新 Input Tool 清單；
    相關 targeted 18 檔 403 passed（未跑 full suite）。
- **Stability Checkpoint（2026-10-03；未改 production behavior）**：
  - `docs/FROZEN_CONTRACTS.md`：39 個 frozen contracts（A Agent/Tool 邊界、B Product Verification、C Risk/Claim、
    D Sources/URL、E Response/LINE、F Task Context、G Data/Ingestion/Security），每項皆對應既有 regression test（名稱已逐一驗證存在）。
  - 補測試（虛構資料）：`tests/unit/test_frozen_contracts.py`（6）：production code 不含 fixture 產品 / 字號 / 公司 / TEST_ONLY、
    `.env` 被 ignore 且 `.env.example` 無 secret 值、查無產品時 Agent C 不產生官方結論（假貨只出現在否定句）。
  - CLAUDE.md §17 Stability Rules（extend via interfaces / frozen 需同意 / 實測失敗只當 regression case / 新功能先修或撤 /
    改 frozen 檔前說明 extension point 不足）。
  - **正式 Baseline full pytest（2026-10-03，含 live integration、db）：762 passed / 0 skipped / 0 failed（672s）**。
    - 首次 checkpoint run 的 1 failed（live image 測試寫死 `["analyze_image"]`）已依使用者選擇 (a) 只更新測試：
      analyze_image 必須第一、身份未確認才允許最多 1 次 `reinspect_image_identity`、未重讀時首次結果必須不需重讀（FC-B6）。
      未改 production code。先前的 1 skipped（live 來源卡）本次通過（依 live 結果而定）。
  - `FROZEN_CONTRACTS.md` 新增 FC-E8（Primary Flex card = 摘要，不是資料 dump），共 39 項；UI production code 未改。
  - Frozen modules / Safe extension points：見 `docs/FROZEN_CONTRACTS.md` 末段。
- **TFDA Claim Policy Corpus Data Layer（2026-10-04；只有 migration + importer + dry-run，未寫 production；
  Agent / Risk / Claim policy runtime 行為未動）**：
  - Corpus：`data/tfda_claim_policy/2026-10-03/`（2026-10-04 以 git mv 從上一層搬入，內容 / sha256 / batch_key 不變；
    版本取 validation.json `research_reference_date` = 2026-10-03）。importer 預設路徑與 tests 已指向此目錄。
  - Migration `0003_claim_policy_corpus.sql`（additive、idempotent、需 0001 marker）：`claim_policy_import_batches` /
    `claim_policy_sources` / `claim_policy_rules` / `claim_policy_examples` / `health_function_catalog`，
    另加 `health_function_historical_sources`（舊評估方法陣列改關聯表才能有 FK）。id = uuid5(corpus 原始 ID)，
    原始 ID / status / review_status / 日期 / raw_data / raw manifest（sha256）全保留；RLS＋只授權 service_role（同 0001）。
    每個 corpus 來源也登記到 `source_registry`（`tfda_claim_policy:<source_id>`），Neo4j Source 與 integrity gate 共用同一 ID。
  - Quarantine（`evidence_use`，`app/ingestion/claim_policy_corpus.py`，只看 status / review_status / policy_effect /
    extraction_method 欄位）：SUPERSEDED（本身或來源）→ `EXCLUDED_SUPERSEDED`；NEEDS_REVIEW（status / review_status /
    policy_effect / 來源）→ `CONTEXT_ONLY`；OCR_UNVERIFIED / PDF_TEXT_LEGACY_UNCONFIRMED → `CORROBORATION_REQUIRED`（不得作唯一證據）；
    例示不比所屬規則更可信。DB CHECK 擋下「SUPERSEDED / NEEDS_REVIEW 卻 DECISION_ELIGIBLE」。
  - RAG：沿用 `rag_documents` / `rag_chunks`。一個（來源, 網址）= 一份文件（124；current 110 / superseded 14），
    976 區段 → 1317 chunks（> 700 字的區段用既有 `chunk_text` 切）；區段 metadata（corpus_document_id / source_id / status /
    review_status / article_or_section / effective_date / extraction_method / evidence_use）存在 chunk metadata。
    SUPERSEDED → `is_current = false`，既有 `match_rag_chunks` 不檢索（本機已驗證）。與既有非 corpus RAG 文件同網址 → blocker。
  - Neo4j（`migrations/neo4j/0002_claim_policy_constraints.cypher`；`ALLOWED_LABELS` 加 ClaimPolicyRule / HealthFunction）：
    ClaimPolicyRule 163、HealthFunction 14、Source 71；`SUPPORTED_BY` 163、`DEFINED_BY {role: CATALOG|EVALUATION_METHOD}` 28、
    `HAS_HISTORICAL_METHOD` 4；`(Claim)-[:MAPS_TO_HEALTH_FUNCTION]` 只在 approved_claims.function_name 正規化後與官方功效名稱完全相同時建立
    （唯讀預覽：12；「促進鐵吸收」與未分類宣稱不連）。例示 / RAG chunk 不做成 node。
  - Importer：`python -m scripts.import_claim_policy_corpus --dry-run`（預設，不連線）；`--compare-supabase --read-approved-claims`
    唯讀比對；`--apply --confirm-project nutri_safe_ai` 才寫入（需 0003 marker）。fail closed（FK / ID / enum / 日期 / 官方網域 /
    validation.json 筆數）、idempotent（content_hash；相同 corpus 重跑全部 unchanged、只更新批次 run_count）、version-aware
    （拒絕降版；變更 → record_version+1；區段文字變更 → 新 RAG 版本、舊版 is_current=false；只改狀態 → 不重新 embedding）。
    illegal_ad_cases 不寫入；open-data inventory 只存 metadata（5 個資料集來源）。
  - Full corpus dry-run：sources 71 / rules 163 / examples 267 / health functions 14 / historical links 4 / RAG 區段 976，errors 0；
    quarantine：sources 61 eligible / 9 superseded / 1 context-only、rules 134 / 29 context-only、examples 260 / 7、functions 11 / 3、
    RAG 區段 876 / 69 superseded / 31 context-only。
  - **正式 Supabase 已由使用者套用 0003 並完成匯入**（2026-10-04 唯讀確認：`migration_0003_claim_policy_corpus = applied`、
    import batch 1、sources 71 / rules 163 / examples 267 / health_function_catalog 14 / historical 4、
    corpus RAG 124 documents（current 110）/ 1317 chunks；Neo4j ClaimPolicyRule 163 / HealthFunction 14 / Source 71 為使用者回報）。
  - runtime 接入：見下方 Claim Policy Runtime Integration。
  - tests：`tests/unit/test_claim_policy_corpus.py`（28）、`tests/db/test_claim_policy_migration.py`（7，本機 0001→0002→0003＋完整 corpus
    寫入本機 DB、重跑 unchanged）；連同 frozen / migration / closeout 相關共 93 passed（未跑 full suite）。
- **Claim Policy Runtime Integration（2026-10-04；DATA schema / Risk Engine 規則 / Resolver / LINE handler / primary Flex 未動；
  程式以虛構資料測試；正式 corpus 的唯讀 smoke 見本節最後一點）**：
  - Evidence Tools（`app/tools/claim_policy_tools.py`，於 `evidence_tools.py` 註冊）：`search_claim_policy_rules`（rule_id，
    或規則類別＋效果交集）、`search_claim_policy_examples`（宣稱原文與官方例示正規化後完全相同才算對應，不推測語意、無關鍵字清單）、
    `lookup_health_function`（官方功效名稱完全相同）。`SupabaseClaimPolicyRepository`（唯讀、整表讀取、process 內快取 10 分鐘）；
    `app_schema_meta.migration_0003_claim_policy_corpus` 不存在 → `corpus_available() = False`，Agent B 略過且不記成資料來源失敗。
  - Evidence priority（Agent B `_claim_policy_stage`，被說出的宣稱、每題一次，在 RAG 之前）：該產品核准功效（VERIFIED 且文字相同即決定）
    → 結構化規則 → 官方例示 → claim-policy RAG → 官方網站。結構化證據已足以決定的宣稱（`policy_decided_claims`）不查 RAG、
    不觸發官方網站（URL / 包裝 / 完整流程三處判斷皆納入）。已確認產品的核准功效類別另查保健功效目錄（法規依據明細用）。
  - Claim policy（`classify_claim_policy(structured=...)`，未提供時行為完全不變）：核准功效比對永遠在前；之後宣稱與
    DECISION_ELIGIBLE 的醫療效能例示（規則 PROHIBITED）相同 → MEDICAL_TREATMENT_CLAIM（`OFFICIAL_MEDICAL_EFFICACY_EXAMPLE`）；
    沒有產品核准功效時與 DECISION_ELIGIBLE 的通常可使用例示（規則 PERMITTED）相同 → 新結果 `PERMITTED_GENERAL_WORDING`
    （→ CONSISTENT）；產品有核准功效時通常可用例示不覆蓋核准範圍。醫療效能判定附上 DECISION_ELIGIBLE 醫療效能禁止規則作為法規來源。
    CONTEXT_ONLY / CORROBORATION_REQUIRED / 有條件或一般功能例示 / RESTRICTED 規則的例示只提供脈絡：原本會是「找不到官方支持」時改為
    INSUFFICIENT_EVIDENCE（`POLICY_CONTEXT_ONLY_NOT_DECISIVE`），不單獨產生合法 / 違法結論。
  - Quarantine：`PolicyEvidenceUse`（與 importer `EvidenceUse` 同值，有測試）在規則 / 例示 / 保健功效 / RAG chunk 上一路保留到
    EvidenceBundle（新欄位 `claim_policy_rules` / `claim_policy_examples` / `health_functions`，納入 `all_sources()` / `evidence_ids()`）。
    EXCLUDED_SUPERSEDED 預設不查出（tools 與 search_rag 皆是）。
  - `search_rag`：corpus chunk 帶 `RagEvidence.policy_metadata`（source_id / status / review_status / evidence_use / article_or_section /
    effective_date / source_url / extraction_method）；metadata 無法解析 → CONTEXT_ONLY。非 DECISION_ELIGIBLE 的 chunk 不進
    `official_reference_ids` / `regulation_sources`（不能成為 final legal evidence，也不能讓宣稱變 CONFLICT）；assess payload 標示 evidence_use。
  - Reporter / UI：primary card 只用 policy 固定模板 reason（FC-E8 不變）。「為什麼？」卡在有結構化 policy 證據時才出現下一層
    [查看法規依據]（新 `ResponseAction.POLICY_BASIS`，deterministic：判定用到的規則在前、條次＋摘要＋來源名稱、待複核標示「不單獨作為結論」、
    保健功效官方說明）與既有 [查看資料來源]（可點擊官方來源卡，現在含法規來源）。沒有新增「查看官方來源」獨立 action，沿用既有來源卡。
  - Frozen 檔案變更（extension point 不足的原因：`classify_claim_policy` 原本沒有「官方例示 / 結構化規則」輸入）：
    `claim_policy.py`（新選填參數＋新 enum 值）、`assessment_rules.assess_claim`（透傳＋新結果對應 CONSISTENT）、
    `verification_agent.py`（新 stage；fast path / early-stop 條件未改，只把 policy 已決定的宣稱納入 web 判斷）、`actions.py`（新 label / follow-up）。
  - tests：`tests/unit/test_claim_policy_runtime.py`（22，虛構規則 / 例示）；targeted 23 檔 524 passed（未跑 full suite）。
  - **Production READ-ONLY smoke（2026-10-04，無任何寫入）**：連線 project marker = nutri_safe_ai；
    `SupabaseClaimPolicyRepository` 對正式 0003 schema 查詢成功（rules 163 / examples 267 / functions 14，`corpus_available()=True`）。
    醫療效能 PROHIBITED 規則 5 條（DECISION_ELIGIBLE）、官方醫療效能例示原文 → 對應 1 筆 DECISION_ELIGIBLE 例示、保健功效含官方定義與來源網址。
    `search_rag`（只做查詢 embedding）：DECISION_ELIGIBLE chunk 回傳 source_id / status / review_status / evidence_use /
    article_or_section / effective_date / source_url 完整；superseded 文件的 chunk 不在 `match_rag_chunks` 結果，current 文件內
    EXCLUDED_SUPERSEDED chunk 0 筆。CONTEXT_ONLY 例示（所屬規則 NEEDS_REVIEW）可讀到，`assess_claim` → INSUFFICIENT_EVIDENCE
    （`POLICY_CONTEXT_ONLY_NOT_DECISIVE`）；CONTEXT_ONLY / NEEDS_REVIEW RAG chunk 可檢索但 `decision_eligible=False`。
    正式 rules 表沒有 SUPERSEDED 規則（superseded 只在 sources / RAG），規則層的 superseded 排除只由虛構資料測試覆蓋。
    尚未跑完整 pipeline（Agent A → C）live regression。
- **Claim verdict consistency ＋ USER_QUESTION 命題風險 ＋ Flex 版面（2026-10-04；DATA / schema / corpus / Risk Engine 規則 /
  resolver 未動，不 hard-code 產品或句子）**：
  - Root cause：(A) CLAIM_RESULT primary 的結論用 `CLAIM_STATUS_SUMMARY[status]`，「為什麼」用另一套組字，完整分析由 LLM 依原始
    approved_claims 自行推論；USER_QUESTION 的產品特定命題只有「核准原文完全相同」或 LLM 比對連結才算支持，比對沒給連結時
    status = UNSUPPORTED → 卡片說「找不到官方資料支持」，完整分析卻說有核准功效。(B) USER_QUESTION 的疾病命題固定落在
    `QUESTION_MEDICAL_NOT_SUPPORTED`（UNSUPPORTED、無 policy），卡片只能寫「找不到官方資料」，沒有命題本身的宣稱風險。
  - Single source of truth：`ClaimAssessment.claim_risk`（`claim_risk_level`：醫療效能 → HIGH、產品方宣稱與官方矛盾 → HIGH、
    與官方一致 → LOW、其餘 → MEDIUM），在 `assess_claim` 決定。Reporter 的 `claim_verdict_line` / `claim_verdict_summary`
    同時用於 primary 重點、RISK 卡重點、「為什麼？」與完整分析的「查證結論」區塊；完整分析標題 = primary 的宣稱風險；
    LLM 收到 `claim_verdicts` 且寫出相反結論（LOW 卻說找不到支持 / HIGH 卻說在核准範圍）時改用固定模板。
  - Health-function matching（`match_approved_function` / `approved_function_decision`，只用於 USER_QUESTION、無疾病語意、
    無絕對化用語、無 EXCEEDS）：ApprovedClaim 的官方功效名稱（＝保健功效目錄官方名稱）與 Agent A 的 target_function
    正規化後完全相同，或沒有 target_function 時命題文字只出現一個官方功效名稱 → APPROVED_SCOPE_CONSISTENT
    （`POLICY_QUESTION_WITHIN_APPROVED_FUNCTION`）；只有部分重疊 / 多個功效 → INSUFFICIENT_EVIDENCE（`POLICY_FUNCTION_MATCH_AMBIGUOUS`）。
    未分類核准宣稱不參與；官方例示 exact-match 規則未放寬。
  - USER_QUESTION：疾病命題以與廣告相同的 `classify_claim_policy` 評估；MEDICAL → status MEDICAL_TREATMENT_CLAIM、
    claim_risk HIGH，rule `QUESTION_PROPOSITION_MEDICAL_CLAIM`，origin 仍是 USER_QUESTION、非產品方 → Risk Engine 產品風險不升級。
    文字固定：「若把這句話當成產品功效宣稱，屬於涉及疾病治療或醫療效能的高風險說法」，摘要註明「不是指你違法，也不是指產品本身危險」。
  - Risk display：`ResponsePayload.claim_risk_level`（只允許 CLAIM_RESULT）→ 標題下方「🟢 官方核准範圍內／符合官方資料」/
    「🟠 需要更多證據」/「🔴 高風險宣稱」；每個命題重點前有 🟢🟠🔴。RISK 卡維持原產品風險燈；PRODUCT_INFO 不顯示風險燈。
  - Flex（`line_renderer.py`）：標題區 / 結論 / 重點 / 附註各自為 section（margin lg + separator）；每個重點是 horizontal box
    （符號 flex 0 ＋ 內容 flex 1）；明細每一行獨立 component（・ / 🟢🟠🔴 開頭為 bullet）。body / note 加 lineSpacing
    （4px / 2px）。TEXT_SIZES、wrap、scaling、按鈕 shrink-to-fit 不變；內文不 shrink；無 xs、無色塊。7 種卡片通過 LINE validate API。
  - 更新舊測試（改的是被要求改變的契約）：USER_QUESTION 疾病命題不再斷言「不是 MEDICAL_TREATMENT_CLAIM」，改斷言
    origin 不變、claim_risk HIGH、產品風險非 HIGH（test_assessment_rules / test_claim_policy / test_agents / test_demo_stabilization /
    test_resolver_parity）；被官方反駁的提問測試改用非疾病命題。FROZEN_CONTRACTS FC-C3 / FC-E1 文字同步更新。
  - tests：`tests/unit/test_claim_verdict_consistency.py`（18，虛構）；targeted 25 檔 565 passed（未跑 full suite）。
- **Product wording ＋ USER_QUESTION claim-policy fast path ＋ 警語編號（2026-10-04；DATA / schema / corpus / Risk Engine 規則未動）**：
  - Product wording root cause：`select_response_type` 把所有 PRODUCT_VERIFICATION / SAFETY_INQUIRY 都給風險卡，
    VERIFIED + LOW 的標題是「這項產品有官方紀錄，產品本身目前看起來屬於低風險」，完整分析標題沿用。
    修正：沒有命題、Risk Engine 為 LOW（無任何風險發現）且產品 VERIFIED → PRODUCT_INFO（無風險燈，摘要「官方紀錄中有…」）；
    許可證失效 / 違規廣告紀錄 / 衝突 / 查無等實際發現仍是風險卡。SAFETY_INQUIRY 的產品資訊加上「官方紀錄代表有登記，
    不代表對每個人都安全…」與官方警語則數提示。VERIFIED_LOW_TITLE 改為「查有官方紀錄，目前沒有發現不當宣稱」（只用於有命題的風險卡）。
    完整分析標題：RISK → 風險標題、CLAIM_RESULT → 宣稱風險、其他 → 卡片標題（無燈）；LLM prompt 第 13 點＋
    `product_info_overclaims` guard（產品資訊 / 成分回答寫出「低風險 / 風險較低 / 很安全」→ 固定模板）。
  - Question fast path root cause：`_claim_policy_stage` 只處理 `is_asserted` 宣稱，USER_QUESTION 命題永遠不在
    policy_decided → claim comparison path 跑 RAG → assess →「claim_beyond_internal_evidence」→ 官方網站 → 第二次 assess（live ~48s）。
    修正：stage 處理所有命題（宣稱與提問同一條路徑：核准功效 → 結構化規則 → 例示 → RAG → web）。
    核准功效層不需要 corpus：核准原文相同、或提問所問功效 = 核准功效類別（含部分重疊 → INSUFFICIENT）→ final。
    疾病命題查醫療效能 DECISION_ELIGIBLE 規則 → decided（不查 RAG / web）；明確治療 / 診斷 / 取代醫療＋規則 → final。
    新 `policy_final_claims`：claim comparison / package path 中所有命題都 final → 直接回（不 RAG、不 web、不 assess）。
    「改善 / 預防某疾病」類命題在有核准功效時仍需一次語意比對（可能等價於核准功效），但不查 RAG / web。
    claim_role 仍是 USER_QUESTION、claim_risk HIGH、產品風險不升級。沒有 corpus 時疾病提問維持原 RAG 路徑。
  - 警語：renderer `_bullet` 顯示前移除行首純列表編號（`1.` / `1、` / `(1)` / `（1）`，`1.5` 這類數值與「第1條」不動），
    統一用一個 bullet component；EvidenceBundle / DB 原文不變。
  - 更新舊測試（被要求改變的行為）：登記查詢 🟢 低風險卡 → 產品資訊無燈（test_adaptive_response ×2、test_agents、
    test_context_binding_fonts「要注意什麼？」→ PRODUCT_INFO）。
  - tests：`tests/unit/test_question_fast_path_and_wording.py`（19，虛構）；targeted 26 檔 584 passed（未跑 full suite）。
    尚未做 live latency 量測（預期疾病提問於已確認產品：不再有 RAG / web / assess）。
- **Claim Policy Runtime 收尾（2026-10-04；schema / corpus / Risk Engine / claim policy 語意 / FROZEN_CONTRACTS 定義 /
  OpenAI prompt 未動；手機實測 treatment question 6–7s、claim risk / UI 正確）**：
  - 列表排版（DISPLAY-ONLY，`line_renderer.split_list_items`）：行首或標點 / 空白後的 `1.` `1、` `(1)` `（1）`，
    且編號從 1 依序遞增、至少 2 項才拆；`1.5`、`0.5%`、`第1條`、字號、日期、句中數字、單一編號不拆。
    前言一行＋每項一個子項目 component（◦、paddingStart、margin sm），沒有前言時項目即第一層 bullet。
    套用於 primary 重點（每點最多 6 子項，不計入 FC-E8 的 3 點）與所有明細（完整功效 / 警語 / 完整分析 / 法規依據）。
    EvidenceBundle / payload / DB 原文不變。3 種 Flex 通過 LINE validate API。
  - 說法查證卡：重點改為命題判定（一致時只標「官方核准功效：功能名稱」）＋「官方核准功效：<類別名稱>」；
    不再在卡片上放完整核准原文（原本兩處重複）。完整分析在「查證結論」後加「官方核准內容」（原文，顯示時拆項）。
    被說出的宣稱（廣告 / 包裝）的 claim_policy_line 不變。
  - 功效詢問 fast path root cause：開放式功效詢問的泛稱判斷只用固定詞表，「核准保健功效」「官方核准的保健功效」「保健效果」
    等組合被當成具體功效 → 保留成命題 → claim path（RAG / assess / web）。修正（intake deterministic 規則，非 prompt）：
    只由修飾詞（官方 / 核准 / 許可 / 保健 / 健康 / 的…）＋泛稱核心詞（功效 / 效果 / 功能…）組成即為泛稱 → 不是命題 →
    既有 structured fast path（不 RAG / assess / web / planner）。指定具體功效（含核准功效名稱）仍是命題（上一輪的 deterministic 路徑）。
  - 純產品名稱 fast path：intent=UNCLEAR、無命題、有品名候選、無字號 / 成分 / URL / 圖片 / 外國 / 缺 context，
    resolver VERIFIED 且 match_type 為 CANONICAL_EXACT / OFFICIAL_ALIAS_EXACT，且使用者原始訊息正規化後就等於官方品名或
    命中的別名 → `structured_fast_path`（`fast_path_reason = product_name_only_exact`）→ PRODUCT_INFO。
    其他 UNCLEAR（有額外語意、只有相似 / 部分名稱、沒有產品）照舊走完整流程。
  - tests：`tests/unit/test_list_layout_and_fast_paths.py`（25，虛構）；targeted 27 檔 609 passed（未跑 full suite）。
- **Approved-function 提問 deterministic fast path（2026-10-04；schema / corpus / Risk Engine / claim policy 語意 /
  Agent A prompt / Frozen Contract 未動）**：
  - Root cause（正式資料唯讀重播，不寫 log）：Agent A 的 target_function 是改寫（「肝臟保健」「肝臟功能」對應官方「護肝」、
    「胃腸功能」對應「胃腸功能改善」），上一輪只認 target 與核准功效名稱完全相同 → 落到 RAG + assess（~21s）；
    部分重疊則被當成 deterministic INSUFFICIENT（太早定案）。另外「沒有 target 時以文字包含核准功效」只看產品自己的核准功效，
    問句同時提到未核准功效時會誤判為單一功效。
  - 條件（全部成立才 final）：產品 VERIFIED、USER_QUESTION、無疾病語意 / 絕對化用語；問句去掉產品 / 品牌名稱後，
    以保健功效目錄（`lookup_health_function` 新參數 `question_texts`、tool `health_functions_in_text`，只比官方名稱字面）
    找到「唯一」一個官方功效名稱；該功效 = 產品核准功效類別（正規化名稱相同）；Agent A 的 target 不是另一個官方功效名稱。
    → `StructuredPolicyContext.question_function` → APPROVED_SCOPE_CONSISTENT（match_method `health_function_catalog`），
    claim comparison path 直接停止：不 RAG / assess / web / planner。target 與核准功效名稱完全相同的既有規則保留。
  - 保守路徑：部分重疊（問句沒有完整官方名稱）、多個功效、target 指向另一個官方功效、產品沒有該核准功效、沒有 corpus →
    既有 evidence path（RAG + 一次 assess）。`match_approved_function` 在沒有 target 時不再以文字包含決定對應（只標 ambiguous）。
  - Primary：所有命題都是「在核准功效範圍內」的提問時，重點只有「官方核准功效：功能名稱」一次；逐條判定在「為什麼」/ 完整分析。
  - Live 唯讀重播（隨機產品）：護肝提問 20.8–22.0s → 5.7–6.4s（tools：product / approved / warnings / ingredients / KG /
    illegal ads / lookup_health_function）。
  - tests：`tests/unit/test_health_function_fast_path.py`（12，虛構）；更新 1 個舊測試（無目錄時不以文字包含判定）；
    targeted 28 檔 621 passed（未跑 full suite）。
- **TFDA Claim Policy Runtime = FROZEN（2026-10-04）**：手機 final regression 通過（exact product name / open efficacy /
  treatment USER_QUESTION / approved-function question 皆走 structured deterministic path，rag / planner / assessment /
  official_web 皆 0 ms）。之後修改本區行為（claim-policy evidence priority、quarantine、USER_QUESTION 命題風險、
  approved-function / efficacy / product-name fast path、claim verdict 單一來源、列表顯示）需使用者明確同意。
  - Full pytest（含 live integration / DB）：891 passed / 1 failed / 1 skipped（663s）。唯一 failure 為
    `test_openai_live.py::test_1_user_question_about_disease_is_not_an_advertised_claim` 的過期斷言（仍斷言 USER_QUESTION 疾病命題
    不是 MEDICAL_TREATMENT_CLAIM、且需要 LLM planner），非 production regression。經使用者同意只更新該測試為現行契約
    （USER_QUESTION、MEDICAL_TREATMENT_CLAIM、claim_risk HIGH、產品風險非 HIGH、不需 planner / RAG / web、最終回答不寫成「產品宣稱」；
    結構化政策使用虛構 corpus `FakeClaimPolicyRepo`）後重跑 `tests/integration/test_openai_live.py`：8 passed（157s）。
    skipped 1 為 `test_line_demo_live.py` 第一題沒有官方來源時的條件式 skip（unit test 已覆蓋）。
- **Product mention provenance 安全修正（2026-10-05；DATA / schema 表 / Risk Engine 規則 / claim policy 未動）**：
  - Agent A 抽出的產品名稱與使用者原文分開保存：`NormalizedUserRequest.product_mentions`（`ProductMention`：`raw_text` / `corrected_name` /
    `provenance` = USER_TEXT / AGENT_CORRECTED / NOT_IN_USER_TEXT）。`product_candidates` 只放使用者寫法（改字者換回原文片段），
    Agent A 的修正寫法只進 `corrected_product_candidates`（`app/agents/intake_agent.py:product_mentions`、`app/normalization.py:locate_in_text`）。
  - Resolver：exact / 基本品名只比對使用者寫法，改字後的名稱不可能成為 CANONICAL_EXACT；NOT_IN_USER_TEXT 名稱不當使用者寫法、名稱相似度不計分。
    3b（修正寫法佐證）改為單一候選請使用者確認（`CORRECTED_NAME_CORROBORATED` + LOW_MATCH_CONFIDENCE），不再自動 VERIFIED。
    字號 / 官方別名 / 逐字 exact 行為不變。
  - tests：新增 `tests/unit/test_product_mention_provenance.py`；`test_p1_fastpath_dedup.py` 的 corroborated 測試改為「需使用者確認」，
    兩個 fixture（planner 重查）改為把品名放進使用者輸入。
- **Bare nutrient routing / Clarification before research / Taiwan imported scope（2026-10-05；resolver 門檻 / provenance / Candidate Relevance Gate /
  Claim Policy / Risk Engine 規則未動）**：
  - Issue 1：整則訊息只有成分名稱（唯一 canonical entity、無品牌 / 公司 / 字號 / 劑型 / 產品字樣 / 代名詞）→ `app/services/bare_name_routing.py`
    在 context 綁定前改為獨立成分詢問，不沿用前一題的已確認產品（原因：基本品名「包含於」已確認產品官方品名的例外把它綁回去）。
    名稱同時是 canonical entity 且完全等於官方產品名稱 → `identity_collision`，回 MISSING_INFORMATION 請使用者選「產品 / 成分」，不替使用者決定。
    代名詞 follow-up 仍沿用前一題產品。未實作：「那 X 呢？」這類語意模糊的省略式 follow-up 的澄清（目前交由 Controller 判斷）。
  - 獨立成分的「生理功能」證據尚未建立：目前只回官方成分名稱（INGREDIENT_LIST）；Interaction corpus 不當成一般功能的證據。
  - Issue 2：`VerificationAgent._try_identity_clarification_stop`——產品只有候選（PARTIAL / 修正寫法待確認 / 多候選）或 collision 時，
    在 fast path 之前停止：不跑 planner / RAG / web / assess / graph / 違規廣告查詢；有宣稱者仍走既有 claims-pending 路徑。
    選定候選後由 `request_for_selected_product` + `resume_with_request` 以原題續跑，不重跑 Controller Agent。
  - Issue 3：`scope_foreign_signal`（assessment_rules）——resolver 已 VERIFIED 到本資料庫（台灣 TFDA 登記，含 imported_tablet_capsule）的產品，
    不因 Controller 抽到國別而被當成外國產品（不看名稱字樣）；沒有台灣紀錄者維持 `foreign_product` 行為。
  - tests：新增 `tests/unit/test_bare_name_routing.py`（16）；既有測試依 provenance 契約把產品名稱放進使用者輸入（fixture），
    `test_p1_fastpath_dedup` 的 planner 重查測試改為「身份未確認不進 planner」；README 加入 Controller / Researcher / Reporter 用語（識別字不變）。
- **Standalone entity 修正（2026-10-05；exact Product / typo / provenance / relevance gate / Taiwan imported / Claim Policy / Risk Engine 未動）**：
  - live bug 根因：Controller 看到前文實體後，把裸成分名稱當成代名詞解析成前一題產品（`context_resolved_names`），
    產品欄位 / intent 都指向前一題產品，上一版 routing 只看 Controller 的 product_candidates 所以沒攔到。
  - `route_bare_name` 現在先看「這一句原文」：整句（去頭尾標點）是唯一 canonical entity、沒有劑型 / 產品字樣 → `standalone_entity`，
    清掉 Controller 的產品 / 品牌 / 前文解析，這一題不綁定前一題產品（不改 context）；代名詞句（「它有含 X 嗎？」）原文不是裸名稱，照舊綁定。
    問句形式（「X 有什麼功能？」）則看 Controller 的 product_candidates 是否全是 canonical entity 且無產品訊號。
  - collision 只算「完全等於官方產品名稱 / 官方別名」；基本品名（括號規格）相同不覆蓋 canonical entity exact。
  - Canonical identity：`EvidenceBundle.standalone_entities`（Ingredient Bridge entity：名稱、類型、來源）＋ `function_evidence_status`
    （NOT_ASSESSED / SUPPORTED / INSUFFICIENT）。identity、legacy ingredients 表、產品成分、功能證據是四件不同的事；
    有 entity 就不會說「找不到成分」。回覆是精簡成分 / 營養素概覽，不是產品卡，也不稱生理功能為「核准保健功效」。
  - 獨立成分路徑 `_try_standalone_entity_path`（`standalone_entity` 模式）：entity 已確認時 lookup_ingredient / RAG / assess_evidence 各一次，
    內部證據不足才官方網站一次；不跑 planner 多輪（live：功能問題 30–40s → 約 13s）。概覽不查功能證據。
    沒有通過 Relevance Gate 的證據 → INSUFFICIENT，回「沒有足夠的官方證據」。graph（ingredient_approved_functions 是產品核准功效關係）在此路徑不查。
  - tests：`tests/unit/test_bare_name_routing.py`（25）。
- **Standalone ingredient identity 統一（2026-10-05；語意路由 / resolver / provenance / Product 行為未動）**：
  - audit：production Ingredient Bridge 只有 78 個 entity（17 NutrientGroup / 61 IngredientForm）、321 個 alias；「鋅 / Zinc / 維生素 C / K / 葉酸 / 魚油」
    等 exact / 已接受 alias 都解析成功；「維生素 A」不在 corpus（entity 與 alias 皆無）。上一版只有「已有 entity」才走獨立成分路徑，
    entity 不存在時掉進 planner 多輪＋RAG，並顯示「沒有找到名稱完全相同的成分」（誤導）。
  - 現在獨立成分詢問（INGREDIENT / EFFICACY / SAFETY intent、無產品識別、無命題）一律走 `standalone_entity` 路徑，identity 與功能證據分開記錄：
    `ingredient_identity_status` = CANONICAL_ENTITY（Bridge entity / 已接受 alias，exact）/ LEGACY_OFFICIAL_NAME（官方成分表 exact）/
    IDENTITY_NOT_IN_CORPUS；`function_evidence_status` = NOT_ASSESSED / SUPPORTED / NO_FUNCTION_EVIDENCE。不用相似度 / embedding 建立 identity；
    候選型 alias（AMBIGUOUS / NEEDS_REVIEW）不算 identity。
  - IDENTITY_NOT_IN_CORPUS：回「識別資料還沒有收錄，不代表不存在」，並依一般規則 RAG 一次＋Gate（問功能時才補官方網站一次）；
    有通過 Gate 的證據 → SUPPORTED，否則 NO_FUNCTION_EVIDENCE。不為了 Q&A 擴充 interaction mapping。
  - 已知：未收錄的裸名稱若被 Controller 判成 product_verification（LLM 偶發），仍會走產品查詢（本次不改語意路由）。
  - tests：`tests/unit/test_standalone_identity.py`（9）。
- **裸一般成分用語路由＋獨立成分「查看完整分析」（2026-10-05；resolver / Product Coverage / Bridge / Claim Policy / Risk Engine / relevance gate / typo 未動）**：
  - 根因：裸名稱只有「在 Bridge corpus 內」才會被攔下；不在 corpus 的一般成分用語（例如維生素＋字母）若被 Controller 判成產品，
    會因為產品名稱含有相同字樣而出現產品候選。
  - `route_bare_name`：整句全是一般成分 / 營養素用語（Candidate Relevance Gate 同一套字元分類：沒有任何有辨識力的字樣、沒有劑型 / 產品字樣）
    且不是完整的官方產品名稱 → 獨立成分詢問（IDENTITY_NOT_IN_CORPUS，證據照一般規則）；完全等於官方產品名稱 → 既有 collision 詢問；
    有辨識力字樣的產品名稱錯字仍走產品候選確認。
  - 「查看完整分析」：獨立成分回覆在有可用證據（已確認 identity、官方成分名稱、或通過 Gate 的功能證據）時才提供；沒有證據不提供。
    沿用既有 request-scoped DETAIL 機制（同一份證據、不重跑 Controller、不新增證據）；產品成分卡按鈕不變。
  - tests：`tests/unit/test_standalone_identity.py`（17）。
- **Taiwan Product Coverage Importer Design + Dry Run（2026-10-04；未寫 production、未套 migration；
  claim-policy / risk / LINE / resolver frozen runtime 未動）**：
  - 研究 snapshot：`data/tfda_product_coverage/2026-10-04/`（原本誤放在 `data/tfda_claim_policy/2026-10-04/`，已搬移；
    SHA256SUMS 全部 OK；9112 列 raw_row_sha256 皆可由原始 CSV 重算）。
  - 新類別（`ProductCategory`，`app/ingestion/tfda_product_coverage.py`）：`domestic_vitamin_tablet_capsule`（InfoId 24，
    source_key `tw_fda_domestic_vitamin_tablet_capsule`）、`imported_tablet_capsule`（InfoId 23，`tw_fda_imported_tablet_capsule`）；
    既有 `health_food` 不變。新類別只代表「查有官方查驗登記／核備資料」：不產生 health_food、核准功效、警語、品牌 / 別名、成分 canonical。
  - Canonical key `TFDA:{info_id}:{原始字號}`（字串、保留前導零；輸入類 196 筆前導零）；products.id = uuid5('product','TW:'+key)、
    permits.id = uuid5('permit','TW:'+key)（與 health_food 的 `TW:<字號>` 命名空間不同）。
  - Schema reuse（0001，無新表）：source_registry（2 列）/ raw_import_records（raw_hash = 官方原始列 sha256；raw_data =
    原始欄位＋登記事實（發證 / 有效日期、原始日期、official_status = null、簽審文件編號、申請商電話地址）＋成分 staging）/
    companies（申請商；只新增，既有公司沿用不更新）/ permits（permit_number = permit_code = 完整原始字號，permit_type = 查驗登記證 / 核備函，
    status = UNKNOWN、status_raw = NULL）/ products（product_category 新值、status = UNKNOWN）。derived_temporal_status 不寫入（隨時間改變），
    只在 dry-run 報告依 as-of 計算。
  - Migration：`migrations/0004_tfda_product_coverage.sql`（2026-10-04 由 proposal 轉為正式 additive migration，**未套用**）：
    permits.expiry_date / signing_document_no、products.product_category CHECK（三值）、product_details view 末尾追加 product_category /
    expiry_date / signing_document_no / permit_status_raw；需 0003 marker。`--apply` 沒有 `migration_0004_tfda_product_coverage` marker 時拒絕寫入。
  - Importer：`python -m scripts.import_tfda_product_coverage --dry-run`（預設、不連線）；`--compare-supabase`（唯讀）；
    `--apply --confirm-project nutri_safe_ai`。fail closed：SHA256SUMS / validation.json 筆數與表頭 / 05 來源（官方網域、sha256）不符 → error；
    逐列隔離：缺字號、非數字字號、缺品名、未知類別、未知 review_status、日期錯誤、與研究 normalized 結果不一致、canonical key 重複、
    跨資料集同字號、與 DB 其他來源列 ID / 字號碰撞。idempotent（相同 snapshot 重跑全部 unchanged、0 寫入）；version-aware
    （DB 已有較新 retrieved_at → 拒絕降版；官方內容變更 → 新 raw 版本、舊版保留、product update）。不以品名合併。
  - Dry-run（研究 snapshot，as-of 2026-10-04）：Domestic 1705 / Imported 7407 / total 9112；would insert 9112、update 0、unchanged 0、
    quarantine 0；key collisions 0；missing required 0；READY 5069 / NEEDS_REVIEW 4043（成分解析品質，不阻擋登記匯入）；
    依日期比較（不寫入）國產 CURRENT 1695 / EXPIRED 10、輸入 7115 / 292。
    唯讀比對正式 Supabase：ID / 字號碰撞 0；申請商 1130 家中 67 家沿用既有公司、新增 1063；與既有 health_food 正規化品名相同 14 筆（audit only）。
    匯入後 products 理論上 565 + 9112 = 9677 列（不是 9677 個唯一商品：跨類別候選未合併）。
  - 成分：只保存 ingredients_raw、研究 token draft、review_status / review_reasons（raw_data.ingredient_staging，
    `interaction_eligible = false`）；不建 ingredients / product_ingredients / Ingredient node / INTERACTS_WITH。
  - 跨類別：08 候選 103 pairs（國產 vs 輸入 85、新類別 vs 公開健康食品參考 18；CONFIRMATION_REQUIRED 98 / FAMILY_ONLY 5）、
    07 同名不同字號 491 列 → 只留 CSV 作研究證據 + dry-run 報告；不 SAME_AS、不 merge、未新增 candidate table。
  - Neo4j plan（dry-run；apply 才寫）：沿用既有 `(Product)-[:SUPPORTED_BY]->(Source)`（未另建 SOURCED_FROM，避免同義關係）、
    `(Product)-[:REGISTERED_AS]->(Permit)`、`(Company)-[:OWNS_OR_APPLIES_FOR]->(Product)`；新 Product 帶 `product_category`；
    Company 用 ON CREATE SET（不改既有屬性）。Source 2 / Product 9112 / Permit 9112 / Company merge 1130。
    既有 565 Product 目前沒有 product_category property → 見下方 Runtime compatibility（Neo4j 0003 backfill，apply 時執行）。
  - **Runtime impact（2026-10-04 audit；1–5、7–9 已由下方 Runtime compatibility 處理，6 依 frozen claim policy 維持不變）**：
    1. `app/normalization.py` `extract_permit_code`（`[A-Z]\d{5}|\d{6}`）會把 10 碼字號截成前 6 碼 → `lookup_permit`
       （`app/tools/product_tools.py`）查不到新字號，且可能誤中 6 碼數字的健康食品字號；`context_binding.py` / `clarification.py` 同樣使用。
    2. `app/risk/risk_engine.py` `RISK_MED_PERMIT_INACTIVE`：status ≠ ACTIVE 一律 MEDIUM「官方許可目前不是有效狀態」→ 新類別（UNKNOWN）每筆都會被誤判。
    3. `product_details` view 與 `ProductRecord`（`app/schemas/evidence.py`）、`_product_from_row` 沒有 product_category / expiry_date（0004 proposal＋新選填欄位）。
    4. `app/agents/reporter_agent.py`：`PRODUCT_STATUS_TEXT`「許可證狀態不明」、候選 `is_active`、官方資訊卡「許可狀態 / 核准功效」用語；
       新類別應顯示「查驗登記證號 / 核備函字號、發證 / 有效日期（依日期，非官方狀態）」，不得出現「官方核准保健功效」。
    5. Agent B fast path（`verification_agent.py`）：功效 / 成分問題要求核准功效 / product_ingredients 存在才停止 → 新類別會落到 planner / RAG / web；
       需類別感知回答（「此類別登記不含保健功效核准」、成分顯示 raw 原文＋未解析提示）。
    6. Claim policy（frozen）：核准功效比對、`STATUTORY_*` 依健康食品管理法；新類別屬一般食品（食安法 §28 標示廣告規範），
       `CLAIM_POLICY_RAG_TEMPLATE`「健康食品標示或廣告…」與「保健食品不能這樣宣稱」等用語需類別感知。
    7. NOT_FOUND（`NOT_FOUND_NOT_FAKE`、risk 訊息）：之後應說明目前涵蓋的官方類別（健康食品 / 國產維生素錠狀膠囊 / 輸入錠狀膠囊）。
    8. Resolver：9677 列下同名跨類別增加 → 更多 MULTIPLE 候選；候選卡 label 應標類別。
    9. `scripts/check_data_integrity.py --expect-products 565`、`graph_sync`（無 product_category）、evidence_tools 的「查台灣官方健康食品紀錄」描述。
  - tests：`tests/unit/test_tfda_product_coverage.py`（19，虛構＋研究 snapshot 筆數）、`tests/db/test_tfda_product_coverage_db.py`
    （4，本機 PostgreSQL：0001→0003＋完整 9112 筆寫入、health_food 列不變、重跑 0 寫入、0004 proposal 可重跑＋typed 欄位）；
    targeted 163 passed；`tests/unit` 全部 868 passed（未跑 live integration）。
- **Claim-policy corpus 路徑整理＋Taiwan Product Coverage runtime compatibility（2026-10-04；未寫任何正式 DB、未套 0004、
  未匯入 9112 筆；claim-policy evidence priority / claim semantics / risk semantics 未動）**：
  - 路徑：claim-policy corpus 9 檔 `git mv` 到 `data/tfda_claim_policy/2026-10-03/`（根目錄不留檔）；sha256 前後相同、
    dry-run 輸出相同（batch_key `79757f38…`）。更新 `scripts/import_claim_policy_corpus.py` 預設路徑與兩個 corpus 測試。
    `data/tfda_product_coverage/2026-10-04/` 未動。
  - 共用規則 `app/product_coverage.py`：`ProductCategory`（移到 `app/schemas/enums.py`，importer 共用）、類別標籤、
    `OFFICIALLY_INACTIVE_STATUSES`（EXPIRED / MERGED / CANCELLED / REVOKED）、`derived_temporal_status`（只顯示日期狀態）、coverage 文字。
  - 字號：`extract_permit_code` 改為「英文字母＋5 碼」或「完整數字段（≥6 碼，前後不接數字 / 英文字母）」：10 碼不截斷、前導零保留、字串；
    健康食品 `A00235` / `000042` / 全形不變。本機 DB 測試：9112 筆官方字號解析結果 = permit_code = 原字號。
  - UNKNOWN：`RISK_MED_PERMIT_INACTIVE` 只在官方明確非有效狀態觸發（適用所有類別，含 health_food 的 UNKNOWN）；
    EXPIRED / CANCELLED / REVOKED / MERGED 不變。有效日期只顯示「有效日期：…（依日期…，非官方狀態）」。
  - 類別感知（`ProductRecord.product_category` / `expiry_date`，`_product_from_row` 讀 0004 view 欄位，缺欄位 = health_food）：
    非健康食品 → PRODUCT_INFO「查有官方查驗登記／核備資料」、字號名稱「查驗登記證號 / 核備函字號」、「登記狀態：官方資料未提供…」、
    「這類官方登記不是健康食品核准，不包含核准的保健功效」；不顯示核准功效、不建立 approved claim。功效詢問視為結構化答案
    （fast path `_details_sufficient`），不落到 planner / RAG / web。健康食品行為不變；claim policy 不因類別改變（醫療宣稱仍 HIGH）。
  - Resolver：使用者字號唯一 → VERIFIED；同一字號對應多筆 → PARTIAL（新 `MULTIPLE_REGISTRATION_RECORDS`）＋候選確認；
    同名 / 跨類別同名 → 既有 MULTIPLE_EXACT_NAMES 候選，不 merge / SAME_AS。候選卡標類別與字號名稱，UNKNOWN 不標失效。
  - NOT_FOUND：`DISCLAIMER_NOT_FOUND` = `NOT_FOUND_NOT_FAKE` =「目前系統查詢的台灣官方資料包含：健康食品、國產維生素類錠狀／膠囊狀食品、
    輸入錠狀／膠囊狀食品。查不到紀錄不代表產品是假貨或違法。」（不聲稱涵蓋一般食品）。
  - Integrity gate（`app/ingestion/product_integrity.py`＋`scripts/check_data_integrity.py`）：不再固定 565；檢查類別已知、來源與類別一致、
    每個來源 products = raw 官方紀錄數、canonical ID = 官方字號推導（health_food `TW:<許可證字號>` 不變）；選填
    `--expect-category <類別>=N`、`--coverage-snapshot <dir>`、`--expect-products N`。Neo4j Product.product_category 與 Supabase 一致
    （0004 marker 存在才是 blocker）。raw 只要求每個產品有 raw link（允許保留舊版本）。
  - Neo4j：`graph_sync` 與 coverage importer 都寫 `product_category`；`migrations/neo4j/0003_product_category.cypher`（index＋
    依健康食品 Source 補 NULL 的 `product_category='health_food'`）只由 coverage `--apply` 在寫入前執行（`prepare_coverage_graph`），未執行。
  - Frozen 檔案變更（使用者本輪明確要求）：`risk_engine.py`（INACTIVE 條件、NOT_FOUND 文字）、`assessment_rules.resolve_product_match`
    （同字號多筆不猜）、`reporter_agent.py`（類別感知文字）、`verification_agent._details_sufficient`。FROZEN_CONTRACTS 未改。
  - 更新舊測試 2 個（NOT_FOUND 文字斷言「不代表是假貨」→「不代表產品是假貨」：test_image_routing、test_response_ux_cleanup）。
  - tests：`tests/unit/test_product_coverage_runtime.py`（49，虛構）；`tests/db/test_tfda_product_coverage_db.py` 新增 runtime 讀取測試；
    `tests/unit` 全部 917 passed；`tests/db` 27 passed（本機 PostgreSQL）。未跑 live integration。
- **Intent-first primary response＋semantic color＋NOT_FOUND semantics（2026-10-04；resolver 門檻 / Claim Policy frozen 行為 /
  Risk Engine 規則 / DB schema / production data 未動；只改 Agent C 回覆組裝、renderer、actions）**：
  - Intent-first：功效詢問 → 標題「核准功效查詢」，第一層只回答功效；產品查證 → 登記狀態；說法查證 → 既有宣稱風險標示；
    成分詢問（已確認產品）→ 成分卡（沒有已整理成分也直接說明）。申請商 / 字號 / 日期放第二層按鈕。
  - 健康食品功效：🟢「官方核准功效」＋功效名稱（每項一點）；按鈕 查看完整功效 / 查看產品資訊 / 查看資料來源（完整原文在完整功效）。
  - 國產 / 輸入類別功效：🟠「未有健康食品核准保健功效資料」＋「查有官方查驗登記／核備紀錄，但這類登記不等於健康食品許可，
    因此不能由這筆資料認定有核准保健功效」（不說產品沒有任何功效）；按鈕 查看產品登記資料 / 查看資料來源。
    新 `ResponseAction.REGISTRATION_INFO`（與官方資訊同內容、下一層只有資料來源）；`OFFICIAL_INFO` 標籤改「查看產品資訊」。
  - 語意顏色（`SemanticTone`、`ResponsePayload.status_line / status_tone / tone`）：GREEN 官方確認 / 核准、ORANGE 資料不足 /
    查無涵蓋紀錄 / 登記≠核准、RED 高風險宣稱。renderer 只替關鍵短語上色（status 行、宣稱風險標示、風險卡標題），內文維持黑字。
  - NOT_FOUND：沒有命題時查無產品 / 查詢失敗 → MISSING_INFORMATION「需要更多資訊」、🟠「未查到對應官方紀錄」（失敗為「暫時無法完成產品查詢」＋重新查詢），
    列出三個涵蓋類別、「查不到紀錄不代表產品是假貨或違法」、請提供完整品名 / 包裝照片 / 證號；無風險燈號。Risk Engine 內部仍 MEDIUM（未改）。
    風險卡的資訊不足標題改為「目前資訊不足，暫時無法確認」（移除「先以中風險看待」）。有命題時沿用說法 / 風險卡。
  - FC-E8：primary 仍最多 3 個重點（多的顯示「還有 N 項」）。FROZEN_CONTRACTS FC-C2 / FC-E1 文字與測試名稱同步更新（使用者本輪要求）。
  - 更新舊測試（被要求改變的行為）：查無產品改 MISSING_INFORMATION（test_adaptive_response、test_response_ux_cleanup、test_agents）；
    功效卡改列功效名稱、原文在完整功效（test_demo_stabilization、test_list_layout_and_fast_paths、test_p1_fastpath_dedup、
    test_context_binding_fonts、test_product_selection、test_product_coverage_runtime）；PRODUCT_INFO「無風險燈」改為無風險用語且
    🟢/🟠 只出現在 status 行（test_claim_verdict_consistency、test_question_fast_path_and_wording）。
  - tests：`tests/unit/test_intent_first_response.py`（12，虛構）；targeted 24 檔 530 passed（未跑 full pytest、未呼叫 LINE validate API）。
- **Taiwan Product Coverage = FROZEN（2026-10-04 Final Freeze Gate）**：
  - Coverage（production）：health_food 565 / domestic_vitamin_tablet_capsule 1705 / imported_tablet_capsule 7407 / total 9677。
  - 維持：**Claim Policy Runtime = FROZEN**、**Product Coverage Runtime = FROZEN**（類別感知、字號解析、UNKNOWN 狀態、NOT_FOUND 涵蓋文字、
    Intent-first 卡片、語意顏色、分類別 integrity gate）。之後修改需使用者明確同意。
  - LINE validate API：14 張（健康食品功效＋完整功效、國產 / 輸入功效＋產品登記資料、NOT_FOUND、查詢失敗、高風險宣稱、
    健康食品 / 國產登記卡、登記類成分卡、跨類別候選 inline、>3 候選 carousel）全部 PASS（虛構資料，含 Quick Reply）。
  - 候選選擇：新增跨類別同名選擇測試（request-scoped、其他使用者拒絕、不重跑 Agent A、沿用原題 intent、依所選類別回答）；selection / webhook /
    adaptive / pending / context 117 passed。ConnectError：reply 時的網路錯誤會讓該 job 失敗並記 `line_job_failed`，queue 繼續（既有測試覆蓋）；
    不重送是既有設計（狀態不明時避免重複），未加特例。
  - Resolver safety audit（只稽核，未改 resolver / threshold）：production 唯讀抽樣 120 產品（exact / 截斷 1–2 字 / 中間錯一字）：
    exact 106 VERIFIED、14 同名 → 候選；NAME_SIMILARITY 自動確認 198 次中 **1 次確認成另一項產品**（錯字讓查詢更接近較短的另一品名，差距 0.057）、
    27 次正確但第二名差距 < 0.10（多為只差一字的姊妹品項，如配方別）；search_products limit 20 沒有漏掉 margin 內的對手（0）。
    `tests/unit/test_resolver_safety_audit.py`：9 個安全行為 PASS＋2 個已知不安全 strict xfail（替換字誤確認、一般品名被包含在多個品名中時自動確認最短者）。
    建議一般化規則（需同意修改 frozen FC-B1 / FC-B2 / FC-B3）：NAME_SIMILARITY 只有在（a）使用者文字依序完整包含於該品名（只有省略、沒有替換字），
    且（b）沒有其他候選也完整包含使用者文字，且（c）第二名 < 0.8 或差距 ≥ 0.10 時才自動確認；其餘改為既有的候選確認卡。
  - Full pytest（含 live integration / DB）：987 passed / 1 failed / 2 xfailed（690s）。唯一 failure 為 `test_line_demo_live.py::test_fifo_order_and_old_card_sources`
    的過期 helper（以按鈕找該題紀錄；虛構品名在正式 DB 查無 → 依上一輪規格為無按鈕的 MISSING_INFORMATION 卡），非 production regression；
    只更新測試 helper（無按鈕時以同一使用者＋altText 結論對應紀錄），重跑該測試：FIFO 斷言通過，之後走既有的條件式 skip（第一題沒有官方來源）。
- **Resolver identity safety（2026-10-04，使用者同意只調整 FC-B1 / FC-B2 / FC-B3；Claim Policy / Risk Engine / schema / production data /
  postback protocol / Agent A prompt / 類別行為未動）→ Taiwan Product Coverage = FROZEN**：
  - 原則：wrong auto-confirm 比多一次確認更危險；分數高於固定門檻不能覆蓋 ambiguity。
  - 強證據不變：唯一字號 / 官方品名 / 官方別名 / 基本品名完全相同 → VERIFIED；名稱＋公司 / 品牌佐證、修正寫法佐證依既有規則。
  - 純 NAME_SIMILARITY 自動確認（`similarity_confirm_blockers`，`app/risk/assessment_rules.py`）三條全部成立：
    A 正規化後使用者文字是官方品名的有序子序列（只有省略、沒有替換字）；B 沒有其他候選品名也包含同一段文字；
    C 第二名 < 0.80（`SIMILARITY_RUNNER_UP_CEILING`）或領先 >= 0.10（`SIMILARITY_SAFE_GAP`）。
    有對手（B / C 不成立）→ MULTIPLE_SIMILAR_NAMES 候選卡（第一名＋對手）；只有替換字、沒有對手 → LOW_MATCH_CONFIDENCE 單一候選確認。
    既有 VERIFIED_MIN_SCORE 0.9 / AMBIGUITY_MARGIN 0.05 未改。候選卡 / postback / 選擇後沿用原題（不重跑 Agent A）不變。
  - 稽核時的兩個 strict xfail（錯字確認成較短的另一項、一般品名被多個品名包含）改為一般 regression test 並通過；姊妹品項只差一字、
    差距 0.096 的截斷查詢（第二名 >= 0.80）現在也走候選確認（依新規則 C，舊測試期待已更新）。
  - FROZEN_CONTRACTS FC-B1 / FC-B2 / FC-B3 已更新（Exact strong evidence 可確認、Fuzzy similarity 在 ambiguity 時必須 clarification、
    不得為命中率犧牲 identity safety）；其他 contract 未動。
  - tests：`tests/unit/test_resolver_safety_audit.py`（16，虛構）＋`test_product_selection.py` 錯字 → 候選卡 → 選擇沿用原題（1）；
    targeted 20 檔 447 passed；`tests/unit` 947 passed。
  - **Full pytest（含 live integration / DB）：995 passed / 0 failed / 1 skipped / 0 xfailed（697s）**。skipped 為 `test_line_demo_live.py`
    第一題（虛構品名查無 → MISSING_INFORMATION）沒有官方來源時的既有條件式 skip（unit test 已覆蓋）。
  - **Taiwan Product Coverage = FROZEN**（health_food 565 / domestic_vitamin_tablet_capsule 1705 / imported_tablet_capsule 7407 / total 9677）；
    **Claim Policy Runtime = FROZEN**、**Product Coverage Runtime = FROZEN**、Resolver identity safety contract（FC-B1/B2/B3）= FROZEN。
- **Product Ingredient Mapping Bridge（2026-10-04；schema audit＋mapping service＋read-only dry-run＋targeted tests；
  未寫 Supabase / Neo4j、未套 migration；Claim Policy / Product Coverage / Resolver frozen 行為未動）**：
  - Corpus：`data/ingredient_interaction/2026-10-04/`（corpus_version 1.0；載入時驗 SHA256SUMS）。只映射 INGREDIENT entity
    （N_* NUTRIENT_GROUP 17、F_* form 13）；D_* drug substance 不在本輪。保留 identity level：魚油 ≠ Omega-3 ≠ EPA ≠ DHA、
    Folate ≠ Folic acid、Calcium ≠ Calcium carbonate；form → group 只用 corpus parent（CONTRIBUTES_TO_NUTRIENT，不是 SAME_AS）。
  - Mapping（`app/ingestion/ingredient_entity_mapping.py`，deterministic、無 embedding / LLM）：
    VERIFIED_EXACT（corpus 英文正式名稱整段相同）/ SUPPORTED_ALIAS（corpus 有來源的同義詞 / 縮寫、人工審核 alias、
    官方原文「X (Y)」一側 exact 且另一側無其他 lineage 線索 → 取較具體者）/ FORM_TO_GROUP（正式 form 推導營養素群）
    才是正式 bridge；AMBIGUOUS（指向互不相關 entity）/ NEEDS_REVIEW（中文譯名完全相同、未 READY alias、alias 部分包含）
    只存候選、不建 CONTAINS。**corpus 中文 alias 全部是 `DISPLAY_ONLY_NOT_AUTOMERGE`** → 中文完全相同也只是候選；
    只有 `ReviewedAlias`（人工審核 overlay，目前 0 筆）能讓中文成為正式 mapping。單一中文字（鈣 / 鋅…）只在名稱主體最後
    才列候選（避免「膽鈣化醇」→ 鈣）；英數邊界不切斷（B1 ≠ B12）。不去鹽名、不去字尾、不做語意合併。
  - 成分來源：health_food = 既有 canonical Ingredient（中文名 / 官方英文名 / OFFICIAL_RECORD 別名；INFERRED 不用）；
    國產 / 輸入 = `raw_data.ingredient_staging.parser_draft.tokens`（token 原樣、nested 內容不拆）。staging READY / NEEDS_REVIEW
    只記為 `source_parse_status`，不影響 mapping。amount：保留 amount_raw；unit 只在「單一數值＋單位」時取；
    amount_normalized 一律 None、amount_basis = UNKNOWN。
  - Runtime payload（`app/services/ingredient_bridge.py`，pydantic、無句子）：`ProductIngredientPayload`（matched_ingredients＋
    contributes_to、candidate_ingredients、bridge_status、evidence_status、missing_information、corpus_version）；
    `DuplicateIntakePayload`（同營養素群 → `DUPLICATE_INGREDIENT_DETECTED`、DUP_* 模板 ID、`intake_amount_assessment =
    NOT_EVALUATED`、缺 AMOUNT_PER_SERVING / SERVING_SIZE / DAILY_SERVINGS / AMOUNT_BASIS；只有候選的產品不參與）。
    尚未接到 Agent B / Agent C / renderer。
  - Schema audit：`ingredients`（uuid5(name_normalized)，TFDA 官方名稱 canonical）與 `product_ingredients`（PK product+ingredient、
    raw_text / amount_text）無法保存 corpus entity、mapping_status / confidence / review_status / 候選 / amount basis /
    corpus_version，放進 ingredients 會讓不同 identity level 碰撞 name_normalized → **需要 additive migration**：
    `migrations/0005_product_ingredient_entity_mapping.sql`（**PROPOSAL，未套用**）：`interaction_corpus_entities`、
    `interaction_corpus_entity_aliases`、`product_ingredient_entity_mappings`（is_formal generated；CHECK：正式 ⇔ 有 entity、
    候選必須 PENDING_REVIEW＋有候選、正式不可待審、FORM_TO_GROUP 必有 via form、normalized 含量需 unit＋basis）。
    Neo4j proposal `migrations/neo4j/0004_ingredient_entity_bridge.cypher`（NutrientGroup / IngredientForm unique）；
    `ALLOWED_LABELS` 已加這兩個 label（尚未寫入）。
  - Dry-run（`python -m scripts.dry_run_ingredient_entity_mapping`，正式 Supabase 唯讀；與唯讀快照結果完全相同；寫入 0）：
    products 9677、至少一個正式 mapping 3650（health_food 47 / 國產 0 / 輸入 3603）、只有候選 3704、無成分 10。
    mapping 單位：VERIFIED_EXACT 4950 / SUPPORTED_ALIAS 5503（其中官方括號 5347）/ FORM_TO_GROUP 4419 / AMBIGUOUS 102 /
    NEEDS_REVIEW 20892 / NO_MATCH 125481。國產 0 正式：成分全中文、corpus 中文只供顯示（911 產品若中文 alias 審核後可能轉正）。
    Neo4j plan：NutrientGroup 17 / IngredientForm 13、CONTAINS 9921、CONTRIBUTES_TO_NUTRIENT 13、SAME_AS 0、INTERACTS_WITH 0。
  - Demo scope（正式 mapping 產品數）：Calcium 951（HF 21 / 輸入 930）、Iron 255（輸入）、Omega-3 523（HF 26 / 輸入 497）、
    Vitamin K 10（輸入）、**Zinc 0、Copper 0**（corpus 沒有鋅 / 銅鹽 form；原料多寫 ZINC OXIDE / 葡萄糖酸鋅 → 候選 1898 / 780）。
    health_food 的「Omega-3 acids」、魚油（中文）、鐵（亞鐵 / 甘胺酸亞鐵）只有候選。
  - tests：`tests/unit/test_ingredient_entity_mapping.py`（26，合成 corpus＋虛構產品；另 1 項讀正式 corpus）、
    `tests/db/test_ingredient_entity_mapping_migration.py`（8，本機 PostgreSQL 0001→0005 重跑、CHECK）；
    targeted 11 檔 297 passed（未跑 full pytest）。
- **Product Ingredient Bridge Replay＋Import Gate（2026-10-04；reviewed overlay v1.0；read-only replay；
  未寫 Supabase / Neo4j、未套 0005；Claim Policy / Product Coverage / Resolver frozen 行為未動）**：
  - Overlay：`data/ingredient_interaction/2026-10-04/overlay/`（由 `REVIEWED_INGREDIENT_ALIAS_FORM_MAPPING_OVERLAY.zip` 的
    `overlay/` 解出；evidence/ 原始頁未放進 repo）。SHA256SUMS 9 檔全部相符；aliases 250 = VERIFIED_OFFICIAL 167 /
    SUPPORTED_AUTHORITATIVE 24（正式可用 191）/ AMBIGUOUS 14 / NEEDS_REVIEW 45；forms 61（沿用 13、新增 48）；
    CONTRIBUTES_TO_NUTRIENT 61、SAME_AS 0。`load_reviewed_overlay` fail closed（缺 SHA256SUMS / 不符、候選有 entity、
    正式無 entity、同字指向兩個 entity、非 CONTRIBUTES_TO_NUTRIENT、允許含量換算、parent 與 corpus 不一致）。
  - 優先序（`classify_surface`）：整段 token 有 overlay alias → 只用 overlay 判定（正式 → SUPPORTED_ALIAS /
    `REVIEWED_OVERLAY_ALIAS_EXACT` / review_status `SOURCE_REVIEWED`；候選 → AMBIGUOUS / NEEDS_REVIEW，即使 corpus 有 exact）；
    overlay 與 corpus 英文正式名稱同 entity → 保留 VERIFIED_EXACT 並記 overlay alias；沒有 overlay alias → 原 corpus 規則不變。
    overlay 只做整段 exact（NFKC / casefold / 空白），不參與括號拆解、部分包含、去鹽名、翻譯合併。
    PRODUCT_FORM_CONTEXT_OVER_BASE_NUTRIENT_ALIAS（Riboflavin / Biotin / 生物素）→ F_*，F_* 只 CONTRIBUTES_TO_NUTRIENT。
  - Replay（`python -m scripts.dry_run_ingredient_entity_mapping --input-json <快照>`；現有 bridge parser；production 唯讀快照
    9677 products / 272 ingredients / 770 product_ingredients，sha256 `e6c60b14…`；同快照另跑 base-only baseline）：
    正式 mapping 產品 3650 → **5968**（health_food 47 → 56 / 國產 0 → 1588 / 輸入 3603 → 4324）；**降級 0**（沒有產品失去
    正式 mapping、沒有正式 → 候選）；輸入 4419 個單位由營養素群細化為同 lineage 的 form（N_B12→F_CYANOCOBALAMIN 等，營養素群不變）。
    mapping 單位：VERIFIED_EXACT 3708（其中 overlay 佐證 3441）/ overlay reviewed alias 23201 / base 同義詞＋官方括號 1424 /
    FORM_TO_GROUP 25999 / AMBIGUOUS 1876 / NEEDS_REVIEW 7436 / NO_MATCH 119283。
    Work 報告的數字是替代 matcher 的基準；本次以現有 parser 為準（國產 1588 相同；輸入 4324 ≠ Work 4206；health_food 56 ≠ 6）。
  - 營養素（正式產品數 / 只有候選；全部類別）：Calcium 1911 / 988、Iron 687 / 456、Magnesium 1001 / 3565（候選主要是
    硬脂酸鎂等賦形劑，overlay 審核為 AMBIGUOUS）、**Zinc 1542 / 353、Copper 652 / 128**、Vitamin D 1486 / 128、Vitamin K 260 / 15、
    Vitamin C 2362 / 124、Omega-3 群 544 / 232（其中 Fish oil F_* 本身 515）。
  - Health-food 保護：565 product IDs 不變（sha256 `c06aee5a…`）、既有 770 ProductIngredient（560 產品）每筆恰好帶入一次、
    raw_text 不變、輸入快照前後相同、寫入 0；overlay 只新增 entity mapping（另一張表），不重建 / 覆蓋 / 刪除既有 Ingredient。
  - 0005 proposal rev 2（未套用）：新增 `interaction_corpus_sources`、`interaction_form_nutrient_links`（只允許
    CONTRIBUTES_TO_NUTRIENT、禁止含量換算 / interaction 繼承、trigger 驗 parent 一致）、view `product_ingredient_entity_contains`
    （importer 只從這裡同步 CONTAINS）；entities 加 graph_label / entity_origin / overlay identity level；aliases 允許 overlay 候選
    （entity_id NULL＋candidate_entity_id）、記 alias_status / source_url / locator / retrieved_at / precedence；mappings 加
    entity_type / relationship_id / alias_evidence_status / overlay_version、`SOURCE_REVIEWED`，CHECK：overlay 候選 alias 不得是正式、
    候選需有候選 entity 或審核 alias。
  - Neo4j preview：NutrientGroup 17 / IngredientForm 61（overlay 新增 48）、CONTAINS 26877（Form 24583 / Group 2294）、
    CONTRIBUTES_TO_NUTRIENT 61（overlay 48 / corpus 13，帶 source / locator）、SAME_AS 0、INTERACTS_WITH 0、Product→Interaction 0、
    Interaction node 0。`migrations/neo4j/0004_ingredient_entity_bridge.cypher` 仍為 proposal。
  - Runtime payload：`MatchedIngredient` / `CandidateIngredient` 加 alias_ids、alias_evidence_status、relationship_ids、
    entity origin、overlay_version；仍只有 enum / ID / 官方原文，沒有最終句子。
  - tests：`tests/unit/test_ingredient_overlay_bridge.py`（32，合成 overlay＋虛構產品；另 1 項讀正式 overlay）、
    `tests/db/test_ingredient_entity_mapping_migration.py`（19，本機 PostgreSQL 0001→0005 重跑＋正式 corpus / overlay 寫入＋CHECK）；
    targeted unit 11 檔 319 passed、`tests/db` 46 passed（未跑 full pytest）。
- **Ingredient Bridge importer＋production read-only compare（2026-10-04；未套 0005、未寫 production；
  Claim Policy / Product Coverage / Resolver frozen 行為未動；未接 Agent B / C；沒有 Interaction runtime）**：
  - Importer：`app/ingestion/ingredient_bridge_import.py`＋`python -m scripts.import_ingredient_bridge`
    （`--dry-run` 預設 / `--compare-supabase` 唯讀 / `--apply --confirm-project nutri_safe_ai`，apply 需 0005 marker、不接受 `--input-json`）。
    順序 A entity → B source＋alias → C form→nutrient → D 正式 mapping → E 候選 mapping → 完成標記
    `ingredient_bridge_import:<corpus>:<overlay>`；Neo4j 只在標記存在後同步（MERGE / SET only）。只寫 0005 五張表＋標記
    （products / ingredients / product_ingredients 寫入會被擋）；不刪除：stale 列或其他版本 mapping → 計畫錯誤、拒絕寫入。
    寫入前 gate（與 DB CHECK 同義）：候選 alias 進正式、正式待審、正式帶候選、未知 entity、FORM_TO_GROUP 無
    CONTRIBUTES_TO_NUTRIENT 關係、候選帶正式 entity…；NO_MATCH 不存。
  - 0005 rev 3（未套用）：加自然鍵 unique index（mapping）、overlay 正式 alias（文字＋版本）unique、正式 / 候選 match_method CHECK、
    trigger `check_entity_mapping_row`（候選不得就地 UPDATE 轉正；正式列不得引用 overlay 候選 alias 或指向其他 entity 的 alias）。
    本輪本機測試發現 rev 2 允許「UPDATE 候選列 → 正式」，rev 3 已擋。
  - Production read-only compare（`--dry-run --compare-supabase`；0005 表不存在 → 視為空表）：would insert entities 78
    （NutrientGroup 17 / IngredientForm 61）、sources 65、aliases 321（base 71＋overlay 250）、form→nutrient 61、
    正式 mapping 54332（VERIFIED_EXACT 3708 / SUPPORTED_ALIAS 24625 / FORM_TO_GROUP 25999）、候選 9312（AMBIGUOUS 1876 /
    NEEDS_REVIEW 7436）；would update 0、would delete 0、collisions 0、invalid formal 0、invalid candidate 0。
    正式產品：health_food 56 / 565、國產 1588 / 1705、輸入 4324 / 7407（共 5968）。
  - Neo4j preview：NutrientGroup 17、IngredientForm 61、CONTAINS 26877（Form 24583 / Group 2294）、CONTRIBUTES_TO_NUTRIENT 61、
    SAME_AS / INTERACTS_WITH / Product→Interaction / Interaction node 0；CONTAINS 帶 mapping_status / raw_text / review_status /
    alias_ids / corpus_version / overlay_version。
  - 本機完整 apply（scratch：pgserver 0001→0005＋production 唯讀快照 9677 / 272 / 770）：第一次寫入 63644 mapping（45.8s），
    第二次全部 unchanged、寫入 0；重複正式 mapping / alias / relationship 0；候選成為正式 0；stearate → N_MG 正式 0；
    products / ingredients / product_ingredients 前後逐列相同；正式產品 HF 56 / 國產 1588 / 輸入 4324。
  - 觀察（未改 matcher）：`VITAMIN C (L-ASCORBYL STEARATE)` 3 個單位經 base 官方括號規則為 N_C 正式（既有規則行為）；
    MAGNESIUM STEARATE / 硬脂酸鎂 / CALCIUM STEARATE 4825 個單位只有候選。
  - tests：`tests/unit/test_ingredient_bridge_import.py`（15，in-memory store）、`tests/db/test_ingredient_bridge_import_db.py`
    （11：idempotent rerun、health_food 不變、候選不轉正（DB 層）、precedence＋CONTRIBUTES_TO_NUTRIENT、版本切換拒絕、
    中途失敗無標記且可補齊、transaction rollback 0 列、Neo4j fake 只在標記後 MERGE）；targeted unit 12 檔 334 passed、
    `tests/db` 57 passed（未跑 full pytest）。
- **Ingredient Bridge Production Import（2026-10-04；0005 rev 3 由使用者於 SQL Editor 套用；Claim Policy / Product Coverage /
  Resolver / Risk Engine / Interaction semantics 未動；沒有 Interaction node / runtime；未接 Agent B / C）**：
  - Preflight（唯讀）：project marker nutri_safe_ai、`migration_0005_product_ingredient_entity_mapping = applied`、products 9677
    （health_food 565 / 國產 1705 / 輸入 7407）；health_food ID / 770 ProductIngredient / 272 Ingredient 與 replay 快照逐列相同；
    bridge 表 0 列；Neo4j 無 bridge label。`--dry-run --compare-supabase`：正式產品 5968（56 / 1588 / 4324）、Zinc 1542、Copper 652、
    delete 0 / update 0 / collision 0 / invalid 0。
  - Importer CLI 改用主鍵排序分頁（`ordered_store`；原 SupabaseCorpusStore range 分頁無 order，大表可能漏列 / 重複）。
  - `--apply --confirm-project nutri_safe_ai`（91s、exit 0）：entities 78、sources 65、aliases 321、form→nutrient 61、
    正式 mapping 54332、候選 9312、完成標記 `ingredient_bridge_import:1.0:1.0 = complete:8fec3ad25a6aeebc`；
    Neo4j：NutrientGroup 17、IngredientForm 61、CONTRIBUTES_TO_NUTRIENT 61、CONTAINS 26877（Group 2294 / Form 24583）。
  - 套用後（唯讀）：重跑 compare 全部 unchanged（insert / update / delete 0）；health_food ID / ProductIngredient / Ingredient digest
    與套用前相同；Neo4j Product 9677、Ingredient 272、既有 CONTAINS→Ingredient 770 不變；SAME_AS / INTERACTS_WITH /
    Interaction node / Product↔Product 0。Supabase：重複自然鍵 0、候選成為正式 0、候選帶 entity 0、未知 entity / alias 0、
    FORM_TO_GROUP 缺關係 0、form 無關係 0、overlay alias provenance 缺漏 0、amount_normalized 0、版本全為 1.0/1.0。
    Neo4j ↔ Supabase：entity ID 集合相同、CONTAINS 配對 = 正式直接 mapping、CONTRIBUTES 配對 = links、CONTAINS 缺 provenance 0、
    候選配對進圖 0。
  - Provenance 抽查（Product → raw → alias（source）→ form → CONTRIBUTES_TO_NUTRIENT（source）→ group）：碳酸鈣 → F_CA_CARBONATE → N_CA
    （988 產品）、ZINC OXIDE → F_ZN_OXIDE → N_ZN（516）、葡萄糖酸鋅 → F_ZN_GLUCONATE → N_ZN（653）、葡萄糖酸銅 → F_CU_GLUCONATE → N_CU（307）、
    氰鈷胺明(維生素B12) → F_CYANOCOBALAMIN → N_B12（1960）；alias 來源 TFDA_ADDITIVES_61（B12 關係來源 ODS_VitaminB12）。
    魚油只到 F_FISH_OIL（EPA / DHA 是各自的健康食品 Ingredient）；F_FOLIC_ACID 1747 vs N_B9 3 分開；stearate → N_MG / N_CA 正式 0
    （NEEDS_REVIEW 2954＋AMBIGUOUS 1384 → N_MG 候選、493 → N_CA 候選）。
  - Runtime retrieval（`app/services/ingredient_bridge.result_from_stored_rows`：已存列 → 既有 payload，不重新比對、版本不符拒絕；
    payload 新增 `official_raw_ingredients` / `unresolved_raw_texts`）。Production 唯讀 smoke 6 例（health_food 既有成分、國產、輸入、
    鋅 form、銅 form、未解析＋已確認）：已存列重建 = 即時重算、payload 無句子、未解析 / 候選不阻擋正式成分（皆 INGREDIENTS_MAPPED）。
  - tests：targeted unit 12 檔 336 passed、`tests/db` 57 passed、live 唯讀 `test_data_stores_live`＋`test_supabase_connectivity` 7 passed
    （未跑 full pytest）。
- **Ingredient Retrieval Runtime Integration（2026-10-04；Ingredient Bridge production data / Claim Policy / Product Coverage /
  Resolver / Risk Engine 規則 / Interaction 未動；只讀 Supabase）**：
  - Tool：沿用 `lookup_product_ingredients`（`app/tools/ingredient_tools.py`＋`evidence_tools.h_product_ingredients`，參數新增
    `ingredient_queries`）。輸出 `ProductIngredientEvidence`（`EvidenceBundle.product_ingredients`，新選填欄位）：
    official_ingredients（A 官方原文）/ confirmed_ingredients（B：只有 VERIFIED_EXACT / SUPPORTED_ALIAS / FORM_TO_GROUP，
    form 附 parent_nutrient、FORM_TO_GROUP 附 via form）/ unresolved_ingredients（C：AMBIGUOUS / NEEDS_REVIEW / NO_MATCH）/
    coverage / query_answers / source；沒有任何句子。原有 `bundle.ingredients`（health_food IngredientRecord）不變。
  - 資料：health_food = 既有 product_ingredients；國產 / 輸入 = raw_import_records.raw_data.ingredient_staging token（另存官方成分欄位原文）；
    mapping = product_ingredient_entity_mappings（只用 `ingredient_bridge_import:<corpus>:<overlay>` 完成標記的版本；多個完成版本 → 不猜、
    視為 bridge 不可用）；entity / alias 小表 process 內快取 600 秒。Supabase repo 新增 3 個唯讀方法。
  - 成分是非題（Agent A 的 ingredient_candidates）：使用者寫法以 NFKC / 大小寫 / 空白標點＋「維他命→維生素」正規化後，
    與 entity 正式名稱 / 正式 alias 完全相同（找不到才用顯示譯名）→ 唯一 entity；多個 → AMBIGUOUS 不猜。
    `IngredientPresence`：CONFIRMED（entity 有正式 mapping；營養素群可經 FORM_TO_GROUP）/ LISTED_IN_OFFICIAL_TEXT（官方原文完全相同，
    不是 identity）/ CANDIDATE_ONLY（只有候選指向它，例如硬脂酸鎂 → 鎂）/ NOT_CONFIRMED（附 related_confirmed：同營養素的其他形式；
    unresolved_remaining > 0 時不可說確定沒有）。只出現在品名中的成分字樣不當是非題（品名以外又問到則照常回答）。
  - Agent B：fast path `_details_sufficient`（INGREDIENT_INQUIRY）改為「有既有成分或有官方原始成分」即足夠——部分成分未識別不算缺資料；
    官方完全沒有成分資料才照原規則走完整流程。`_guardrail_product_details` 帶入是非題寫法並把證據放進 bundle。
  - Agent C（primary 仍 deterministic，FC-A3）：依問題語意由結構化事實組句——「有哪些」：官方列有 N 項（有部分已確認時才說明幾項已確認 /
    尚未識別），已確認的先列並附「X 的來源形式」；「有沒有」：CONFIRMED「有。…」、CANDIDATE_ONLY「目前無法確認…不能視為含有」、
    NOT_CONFIRMED＋相關成分「…不是同一種成分」＋「另有 N 項尚未完成識別，不能完全排除」。status：官方登記成分 / 已確認含有 /
    官方成分列有（GREEN）、目前未能確認（ORANGE）。不顯示內部 entity ID。完整分析（LLM）輸入新增 `product_ingredients`（無 ID）＋規則 14。
  - Flex：primary 仍最多 3 點（FC-E8）；「查看完整成分」= 官方成分逐項標示（有已確認時）＋官方成分欄位原文，多行分段、最多 40 項；
    下一層「查看資料來源」（`DETAIL_FOLLOW_UPS[INGREDIENTS]`）。`EvidenceBundle.all_sources()` 加入成分來源。Renderer 未改。
  - Prompt：Agent A「有沒有 / 含不含某成分」= ingredient_inquiry＋ingredient_candidates、不是 claims（live：4 種問法皆正確、「可以降血糖嗎」仍為命題）。
  - Frozen 檔案變更（本輪需求直接要求的 fast path routing 與成分卡內容，既有 extension point 無法達成）：`verification_agent`
    `_details_sufficient` / `_guardrail_product_details`、`reporter_agent` INGREDIENT_LIST 的摘要 / status / 成分明細（select_response_type 未改）、
    `actions.DETAIL_FOLLOW_UPS`（safe extension point）。FROZEN_CONTRACTS 未改。
  - tests：`tests/unit/test_ingredient_retrieval_runtime.py`（21，虛構 entity / 產品）；`test_image_context_sources` 的無效網址測試
    列舉補上新的成分證據來源（行為不變）。targeted 28 檔 612 passed；live 唯讀 `test_data_stores_live`＋`test_supabase_connectivity` 7 passed；prompt 變更後 `test_openai_live`＋`test_line_demo_live`
    13 passed / 1 skipped（既有條件式 skip：虛構品名查無 → 無官方來源）。未跑 full pytest。
  - Production READ-ONLY smoke（產品由 DB 隨機挑選，固定 intake、不呼叫 OpenAI、不寫 log）：health_food 無 mapping（列既有成分）/
    health_food 魚油（EPA / DHA 各自為 Omega-3 來源）/ 登記類「有鋅嗎」CONFIRMED（ZINC GLUCONATE）/ 67 項成分「有銅嗎」CONFIRMED /
    硬脂酸鎂「有鎂嗎」CANDIDATE_ONLY / 魚油「有 EPA 嗎」NOT_CONFIRMED＋相關成分 / 「有葉酸嗎」CONFIRMED；全部 VERIFIED＋structured_fast_path、
    1.6–2.8 秒、無 planner / RAG / web、卡片與明細無內部 ID。
  - LINE validate API（只驗證不送出）：成分清單 / 有 / 候選 / 未確認 / health_food 清單 / 完整成分 / 61 項長明細 共 7 張 PASS。
- **Candidate Relevance Gate（2026-10-04；手機 smoke 發現：未知辨識名稱＋常見營養素 → NAME_SIMILARITY 列出不相關候選；
  exact / alias / 字號 / 基本品名 / 修正寫法 / 自動確認 0.9 與 margin / postback / Ingredient Bridge / Agent A 未動）**：
  - Root cause：resolver 只防 unsafe auto-confirm；候選產生 / 顯示只看名稱相似分數，一般字樣（維生素A、魚油…）重疊就 >= 0.5。
  - `app/risk/candidate_relevance.py`＋`assessment_rules.apply_candidate_relevance_gate`（resolver 結果的後置過濾，只處理
    PARTIAL_MATCH＋NAME_SIMILARITY；只移除候選、不確認任何產品；多候選只剩一項 → 單一候選確認）。一般用語 = 固定劑型 / 類別 / 規格寫法、
    維生素寫法、單字礦物質、「牌」＋DB 官方成分名稱與 Ingredient Bridge entity / alias 名稱（`get_identity_generic_terms`，快取）＋
    候選本身中「不同申請商、不同品名」共用的字樣。逐字對齊（LCS）只數 distinctive 字：對上 >= 沒對上且 >= 2 字、或只差 1 字（錯字）、
    或使用者寫法是官方品名省略字、或候選含使用者明確給的品牌 / 公司 → 保留。distinctive < 2 字（一般查詢）→ 不擋。
  - 全部移除 → 有公司 / 推測品牌時同既有 COMPANY_ONLY，否則 NOT_FOUND＋`ProductMatch.not_found_reason = NO_PRODUCT_IDENTITY_SUPPORT`
    （`unsupported_identity_terms`、`filtered_candidate_count`，log 只記代碼與數量）。Agent B 新 `structured_identity_not_found`：
    沒有命題 / 網址時直接停止（違規廣告照查），不跑 planner / RAG / web。Agent C 在既有查無卡摘要說明哪段名稱沒有官方支持。
  - FROZEN_CONTRACTS 新增 FC-B7（additive）；其他 contract 未改。Frozen 檔案變更：`assessment_rules`（後置 gate）、`verification_agent`
    （新 stop、resolver 傳入一般用語）、`reporter_agent`（查無摘要句）。
  - Production READ-ONLY audit（lookup_product＋resolver，前後對照）：一般營養素查詢 5 例不變；未知前綴＋營養素 25 例：原本 24 例有候選 → 1 例
    （官方品名恰為該營養素名稱、對齊平手 → 仍是候選確認，不自動確認）；品牌前綴＋近似品名：原本含同前綴產品 11 例 → 11 例保留、自動確認 0；
    一字錯字 60 例：正確產品仍在候選 60/60、自動確認 0、其中 25 例移除不相關對手；同名多筆不變。過程中修正兩個一般化問題
    （跨申請商共用字樣規則吃掉錯字的正確產品 9/60 → 改為逐字對齊＋1 字容忍；同名不同申請商寫法被誤當共用字樣 → 要求不同品名）。
    端到端（真實 Agent B，固定 intake）：未知前綴＋維生素A / 魚油 → structured_identity_not_found、0 候選、無 planner / RAG / web、1.5s / 0.4s；
    「維生素A」→ 3 候選確認卡；3 張 Flex 通過 LINE validate API。
  - tests：`tests/unit/test_candidate_relevance_gate.py`（13，虛構）；targeted 34 檔 758 passed；live 唯讀 data stores 7 passed；`test_line_demo_live` 5 passed / 1 skipped（既有條件式 skip）。未跑 full pytest。
- **Product-Core Relevance Gate（2026-10-04，取代上一節 gate 的支持判斷；resolver 門檻 / exact / alias / 字號 / 自動確認 / postback /
  Ingredient Bridge / Agent A 未動）**：
  - Root cause：上一版 gate 讓品牌 / 公司 anchor 單獨足以保留候選（明確品牌包含即通過；或品牌字數抵過缺少的核心字），
    且「名稱＋品牌佐證」的單一候選（NAME_COMPANY_CORROBORATED）未經 gate → 同品牌、不同產品核心的產品成為唯一候選。
  - 規則（`app/risk/candidate_relevance.py`）：使用者寫法逐字分 FORM（劑型 / 規格 / 數量 / 牌 / 食品，忽略）、NUTRIENT（維生素寫法、
    單字礦物質、類別用語、DB 官方成分與 ingredient entity 名稱；必須完整對上，維生素 A ≠ C）、DISTINCTIVE（品牌與產品核心描述）。
    逐字對齊候選「官方品名｜申請商」：NUTRIENT 缺字、DISTINCTIVE 連續缺 >= 2 字、或缺字超過核心 20%（至少容忍 1 字）→ 不符；
    使用者寫法是官方品名省略字 → 符合。品牌＋品名分開時以「品牌＋品名」組合檢查。移除品牌 anchor 捷徑與「跨申請商共用字樣」規則。
  - Gate 範圍擴大到未確認的 NAME_COMPANY_CORROBORATED；VERIFIED 不經過。全部移除 → NOT_FOUND（NO_PRODUCT_IDENTITY_SUPPORT），
    `unsupported_identity_terms` = 最接近候選缺少的片段；Agent C 摘要改為「名稱相近的官方紀錄都對不上你寫的「…」；只是品牌相同或成分、劑型字樣相同的產品不會列為候選」。
  - 行為變更：已知品牌＋該品牌沒有的核心描述（上一節的「品牌＋近似品名」）現在不列同品牌其他產品（依本輪規格）；品牌＋省略字 / 同核心仍可列。
  - Production READ-ONLY audit：由資料找品牌前綴（同申請商兩個品名的共同開頭）60 組——同品牌＋他廠核心：原本 25 組顯示同品牌其他產品 → 0；
    同品牌＋同核心：目標保留 58/58（同 gate 前）；未知前綴＋營養素 25 → 0 候選（上一版平手案例也解決）；一字錯字 60/60 保留、自動確認 0；
    一般營養素查詢 / 同名多筆不變。
  - tests：`tests/unit/test_candidate_relevance_gate.py` 改寫（17，虛構品牌 NUTRIX / ZENITH）；targeted 34 檔 762 passed；live data stores 7 passed、
    `test_line_demo_live` 5 passed / 1 skipped（既有條件式 skip）；端到端未知前綴 → structured_identity_not_found、3 張 Flex 通過 LINE validate。未跑 full pytest。
- **Phone smoke 回歸修正：exact / 產品 vs 成分路由 / 短品名錯字（2026-10-04；resolver 門檻 / 自動確認 / postback / Ingredient Bridge /
  Product-Core gate 判斷未放寬；Ingredient Runtime 尚未 freeze）**：
  - 診斷（唯讀 analysis_requests / evidence_logs，當日 40 筆 LINE 紀錄）：
    1. Case 1 該筆（品牌＋螯合鋅錠＋有鋅嗎）：Agent A 完整保留品名；但 Supabase products / raw 欄位 / alias 都沒有這個品名，該品牌唯一產品是
       另一核心（B群＋植物萃取）→ exact / alias / base 沒命中、相似度唯一候選即同品牌不同核心 → gate 移除 → NOT_FOUND，符合 Product-Core 規格。
       exact 不經 gate 的契約本來就成立（gate 只處理未確認的 NAME_SIMILARITY / NAME_COMPANY_CORROBORATED），已加 spy 測試鎖定。
       另修正缺少片段的顯示（對齊巧合造成「br」半個詞 → 顯示整段；營養素整段顯示）。
    2. Case 2（營養素＋膠囊）：Agent A 只抽出成分、intent=unclear、沒有產品 → planner 跑 lookup_ingredient（模糊比對回傳其他維生素）→
       reporter 以「官方資料中找到以下成分」列出不相關成分。
    3. Case 3（3 字短品名一字錯字）：Agent A 沒抽出任何實體（intent=unclear）→ resolver 根本沒有執行；直接送 resolver＋gate 會保留正確候選
       （production 422 個短品名替換測試 0 遺失）。根因是路由，不是 gate。
  - 修正（一般化）：
    * Agent A prompt：只有一個名稱的訊息、「成分＋劑型」片語 → product_candidates / product_verification。deterministic 保險
      `apply_product_phrase_routing`（文字 / 語音、無任何產品識別 / 命題 / 網址 / 代名詞時）：成分＋（≤6 字）＋劑型 → 產品候選；
      intent unclear＋沒有實體＋沒有問句線索的單一名稱 → 產品查詢。明確成分問題（X 是什麼、X 的功效）不受影響。
    * `EvidenceBundle.standalone_ingredients`（新）：lookup_ingredient 結果與「已確認產品的成分」（bundle.ingredients）分開；reporter
      不針對產品時只列與所問名稱完全相同的官方成分（「這不是指任何一項產品的成分」），沒有就說找不到完全相同名稱；成分名稱查詢結果
      永遠不會出現在產品成分卡 / 完整成分。
    * Gate：明確的單字編輯（替換 / 多一字 / 少一字）規則——只差一字 → 保留為候選（仍只請使用者確認）；改動的字若是營養素字樣仍是衝突。
  - Production READ-ONLY 決策路徑（真實 Agent A＋B＋C，不寫 log）：exact 官方品名＋成分問題 → CANONICAL_EXACT / fast path / 成分卡；
    官方別名 → N/A（production PRODUCT 官方別名 0 筆，synthetic 覆蓋）；字號 → PERMIT_NUMBER；「營養素＋膠囊」→ 產品（DB 有同名產品 → exact）；
    「營養素是什麼」→ 成分查詢、無完全相同官方成分 → 不列其他成分；短品名一字錯字 → 2 候選確認（含正確產品）；長品名一字錯字 → Agent A
    在 product_candidates 直接寫成修正後官方品名 → CANONICAL_EXACT（既有 Agent A 行為，未改）；未知品牌＋營養素 → identity_not_found；
    同品牌不同核心 → identity_not_found；同品牌同核心 → exact。手機原句重播：短品名錯字 → 1 候選確認；營養素＋膠囊 → 產品；其餘同上。
  - tests：`tests/unit/test_product_routing_and_typos.py`（16，虛構）；targeted 37 檔 800 passed；live（openai＋line demo＋data stores＋
    connectivity）20 passed / 1 skipped（既有條件式 skip）。未跑 full pytest。
- 最新完整 pytest（2026-10-03，LINE 整合後）：466 passed / 7 skipped / 0 failed（當時 DB 尚未 seed）。
- 2026-10-03 seed 後（未改 production code，未重跑完整 suite）：`test_data_stores_live.py` 6 passed（原本 skip）、
  `test_line_demo_live.py` 6 passed。
- 產品選擇後（未跑完整 suite）：selection / postback / context / renderer / resolver 相關 unit 150 passed（6 個檔案）。
- P1 後（未跑完整 suite）：resolver / fast path / dedup / semantic / parity / LINE / agents / tools / risk 相關 unit 276 passed（12 個檔案）；
  `test_line_demo_live.py` 6 passed（446s）。

## Ingredient Runtime Freeze（2026-10-05）

**Final freeze gate PASSED**：
- 先前 88 個 error 為 Windows pytest 暫存目錄 PermissionError（非 production regression）；重跑該批 131 passed。
- Full pytest（fresh basetemp）：**1202 passed / 1 skipped / 0 failed / 0 errors**（6 個 Supabase deprecation warnings，本次不處理）。

目前 FROZEN：
- **Product Identity Safety = FROZEN**（product mention provenance、corrected name 只能候選確認、Clarification before research、台灣登記進口產品不算外國產品；FC-B8 / B9 / B10）
- **Candidate Relevance = FROZEN**（FC-B7）
- **Ingredient Bridge Data Layer = FROZEN**（0005 migration / importer / 1.0 corpus＋overlay；production 已匯入）
- **Ingredient Retrieval Runtime = FROZEN**（產品成分三層、「有沒有某成分」答案、standalone identity / function evidence 分離；FC-H1 / H2 / H3）
- **Standalone Ingredient/Nutrient Routing = FROZEN**（原文優先於前文產品、代名詞仍綁定、collision 詢問、獨立成分「查看完整分析」；FC-F4 / FC-E9）
- 維持：Claim Policy Runtime、Product Coverage Runtime、Resolver identity safety（FC-B1/B2/B3）= FROZEN。
- 已知（不影響 freeze）：Controller 對未收錄裸名稱偶發判成產品時，已由原文優先規則攔下（一般成分用語）；Supabase deprecation warnings 待後續處理。
- 文件用語：Controller Agent / Researcher Agent / Reporter Agent（Agent A/B/C 為內部識別字，程式未改名）。

## Interaction Runtime（Phase 1 資料層 production / Phase 2 Researcher tools，2026-10-05）

**Phase 1 資料層（production / VERIFIED）**：0006 已由使用者套用；`import_interaction_corpus --apply` 已執行（marker `interaction_corpus_import:1.0`）。
Supabase：15 DrugSubstance / 25 pairwise Interaction / 17 DuplicateIntakeRule / 67 participants / 60 evidence，其中 12 筆 pairwise
`deterministic_conclusion_allowed`（`interaction_conclusion_eligible` view）；Neo4j：`HAS_EVIDENCE`（中性關係，方向在 `supports_direction`）、
`FROM_SOURCE`、`PARTICIPATES_IN`，沒有 Bridge 節點被新建。Neo4j `CONTAINS` 基線（唯讀確認）：Product→Ingredient 770 ＋
→IngredientForm 24583 ＋ →NutrientGroup 2294（成分 Bridge 兩類共 26877）＝ 27647。

**Phase 2 Researcher tools（本輪；未改 schema / production data / frozen identity、resolver、ingredient 語意）**：
- Controller（Agent A）：新 `Intent.INTERACTION_INQUIRY`；`IntakeExtraction.interaction_subjects` / `interaction_mode`
  （pairwise / duplicate_comparison）→ `NormalizedUserRequest.interaction_subjects`。對象名稱必須逐字出現在使用者原文，否則丟棄
  （LLM 不能把名稱翻成英文 / 別的 identity）；產品對象只放 `interaction_subjects`、不放 `product_candidates`（逐一由 Researcher 確認）。
- Researcher（Agent B）：`VerificationAgent.verify()` 最前面對 interaction intent 分派到 `app/agents/interaction_research.py`
  （其他 intent 流程不變；要改 frozen 檔 verification_agent.py 的原因：路徑分派 / 「結構化證據足夠就不跑 RAG / 官方網站」屬於控制流程，
  evidence tool registry 這個 extension point 無法表達；只加 constructor 參數、mixin 繼承與 6 行分派）。
  流程：逐一確認對象 identity（成分 = Bridge exact、藥品 = corpus DrugSubstance exact、產品 = 既有 deterministic resolver 各自解析）→
  任一未確認 → `structured_clarification`（不做 interaction research；未確認產品以 `bundle.product_match` 呈現候選，沿用既有確認卡）→
  全部確認 → `INTERACTION_TOOLS`（`lookup_interactions` / `lookup_duplicate_ingredients` / `lookup_interaction_evidence`；
  輸入 canonical ID、strict schema、guard、不在 LLM planner allowlist）。
- 結論資格（`app/services/interaction_service.py`，deterministic、不產生句子）：`InteractionConclusionStatus` =
  CONCLUSION_ELIGIBLE（只有 corpus `deterministic_conclusion_allowed` ＋ 有支持的 DECISION_ELIGIBLE evidence ＋ identity 直接對應，
  仍附 applicability 條件原文、`evaluated = False`）/ LINEAGE_NOT_INHERITED / EVIDENCE_CONFLICT / NEEDS_REVIEW /
  INSUFFICIENT_FOR_CONCLUSION。**lineage 是資料驅動**：產品只經 form → group 對應、而 Bridge link 的
  `automatic_interaction_inheritance` 不是 true（目前全是 false）→ 只說明、不肯定（判斷題：若要放寬，改 link 資料而非程式）。
  INT_FISH_WAR 維持 EVIDENCE_CONFLICT / CONFLICTING / 不 eligible。NO_CORPUS_RECORD ≠ 沒有交互作用。
- Evidence：逐筆保留 source / `supports_direction` / `evidence_use` / `is_official` / 取得狀態；`direction_role` 為 deterministic 分類
  （NO_SIGNIFICANT_EFFECT 等 `counts_as_support = False`；未知新值 UNCLASSIFIED = 不支持）。
- 重複成分（產品 ↔ 產品）：只經 Bridge 已確認的 form / group（form → group lineage；不用 display string）→
  `DUPLICATE_INGREDIENT_DETECTED`，一律 `dose_evaluation = NOT_EVALUATED`、`severity = DOSE_DEPENDENT_NOT_ASSIGNED`，
  `prohibited_inferences` 列出不得推論的事項（OVERDOSE / EXCEEDS_UPPER_LIMIT / TOXICITY / UNSAFE_COMBINATION…）；
  候選 / 未解析成分不參與，`duplicate_coverage` 回報每個產品尚有幾個官方成分未識別。沒有 Product↔Product edge / record。
- DrugSubstance：只支援 corpus 已知 canonical identity（英文 canonical / 來源同義詞 exact）；中文譯名 / 商品名 / NEEDS_REVIEW 的官方中文名
  不用於比對 → `DRUG_ACTIVE_SUBSTANCE_UNMAPPED` → clarification。**尚未**提供台灣藥品商品名 → active ingredient 對應。
- 資料不足才依既有 Researcher policy：只有 corpus 完全沒紀錄（NO_CORPUS_RECORD）才補 RAG 一次 ＋（必要時）官方網站一次；結果不會變成 interaction 結論。
- 候選選定：`request_for_selected_product` 對 interaction request 只固定那一個產品對象（`pinned_permit_number`，以名稱相似度選最接近的未固定產品對象），
  其餘對象不變，不重跑 Controller。
- 已知限制：兩個產品必須都以名稱 / 字號寫在同一句（「這兩個」指代前文兩個產品不支援）；產品↔產品 pairwise 目前也會同時查重複成分。
- tests：`tests/unit/test_interaction_runtime.py`（40）；fixture `tests/fixtures/interaction_data.py`、`InMemoryInteractionRepo`（tests/fakes.py）。

**Phase 3 Reporter Agent ＋ LINE UI（2026-10-05；未改 DB schema / production data / eligibility 語意 / 其他 frozen runtime）**：
- 新 `ResponseType.INTERACTION_RESULT`（無風險燈號；產品風險與 interaction finding 分開，不會因 finding 把產品標 HIGH）、
  `ResponseAction.INTERACTION_EVIDENCE`（查看證據來源）/ `INTERACTION_MAPPING`（查看成分對應）。
  Reporter 邏輯集中在 `app/agents/interaction_reporter.py`（deterministic；只讀 `bundle.interaction_research`，不查資料、不新增證據）。
- Primary card（一則 Flex、最多 3 點、每點 ≤ 78 字，Renderer 不會截斷關鍵限制；status 短語一律 ORANGE，沒有 🟢/🔴）：
  * CONCLUSION_ELIGIBLE →「有可用的交互作用證據」＋資料庫記載的影響；一律說明「是否適用於你的實際劑量、用法與族群，目前無法自動判斷」
    （applicability.evaluated = false）。不寫「不能一起吃」。
  * LINEAGE_NOT_INHERITED →「目前不能直接套用交互作用結論」：說明產品成分形式可追到某營養素類別，但資料不允許自動繼承。
  * EVIDENCE_CONFLICT →「目前證據不一致」；NEEDS_REVIEW / INSUFFICIENT →「目前證據不足以確定」；皆不下肯定結論。
  * NO_CORPUS_RECORD →「目前交互作用資料庫沒有收錄這組關係」＋「未收錄只代表資料庫尚無紀錄，不能當作可以放心一起使用的依據」
    （不出現「沒有交互作用」）；補查過官方文件時只說「僅供參考，不能當結論」。
  * 重複成分 →「重複成分提醒」：「兩項產品都含有「X」；尚未完成劑量評估」＋尚有 N 項成分未完成辨識；成分形式不同另標示。
    不出現過量 / 超過上限 / 有毒 / 不能一起吃 / 一定有風險。
  * pairwise ＋ 重複成分同時存在 → 同一張 primary：重點 1 = 最重要的 interaction 結果、重點 2 = 重複成分、重點 3 = 限制 / 不確定性。
  * 對象未全部確認 → 先出既有產品候選確認卡（MISSING_INFORMATION，不顯示任何 interaction 結論）；藥品無有效成分對應 →
    「請補充藥品的有效成分」（不說已能辨識所有台灣藥品商品名、不猜）；corpus 不可用 → 如實說明暫時查詢不到（附「重新查詢」）。
- 按鈕（最多 3）：查看完整分析 / 查看證據來源 /（有已確認產品才有）查看成分對應；全部 request-scoped postback（同 user + 同 request_id）。
  * 證據來源：逐筆列出標題 / 官方或非官方 / 證據方向白話；NO_SIGNIFICANT_EFFECT →「未發現明顯影響（不支持有交互作用）」、
    MIXED →「結果不一致（不能視為支持）」、CONTEXT_NOT_PAIR_SPECIFIC →「背景資料，不是這一組的直接證據」；未知新值 →「方向尚未分類，不當作支持」；
    重複成分規則的證據標為「支持這項重複成分的說明」。下一層沿用既有 SOURCES（可點擊來源卡）。
  * 成分對應：官方成分 → 成分形式 → 營養素類別（只列已確認的正式對應；未辨識成分只計數）。
    Researcher 在 `SubjectResolution.ingredient_mappings`（新增可選欄位）保存這份對應。
  * 完整分析：Reporter LLM 只收到 `interaction_research` 的 structured facts（白話名稱，無內部 ID / enum）；不重跑 Controller、不重新查 corpus / RAG / web。
    LLM 正文過 guard（內部代碼、禁止說法、無可肯定結論時不得斷言存在交互作用），不合格或 LLM 失敗改用固定模板；
    標題、限制（適用條件尚未評估 / 劑量未評估 / 未收錄意義 / 非醫療建議）一律由程式附上。
- frozen 檔案的最小修改（extension point 不足的原因：回覆型態選擇 / compose / 候選確認屬於既有流程的控制分派，無法只靠新增 detail action 表達）：
  * `app/agents/reporter_agent.py`：`select_response_type` / `compose_payload` / `report` / `detail` 各一段 interaction 分派；其他 intent 流程不變。
  * `app/services/clarification.py`：`request_for_confirmed_target` 對 interaction 題改走 `request_for_selected_interaction_product`
    （只固定該產品對象；否則「是」會讓同一個低信心候選再被確認一次）。
  * `app/response/actions.py`（標籤 / DETAIL_FOLLOW_UPS）、`app/response/line_renderer.py`（TYPE_LABELS / TEMPLATES 註冊）；`service.py` 未改。
- **Answer-first Interaction UI（手機 smoke 後；只改 Controller 問題型態欄位＋Reporter / response 呈現，corpus / Researcher / eligibility / Risk 未動）**：
  * Controller 新增結構化欄位 `interaction_question`（together_use / interaction_existence / explanation；Prompt 以語意判斷，沒有關鍵字規則；
    沒給時視為 together_use）；重複成分問題仍由 `interaction_mode = duplicate_comparison` 決定。
  * Primary card：標題 = 直接回答；summary = 一句原因（eligible 用 clinical_effect）；重點 ≤ 2（重複成分摘要 / 限制）；
    證據筆數、來源、證據方向、技術說明、「官方資料來源 N 筆」一律不在 primary。
    together_use：ELIGIBLE / LINEAGE →「目前不能直接判定是否適合一起使用。」（適用條件未評估，絕不說可以 / 不可以）；
    CONFLICT →「目前無法確定是否適合一起使用，因為現有證據不一致。」；NEEDS_REVIEW / INSUFFICIENT →「目前資料不足，無法確定…」；
    NO_CORPUS_RECORD →「目前資料不足，不能判定兩者是否可以一起使用。」。interaction_existence：ELIGIBLE →「有，資料庫收錄了 A 與 B 的交互作用。」，
    其餘「無法確定 / 資料不足 / 資料庫沒有收錄這組關係，無法判定是否有交互作用」；explanation：ELIGIBLE →「資料庫對 A 與 B 的說明如下。」。
    重複成分：有 →「有，兩項產品都含有「X」。」＋「目前尚未評估兩項產品的實際攝取量。」；沒有 →「以已確認的成分比對，沒有找到重複的成分。」＋未辨識覆蓋。
    status 短語：「需要確認實際用量與使用方式」/「現有證據不一致」/「資料不足」/「資料庫未收錄這組關係」/「尚未評估實際攝取量」（一律 ORANGE，無風險燈）。
  * 按鈕（≤ 3、request-scoped）：為什麼？（deterministic：資料庫記載的影響 / 適用條件 / 尚未評估，不用 LLM）/ 查看證據 / 查看成分對應。
    「查看完整分析」（LLM 完整分析）不再放在 primary；程式保留（`ReporterAgent.detail(DETAIL)`），postback 未提供時被拒絕。
  * tests：`test_interaction_reporter.py` 改寫為 answer-first（eligible / conflict / no-record / lineage × 三種問法、duplicate 有 / 無、
    適用條件未知不變成可以 / 不可以、primary 無證據洩漏、證據在按鈕後）；targeted 13 檔 311 passed（含 LINE validate API 41 則 Flex）。
- **Task-context safety patch（Phase 3 後）**：兩個以上產品對象的 interaction 題（`pipeline.is_multi_product_comparison`，以 Researcher 的
  已確認 / 待確認產品對象計數）不再把 `bundle.product_match` 的任一產品寫成 confirmed_product，也不留產品實體；
  `TaskContextStore.record(clear_products=True)` 同時清掉 confirmed_product、current_product 與更早的產品指稱，
  所以比較後的「它有哪些成分？」→ MISSING_INFORMATION（不悄悄選任一產品、也不退回更早的產品）；下一題明確寫產品照常；
  單一產品 ＋ 藥品維持既有單一產品 context。修改處：`service.py` / `context_store.py`（frozen，只加參數與守門）、`pipeline.py`。
  tests：`tests/unit/test_interaction_context_safety.py`（6，已確認移除守門後 4 個會失敗）。尚未提供台灣藥品商品名 → 有效成分對應。
- **Production READ-ONLY 手機 smoke 候選（2026-10-05，真實 Controller ＋ Supabase / Neo4j，未寫 log）**：
  * 1 eligible 成分↔成分：「鋅跟銅可以一起吃嗎」「鐵和鋅可以一起吃嗎」→ pairwise；RESOLVED×2；COMPLETE；INT_ZN_CU / INT_FE_ZN ELIGIBLE；
    標題「有可用的交互作用證據」＋適用條件尚未評估；按鈕 完整分析 / 證據來源；只 lookup_interactions（無 RAG / web）；5–10s。
  * 2 eligible 成分↔藥品（canonical 名稱）：「維生素K跟Warfarin可以一起吃嗎」「鈣跟Levothyroxine可以一起吃嗎」→ 同上（INT_K_WAR / INT_CA_LT4）；6–8s。
  * 3 重複成分（產品↔產品，兩者皆 VERIFIED、只用正式 mapping）：「檸檬酸鈣細顆粒跟高鈣脫脂奶粉有沒有重複的成分」→ 鈣（形式相同）；
    覆蓋 1/1 與 1/3（未辨識 0 與 2）；「優鎂錠狀食品跟檸檬酸鎂膠囊有沒有重複的成分」→ 鎂（成分形式不同）；覆蓋 1/5、1/4（未辨識 4 與 3）。
    mode = duplicate_comparison；標題「重複成分提醒」；按鈕 完整分析 / 證據來源 / 成分對應；只查產品＋成分＋重複（無 RAG / web）；8–11s。
  * 4 EVIDENCE_CONFLICT：「魚油跟Warfarin可以一起吃嗎」；「樂活保健魚油跟Warfarin可以一起吃嗎」（產品直接含魚油形式；覆蓋 1/7，未辨識 6）
    → INT_FISH_WAR CONFLICT；標題「目前證據不一致」；無 RAG / web；7–8s。corpus 只有這一筆衝突。
  * 5 NO_CORPUS_RECORD：「鈣跟鋅可以一起吃嗎」「維生素C跟Warfarin可以一起吃嗎」→ pairs NO_CORPUS_RECORD；標題「目前交互作用資料庫沒有收錄這組關係」；
    只有「查看完整分析」；依既有 Researcher policy 會補 RAG ＋ 官方網站（33–43s，reply token 仍在 50s 內，但接近）。
  * 以上不更動 automatic_interaction_inheritance；藥品只用 corpus canonical 名稱（沒有台灣商品名對應）。
- tests：`tests/unit/test_interaction_reporter.py`（35，虛構資料）；`tests/integration/test_interaction_line_validate.py`
  （11 種狀態 ＋ 全部 detail / 來源卡共 41 則 Flex 通過 LINE validate API）。
  targeted unit 21 檔 470 passed ＋ 12 檔 264 passed（含 Windows 暫存目錄 PermissionError 的 1 個 error，換 basetemp 重跑 49 passed）；未跑 full pytest。

## 注意事項

- DB 目前為正式 565 health_food＋9112 查驗登記／核備（共 9677 products，DATA FREEZE）；寫入 / reset 需使用者同意。
- Migration：新 Supabase 已套用 0001 + 0002 + 0003（claim policy corpus）+ 0004（product coverage，使用者於 SQL Editor 套用），皆已匯入。
  全新 DB：0001 → 0002（no-op）→ 0003 → 0004。`tests/db/test_migration_0002_upgrade.py` 以本機 PostgreSQL 驗證
  舊 0001 等價 schema → 0002 → 0002 與全新安裝 schema 一致。

## 下一步（依序）

**TFDA Claim Policy Corpus：0003 已套用、corpus 已匯入、runtime 已接入、唯讀 smoke 通過**。
下一步：完整 pipeline live regression（核准功效原文廣告、官方醫療效能例示原文、待複核例示、無產品一般可用詞句、URL / 圖片宣稱），
確認延遲與 web fallback 次數；不需要再 migration / import / embedding。

**TFDA Product Coverage — Production Import 完成（2026-10-04）**：
- Preflight（唯讀）PASS：marker nutri_safe_ai、products 565（全 health_food）/ permits 565 / raw 565、integrity blockers 0；
  使用者於 SQL Editor 套用 0004 後再確認：`migration_0004_tfda_product_coverage = applied`、565 列與套用前逐欄相同、view 有新欄位；
  `--compare-supabase` 仍為 Domestic 1705 / Imported 7407 / insert 9112 / collisions 0 / missing 0 / errors 0。
- `--apply --confirm-project nutri_safe_ai`（exit 0、errors 0）：source_registry 2、raw_import_records 9112、companies 新增 1063（沿用 67）、
  permits 9112、products 9112；Neo4j：0003 index＋health_food backfill、Source 2 / Company merge 1130 / Permit 9112 / Product 9112 / links 9112。
- 匯入後（唯讀）：health_food 565 / domestic 1705 / imported 7407 / total 9677。`check_data_integrity --expect-category health_food=565
  --coverage-snapshot data/tfda_product_coverage/2026-10-04 --expect-products 9677` PASS（blockers 0）：Supabase ↔ Neo4j Product / Permit 9677、
  Company 1308、Source 74 一致；Neo4j product_category 缺 0；每來源 products = raw；canonical ID 全部由官方字號推導。
- 565 health_food：products / permits / raw / product_claims 652 / approved_claims 14 與匯入前逐欄相同；SUPPORTED_BY 仍指向健康食品來源。
- 新兩類：status / permit status 全 UNKNOWN、status_raw NULL；expiry_date 9112；字號 10 碼 9112、前導零 196、permit_code = 字號 = extract_permit_code；
  全 DB permit_number / permit_code 無重複；product_claims / product_ingredients / product_warnings 0；Neo4j 新 Product 無 CONTAINS / HAS_APPROVED_CLAIM，
  Ingredient 仍 272，無 SAME_AS / INTERACTS_WITH 關係型別。全 DB 同名組 257（不合併）。
- 重跑 `--compare-supabase`：products / permits / raw 9112 unchanged、insert 0、companies 1130 reused、errors 0。
- Production READ-ONLY smoke（真實 Supabase / Neo4j repo，intake 用固定抽取不呼叫 OpenAI，不寫 log，產品由 DB 隨機挑選）：
  health_food 功效 → PRODUCT_INFO＋核准功效（fast path 1.8s）；國產 10 碼字號 → VERIFIED PERMIT_NUMBER、「查有官方查驗登記／核備資料」、
  無核准功效、LOW 無 INACTIVE（0.5s）；輸入前導零＋依日期已過有效日期 → 同上，日期狀態標「非官方狀態」（0.4s）；跨類別同名 3 筆 →
  structured_clarification 候選（標類別＋字號名稱），不合併。全部無 planner / RAG / assess / web。
- tests：targeted 12 檔 245 passed（未跑 full pytest、未跑 live integration）。
- 下一步：手機 smoke（10 碼字號、新類別品名、跨類別同名選擇、查無產品文字）；新類別成分問題目前仍走完整流程（尚無結構化成分）。

**Product Ingredient Mapping Bridge（2026-10-04 production import 完成，marker `ingredient_bridge_import:1.0:1.0`）**：下一步——
(a) 手機成分 smoke（Agent B / C 已接 bridge，見 Ingredient Retrieval Runtime Integration）：「XX 有哪些成分」→「它有鋅嗎」、
    硬脂酸鎂產品問鎂、魚油產品問 EPA、長成分清單的「查看完整成分」；
(b) 版本更新（overlay / corpus 新版）需另寫版本切換流程（importer 目前對其他版本 / stale 列 fail closed）；
(c) overlay 剩下的 AMBIGUOUS / NEEDS_REVIEW（硬脂酸鎂、MAGNESIUM STEARATE、氧化鐵色素、複方…）需新的審核來源，不可自行補；
(d) 之後才接 Interaction / DrugSubstance / Evidence 與 Agent B evidence tool。

0. 手機重測 image identity：有字號但品名 / 標章小字易誤讀的包裝、字號被擋住只看到品名、同名多公司包裝（含公司名）
0a. 手機重測 pending clarification：只傳相似包裝圖 → 回「是」/「不是」/ 改傳其他品名；字號＋不同品名 → 回「是同一個」
0. 手機重測 claim policy：已確認產品的包裝 / 廣告圖（核准功效原文）、同圖加療效句、多品項產品名 + 廣告宣稱
0b. 手機重測 URL：一個產品 TFDA 官方頁、一個非官方商城 / 廣告頁（見 URL Verification Stabilization）
0c. 補官方 policy corpus（醫療效能認定準則、保健功效公告）進 RAG 需使用者同意（DATA FREEZE）
1. 在 LINE Developers Console 設定 Webhook URL（`https://<public-host>/line/webhook`），以真實手機走一次 demo 主線
2. RequestStore / TaskContextStore 改 Supabase 持久化（多 instance 前必須；目前為 prototype limitation：單一 process、重啟即清空）
