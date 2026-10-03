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
    「不代表是假貨或違法」。response_type 依 `select_response_type` 設計：產品查證 → RISK 卡、已確認產品的功效問題 →
    PRODUCT_INFO（含核准功效）、SOURCE_SUMMARY 只出現在不指定產品的一般問題。
  - Claim role：使用者問「可以治療糖尿病嗎」→ USER_QUESTION / CONFLICT_WITH_OFFICIAL / CLAIM_RESULT（無風險燈）；
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
  - Corpus：`data/tfda_claim_policy/`（`2026-10-03/` 子目錄為空，結構化檔案在上一層；版本取 validation.json
    `research_reference_date` = 2026-10-03）。
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
- 最新完整 pytest（2026-10-03，LINE 整合後）：466 passed / 7 skipped / 0 failed（當時 DB 尚未 seed）。
- 2026-10-03 seed 後（未改 production code，未重跑完整 suite）：`test_data_stores_live.py` 6 passed（原本 skip）、
  `test_line_demo_live.py` 6 passed。
- 產品選擇後（未跑完整 suite）：selection / postback / context / renderer / resolver 相關 unit 150 passed（6 個檔案）。
- P1 後（未跑完整 suite）：resolver / fast path / dedup / semantic / parity / LINE / agents / tools / risk 相關 unit 276 passed（12 個檔案）；
  `test_line_demo_live.py` 6 passed（446s）。

## 注意事項

- DB 目前為正式 565 筆 seed（DATA FREEZE）；寫入 / reset 需使用者同意。
- Migration：新 Supabase 已套用 0001 + 0002 + 0003（claim policy corpus，已匯入）。全新 DB：0001 → 0002（no-op）→ 0003。`tests/db/test_migration_0002_upgrade.py` 以本機 PostgreSQL 驗證
  舊 0001 等價 schema → 0002 → 0002 與全新安裝 schema 一致。

## 下一步（依序）

**TFDA Claim Policy Corpus：0003 已套用、corpus 已匯入、runtime 已接入、唯讀 smoke 通過**。
下一步：完整 pipeline live regression（核准功效原文廣告、官方醫療效能例示原文、待複核例示、無產品一般可用詞句、URL / 圖片宣稱），
確認延遲與 web fallback 次數；不需要再 migration / import / embedding。


0. 手機重測 image identity：有字號但品名 / 標章小字易誤讀的包裝、字號被擋住只看到品名、同名多公司包裝（含公司名）
0a. 手機重測 pending clarification：只傳相似包裝圖 → 回「是」/「不是」/ 改傳其他品名；字號＋不同品名 → 回「是同一個」
0. 手機重測 claim policy：已確認產品的包裝 / 廣告圖（核准功效原文）、同圖加療效句、多品項產品名 + 廣告宣稱
0b. 手機重測 URL：一個產品 TFDA 官方頁、一個非官方商城 / 廣告頁（見 URL Verification Stabilization）
0c. 補官方 policy corpus（醫療效能認定準則、保健功效公告）進 RAG 需使用者同意（DATA FREEZE）
1. 在 LINE Developers Console 設定 Webhook URL（`https://<public-host>/line/webhook`），以真實手機走一次 demo 主線
2. RequestStore / TaskContextStore 改 Supabase 持久化（多 instance 前必須；目前為 prototype limitation：單一 process、重啟即清空）
