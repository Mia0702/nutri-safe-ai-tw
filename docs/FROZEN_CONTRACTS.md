# FROZEN_CONTRACTS — 呷安心（nutri_safe_ai）

> Stability Checkpoint（2026-10-03）。這裡列的是**已完成、且新功能不得破壞**的行為契約。
> 規則以 `CLAUDE.md` 為準；目前狀態見 `docs/CURRENT_STATE.md`。
>
> - 這份文件只記「行為」，不記實作細節；實作可以重構，只要行為與對應的 regression test 不變。
> - 修改任何 frozen contract 需要使用者明確同意（見 CLAUDE.md §17）。
> - 每個 contract 至少有一個 regression test；測試全部使用虛構資料（TEST_ONLY），不含真實產品。

---

## A. Agent / Tool 邊界

> 文件用語：Agent A = Controller Agent、Agent B = Researcher Agent、Agent C = Reporter Agent（僅文件命名；程式識別字與測試名稱不變，契約內容未改）。

| ID | Contract | Regression tests |
|---|---|---|
| FC-A1 | Agent A 只用 Input Tools（analyze_image / transcribe_audio / fetch_url_content / reinspect_image_identity），碰不到 Evidence Tools 與資料服務；文字輸入不呼叫任何 Input Tool。 | `test_tool_boundaries.py::test_agent_a_cannot_reach_evidence_tools_or_data_services`, `::test_text_input_uses_no_input_tool`, `::test_input_and_evidence_tools_are_disjoint_and_complete` |
| FC-A2 | Agent B 只用 Evidence Tools，不使用 Input Tools。 | `test_tool_boundaries.py::test_agent_b_does_not_use_input_tools` |
| FC-A3 | Agent C / Renderer 不查資料、不呼叫 Agent；primary response（`compose`）deterministic、不呼叫 LLM；只有「查看完整分析」才呼叫 LLM。 | `test_tool_boundaries.py::test_agent_c_and_renderer_do_not_query_data`, `::test_renderer_does_not_call_agents`, `test_adaptive_response.py::test_primary_response_is_one_message_without_llm_report`, `::test_detail_generates_full_analysis_on_demand_only` |
| FC-A4 | Agent C 不新增 EvidenceBundle 中沒有的官方結論；推測關聯不得寫成官方確認或擁有關係。 | `test_frozen_contracts.py::test_reporter_adds_no_official_conclusion_without_official_evidence`, `test_brand_inference.py::test_reporter_rejects_body_that_calls_inference_official`, `::test_reporter_rejects_body_that_describes_inference_as_ownership` |
| FC-A5 | 所有非文字輸入經 Input Tool 轉成文字後走同一段語意理解；Input Tool 全失敗且無文字時如實報錯，不假裝理解。 | `test_tool_boundaries.py::test_image_and_audio_go_through_input_tools_into_one_request`, `::test_all_input_tools_failed_without_text_raises`, `::test_failed_input_tool_with_text_continues_with_note` |
| FC-A6 | （2026-10-05 新增，additive）**語音逐字稿字形正規化**：轉錄含簡體字時，送進 Controller 前轉成繁體字形（OpenCC `s2t`；只處理字形，不改詞意、不做台灣慣用語改寫、不猜產品、不影響 resolver）；已是繁體 → no-op（「台」保留）；英文 / 數字 / 許可證字號不變；`raw_transcript` 與 `normalized_transcript` 保存在 request（log / debug），Controller 與使用者介面使用 normalized。轉換後仍只是使用者說的話：聽錯字照既有規則走候選確認，不會因此自動 VERIFIED。 | `test_transcript_normalization.py` |

## B. Product Verification（deterministic resolver）

| ID | Contract | Regression tests |
|---|---|---|
| FC-B1 | （2026-10-05 修訂：修正寫法佐證不再自動 VERIFIED，見 FC-B8）證據優先序：使用者提供的許可證字號（唯一）> 官方品名 / 官方別名 exact > 基本品名 exact > 修正寫法＋原始寫法佐證 > 品名＋公司 / 品牌 > 名稱相似 > 公司 / 推測品牌（只能 PARTIAL）。（2026-10-04 經使用者同意調整）**Exact strong evidence 可確認**：唯一字號 / 官方品名 / 官方別名完全相同 → VERIFIED；同一字號對應多筆 → 候選確認。 | `test_resolver_parity.py`, `test_p1_fastpath_dedup.py`, `test_image_identity_robustness.py::test_name_with_company_picks_the_corroborated_candidate`, `test_resolver_safety_audit.py`::test_exact_canonical_name_verifies`, `::test_exact_official_alias_verifies`, `::test_exact_registration_number_verifies`, `::test_name_company_corroboration_does_not_regress`, `test_product_coverage_runtime.py::test_registration_number_matching_multiple_records_asks_for_choice` |
| FC-B2 | 兩個以上真正合理的候選（ambiguous）不猜：PARTIAL_MATCH、不設 best_match；資訊類問題走 `structured_clarification`（不跑 planner / RAG / web / assess）。（2026-10-04）**Fuzzy name similarity 在 ambiguity 時必須 clarification**：純名稱相似只有在 A 使用者文字只靠省略字即可由官方品名得到（沒有替換字）、B 沒有其他候選品名也包含同一段文字、C 第二名 < 0.80 或領先 >= 0.10 全部成立才自動確認；有對手 → 候選卡，只有替換字 → 單一候選確認。同名、跨類別同名 / 相似名、一般品名一律候選。 | `test_resolver_parity.py`, `test_p1_fastpath_dedup.py`, `test_resolver_safety_audit.py`, `test_product_selection.py::test_fuzzy_typo_goes_to_candidate_card_and_selection_resumes_original_intent` |
| FC-B3 | 名稱相似度一律以使用者原始寫法計分；planner 查詢名稱 / 抄來的字號不能確認產品；resolver 鎖定的結果 planner 不可改寫。（2026-10-04）**不得為提高命中率犧牲 identity safety**：分數高於固定門檻不能覆蓋 ambiguity；wrong auto-confirm 比多一次確認更危險。 | `test_resolver_parity.py`, `test_p1_fastpath_dedup.py`, `test_resolver_safety_audit.py`::test_typo_closer_to_a_wrong_shorter_product_asks_for_choice`, `::test_top_gap_0057_asks_for_choice` |
| FC-B4 | 圖片品名＋字號唯一命中：OCR 差異只標 `OCR_UNCERTAIN` / `IMAGE_TEXT_CONFLICT`（VERIFIED，不單獨造成 HIGH）；只有清楚完整的官方品名指向另一產品才是 CONFLICT。使用者打字的字號＋不同品名仍是 CONFLICT。 | `test_image_identity_robustness.py::test_permit_exact_with_ocr_name_typo_is_verified_with_flags`, `::test_permit_a_with_clear_official_name_b_is_identity_conflict`, `::test_typed_text_permit_with_different_name_keeps_conflict` |
| FC-B5 | 認證標章是視覺證據，只能輔助；標章文字不能當品名 / 品牌、不能單獨確認產品。 | `test_image_identity_robustness.py::test_permit_exact_with_unreadable_certification_mark`, `::test_certification_mark_alone_cannot_confirm_product` |
| FC-B7 | （2026-10-04 新增，additive）**Candidate suggestion 必須有 identity relevance，generic ingredient overlap alone 不足以建立 product candidate**：純名稱相似（PARTIAL_MATCH＋NAME_SIMILARITY）的候選必須支持使用者寫法中有辨識力的部分（品牌 / 公司 / 品名特有字樣）；只有營養素 / 成分 / 劑型 / 類別字樣重疊 → 不列候選，NOT_FOUND（`NO_PRODUCT_IDENTITY_SUPPORT`），不跑 planner / RAG / web。只過濾、不確認；exact / alias / 字號 / 基本品名 / 自動確認門檻不變；無辨識字樣的一般查詢照常列多候選；錯字仍可候選確認。（2026-10-04 補充）**Brand match alone 不足以建立 product candidate**：品牌 / 公司相同只代表可能同品牌；使用者寫了產品核心描述時，未確認候選（含名稱＋品牌佐證的單一候選）必須同時支持該核心，核心衝突即使同品牌也不顯示。 | `test_candidate_relevance_gate.py` |
| FC-B6 | 局部二次 Vision 只在圖片產品身份未確認、且字號未命中時做，最多一次；readable=false 的欄位不採用。 | `test_image_identity_robustness.py::test_no_second_pass_when_identity_resolved`, `::test_no_permit_uncertain_name_reread_*`, `::test_reread_ignores_unreadable_fields` |
| FC-B8 | （2026-10-05 新增）**Product mention provenance**：Agent A 抽出的產品名稱與使用者原文分開保存（`product_mentions`：USER_TEXT / AGENT_CORRECTED / NOT_IN_USER_TEXT）。`product_candidates` 只放使用者寫法；Agent A 改字後的名稱只是線索（`corrected_product_candidates`），不得成為 CANONICAL_EXACT；修正寫法與官方品名相同也只列為單一候選請使用者確認（`CORRECTED_NAME_CORROBORATED`），不自動 VERIFIED；輸入中找不到的名稱不當使用者寫法。exact / 基本品名 / 字號 / 官方別名行為不變。 | `test_product_mention_provenance.py`, `test_p1_fastpath_dedup.py::test_corrected_name_corroborated_by_user_wording_needs_user_confirmation` |
| FC-B9 | （2026-10-05 新增）**Clarification before research**：產品身份未確定（PARTIAL / 多候選 / 修正寫法待確認 / 名稱 collision）時不跑 planner / RAG / 官方網頁 / assess / graph / 違規廣告查詢，直接請使用者確認；選定後以原題續跑（不重跑 Controller Agent）。有宣稱者仍走既有 claims-pending 路徑。 | `test_bare_name_routing.py::test_unconfirmed_typo_candidate_stops_before_any_research`, `::test_selected_candidate_resumes_original_intent_without_controller_rerun`, `test_p1_fastpath_dedup.py::test_unconfirmed_candidate_stops_before_planner_so_no_requery_can_verify` |
| FC-B10 | （2026-10-05 新增）**台灣官方登記的進口產品不是外國產品**：resolver 已 VERIFIED 到本資料庫（台灣 TFDA 登記，含 imported_tablet_capsule）的產品，不因國別 / 名稱字樣被當成 foreign_product（`scope_foreign_signal`）；沒有台灣紀錄的外國產品維持原行為。 | `test_bare_name_routing.py::test_taiwan_registered_product_with_country_word_is_not_foreign_scope`, `::test_unregistered_overseas_product_keeps_foreign_behavior` |

## C. Risk / Claim

| ID | Contract | Regression tests |
|---|---|---|
| FC-C1 | Risk Engine deterministic，分開評 Product Verification / Claim / Evidence Strength 再合成；沒有「有 DB = 低風險」這類規則。 | `test_risk_engine.py`, `test_assessment_rules.py` |
| FC-C2 | NOT_FOUND ≠ 假貨：NOT_FOUND 單獨不會 HIGH，必附「不代表是假貨」說明；查詢失敗不等於查無。（2026-10-04 經使用者同意調整）沒有命題的查無產品 / 查詢失敗在 LINE 上是 MISSING_INFORMATION（無風險燈號、列出涵蓋的官方類別），Risk Engine 內部結果不變。 | `test_risk_engine.py::test_not_found_is_medium_with_not_fake_disclaimer_and_never_high_alone`, `::test_failed_lookup_does_not_claim_not_found`, `test_adaptive_response.py::test_unknown_product_is_missing_information_and_not_called_fake`, `test_intent_first_response.py::test_not_found_is_missing_information_without_risk_light` |
| FC-C3 | 官方登記的產品也可能有不當宣稱：產品方 / 廣告的醫療宣稱即使產品 VERIFIED 也是 HIGH；使用者提問（USER_QUESTION）不會讓產品風險變 HIGH；流傳說法是 MEDIUM。（2026-10-04 經使用者同意調整）提問中的疾病治療命題仍以同一套 claim policy 評估，`ClaimAssessment.claim_risk = HIGH`，只以「若把這句話當成產品功效宣稱，屬高風險說法」呈現，claim_role 保持 USER_QUESTION。 | `test_risk_engine.py::test_product_side_medical_claim_is_high_even_for_verified_product`, `::test_question_about_disease_on_verified_product_is_not_high`, `::test_circulating_medical_claim_is_medium_not_high`, `test_claim_verdict_consistency.py::test_medical_treatment_question_keeps_role_and_shows_high_claim_risk_without_blame` |
| FC-C4 | Claim policy 順序：明確治療 / 診斷 → 核准功效比對 → 無對應的疾病宣稱 = MEDICAL、其餘 UNSUPPORTED → 產品未確認 / 比對失敗 = INSUFFICIENT（不 HIGH）；核准功效原文不會被當醫療宣稱。 | `test_claim_policy.py` |
| FC-C5 | 開放式功效詢問（「核准功效是什麼？」）不是命題；只傳圖片的包裝宣稱存為 observed，不自動查證（醫療宣稱 / 使用者明確要求除外）。 | `test_demo_stabilization.py`, `test_image_routing.py` |
| FC-C6 | Phase 1 只有台灣：提到外國產品時加上「只查台灣資料」說明。 | `test_risk_engine.py::test_foreign_country_adds_tw_scope_disclaimer` |

## D. Sources / Official Web / URL

| ID | Contract | Regression tests |
|---|---|---|
| FC-D1 | Official web fallback 只保留台灣官方網域的結果。 | `test_tools.py::test_official_web_only_keeps_allowed_domains_and_visited_pages`, `::test_is_allowed_official_url` |
| FC-D2 | URL source role 以（重新導向後的）網域判斷；官方頁陳述不進 claims；非官方頁自稱官方仍是第三方說法。 | `test_url_source_role.py` |
| FC-D3 | fetch_url_content 只允許 http(s) 公開 IP，每次 redirect 重新檢查（SSRF）。 | `test_tool_boundaries.py::test_fetch_url_blocks_non_public_targets`, `::test_fetch_url_rechecks_host_on_redirect` |
| FC-D4 | 來源卡只用該題保存的 evidence；網址需通過 `safe_source_url` 才有 uri 按鈕。 | `test_image_context_sources.py` |

## E. Response / LINE

| ID | Contract | Regression tests |
|---|---|---|
| FC-E1 | 每個 request 只送一則 primary Flex；只有 RISK 有 risk_level；CLAIM_RESULT 另有宣稱風險標示 `claim_risk_level`（🟢 / 🟠 / 🔴，取自 ClaimAssessment，2026-10-04 新增）；PRODUCT_INFO / MISSING_INFORMATION 無風險燈號（2026-10-04：可有 `status_line` 關鍵短語，🟢 官方確認 / 🟠 資料不足或登記≠核准 / 🔴 高風險，是語意顏色不是風險燈號）。 | `test_adaptive_response.py::test_payload_risk_level_only_for_risk_type`, `::test_every_response_type_renders_one_neutral_flex_message`, `test_line_webhook.py::test_each_input_type_returns_exactly_one_flex_with_quick_reply`, `test_response_ux_cleanup.py` |
| FC-E2 | 按鈕一律 request-scoped（request_id＋同一 user）；舊卡片回答舊題；不屬於該題的 action / candidate 一律拒絕。 | `test_adaptive_response.py::test_every_button_is_request_scoped_postback`, `::test_old_button_returns_old_request_after_new_question`, `::test_action_of_other_user_or_unknown_request_is_rejected`, `test_product_selection.py` |
| FC-E3 | 逾時：一張 ERROR card；晚到結果不補發、不覆蓋紀錄、不寫 context。 | `test_adaptive_response.py::test_timeout_returns_error_card_and_late_result_is_discarded`, `test_line_webhook.py::test_timeout_sends_one_error_card_and_late_result_never_arrives` |
| FC-E4 | Webhook：簽章驗證；webhookEventId 去重；per-user FIFO、不同 user 平行；單一 job 失敗不卡後續；reply 過期 / 400 改 push（retry key = request_id）。 | `test_line_webhook.py::test_webhook_rejects_bad_signature_and_accepts_valid`, `::test_redelivered_event_is_answered_once`, `::test_replies_follow_fifo_order_per_user`, `::test_queue_job_failure_does_not_block_next_request`, `::test_fresh_reply_token_uses_reply_and_stale_uses_push_with_retry_key` |
| FC-E5 | 多候選確認卡選擇：候選必須屬於那一題；不重跑 Agent A；結果必須是所選產品才回答。 | `test_product_selection.py` |
| FC-E6 | 圖片 query label 在收到時固定，所有 postback 沿用原題 label。 | `test_image_context_sources.py` |
| FC-E7 | Flex text / button 跟隨 LINE 字體設定（scaling / wrap）。 | `test_context_binding_fonts.py` |
| FC-E8 | **Primary Flex card = 摘要，不是資料 dump**：只顯示最重要結論與最多 3 個重點（`MAX_KEY_POINTS = 3`）；長篇功效、完整警語、完整來源、詳細證據放 on-demand detail card；MISSING_INFORMATION 不顯示無價值的「完整分析 / 為什麼」；官方資訊第一層只顯示核心欄位（品名、字號、狀態、申請商、功效摘要），完整功效 / 警語 / 來源在下一層。新功能不得因為有更多資料就把全部資訊塞回 primary card（新內容一律走新的 detail action）。 | `test_adaptive_response.py::test_every_response_type_renders_one_neutral_flex_message`, `::test_detail_generates_full_analysis_on_demand_only`, `test_response_ux_cleanup.py::test_ambiguous_text_product_is_missing_information_with_choices_only`, `::test_missing_information_offers_retry_only_when_a_source_failed`, `::test_official_info_is_compact_summary_with_follow_up_actions`, `::test_full_efficacy_detail_shows_full_deduped_wording`, `::test_warning_detail_is_split_into_points` |
| FC-E9 | （2026-10-05 新增）獨立成分 / 營養素回覆只在有可用證據（已確認 identity、官方成分名稱、通過 Gate 的功能證據）時提供「查看完整分析」；沿用 request-scoped DETAIL（同一份證據、不重跑 Controller、不新增證據）；沒有證據不提供；產品成分卡按鈕不變。 | `test_standalone_identity.py::test_function_question_with_evidence_has_full_analysis_and_detail_reuses_request`, `::test_standalone_without_any_usable_evidence_has_no_full_analysis_button`, `::test_product_ingredient_card_buttons_unchanged` |

## F. Task Context

| ID | Contract | Regression tests |
|---|---|---|
| FC-F1 | Task context per user、TTL 30 分鐘、最近 5 題；開始新問題 / 結束本次查證清除；舊 session 的結果不寫回。 | `test_line_webhook.py::test_start_new_question_clears_previous_product`, `::test_end_session_clears_context`, `::test_task_context_ttl_and_stale_session_result_is_dropped`, `::test_task_context_keeps_only_recent_requests` |
| FC-F2 | 已確認產品是代名詞 follow-up 的 authoritative referent（以字號查證）；明確提出的新產品 / 字號優先；無法解析指稱 → 要求補資訊，不猜。 | `test_context_binding_fonts.py`, `test_line_webhook.py::test_pronoun_without_context_asks_for_product` |
| FC-F3 | Pending clarification：短回覆 CONFIRM 以候選續上一題（不重跑 Agent A）、REJECT 清候選、明確新產品優先；確認只代表 USER_CONFIRMED_TARGET，衝突保留；new / end / TTL 清除。 | `test_pending_clarification.py` |
| FC-F4 | （2026-10-05 新增）**Standalone entity 優先於前文產品**：整句原文（不依賴 Controller intent）是 canonical 成分 / 營養素或一般成分用語、沒有品牌 / 公司 / 字號 / 劑型 / 產品字樣 / 代名詞 → 獨立成分詢問，這一題不綁定前一題已確認產品（不改 context）；代名詞 follow-up（「它有含 X 嗎？」）仍綁定；名稱完全等於官方產品名稱（或別名）→ collision 詢問（基本品名相同不算）。 | `test_bare_name_routing.py`, `test_standalone_identity.py::test_bare_entity_after_confirmed_product_does_not_bind_previous_product`, `::test_pronoun_follow_up_still_binds_previous_product`, `::test_bare_generic_nutrient_not_in_corpus_is_standalone_not_product_candidates` |

## G. Data / Ingestion / Security

| ID | Contract | Regression tests |
|---|---|---|
| FC-G1 | 推測品牌永遠 INFERRED（不是 OFFICIAL_RECORD）；OWNS_BRAND 只限官方 / 人工審核且有 ownership evidence。 | `test_brand_inference.py`, `test_brand_quality_gate.py`, `test_migration_schema.py` |
| FC-G2 | Ingestion quality gates：公司名稱正規化只去強證據附註；成分解析狀態與 canonical 合併只限格式差異。 | `test_company_name.py`, `test_ingredient_parser.py`, `test_ingredient_canonical.py`, `test_ingestion.py` |
| FC-G3 | graph_sync 分批且 MERGE 可重跑；reset 需 confirm＋marker；integrity gate 有 blocker 時失敗。 | `test_data_closeout.py` |
| FC-G4 | Migration：全新安裝與 0001 → 0002 升級 schema 一致。 | `tests/db/test_migration_postgres.py`, `tests/db/test_migration_0002_upgrade.py` |
| FC-G5 | 不 log secrets / 未列入白名單的欄位；secret key 不外洩；legacy service role key 不使用。 | `test_pipeline_api.py::test_json_log_formatter_redacts_secrets_and_drops_unknown_fields`, `test_settings.py` |
| FC-G6 | `.env` 不進版控；`.env.example` 只有變數名稱或安全 placeholder。 | `test_frozen_contracts.py::test_env_file_is_ignored_and_example_has_no_secret_values` |
| FC-G7 | Production code 不 hard-code 測試 fixture 的產品 / 字號 / 公司或 TEST_ONLY 標記。 | `test_frozen_contracts.py::test_production_code_contains_no_fixture_identities` |

## H. Ingredient / Standalone Entity

| ID | Contract | Regression tests |
|---|---|---|
| FC-H1 | （2026-10-05 新增）**成分 identity 與功能證據分開**：identity 只用 exact（Ingredient Bridge canonical entity / 已接受 alias；官方成分表完全相同名稱），不用相似度 / embedding；候選型 alias 不算 identity。`ingredient_identity_status`（CANONICAL_ENTITY / LEGACY_OFFICIAL_NAME / IDENTITY_NOT_IN_CORPUS）與 `function_evidence_status`（NOT_ASSESSED / SUPPORTED / NO_FUNCTION_EVIDENCE）是不同欄位；未收錄 ≠ 不存在；不稱生理功能為產品核准保健功效；Interaction corpus 不當一般功能證據。 | `test_standalone_identity.py` |
| FC-H2 | （2026-10-05 新增）獨立成分路徑（`standalone_entity` 模式）每種證據各查一次（lookup_ingredient / RAG / assess_evidence，必要時官方網頁一次），不跑 planner 多輪；同一組工具與 Relevance Gate，不降低證據門檻。 | `test_standalone_identity.py`, `test_bare_name_routing.py::test_function_question_runs_each_evidence_tool_once_and_reports_insufficient_without_evidence` |
| FC-H3 | Ingredient Bridge / 產品成分三層（官方列有 / 已確認 identity / 待審候選）、「有沒有某成分」答案的既有行為。（2026-10-05 呈現層簡化，經使用者要求）**成分清單只顯示官方登記的成分名稱（原樣）**：「查看完整成分」標題反映來源範圍：官方成分欄位為「官方登記成分（N 項）」，健康食品（只登記保健功效相關成分）為「保健功效相關成分（N 項）」，不推論超出來源的完整性，逐項不附成分身分 / 來源形式 / 未識別數量等 mapping metadata，清單已完整時不重複官方成分欄位原文（只有被截斷時才附原文）；內部資料（confirmed / unresolved / mapping / raw）不變。 | `test_ingredient_retrieval_runtime.py`, `test_ingredient_overlay_bridge.py`, `test_ingredient_entity_mapping.py`, `test_ingredient_bridge_import.py` |

## I. Multi-image

| ID | Contract | Regression tests |
|---|---|---|
| FC-I1 | （2026-10-05 新增，additive）**Multi-image bundle（同一產品 2–3 張照片）**：2 張以上圖片走 bundle path（一次 Vision `analyze_image_bundle` → 每個值保留 `observed_in` / readable / confidence → deterministic fusion → 既有 pipeline 只進一次）；單張圖片完全沿用 `analyze_image`。不同的清楚許可證字號 → `MULTI_IMAGE_IDENTITY_CONFLICT`、明確不同的完整品名且無共同字號 / 公司 / 品牌 → `SAME_PRODUCT_UNCERTAIN`：停止產品查證（不跑語意理解 / 任何證據工具）、請使用者確認，不任選其一；錯字層級差異只是 `OCR_UNCERTAIN`，依既有 identity 優先序。成分只能是成分、不成為產品候選；包裝宣稱沿用 observed_package_claims 並標示來源圖片。超過 3 張直接拒絕。結果與圖片順序無關。 | `test_image_bundle_fusion.py` |
| FC-I2 | （2026-10-05 新增，additive）**LINE 多照片蒐集**：只在使用者明確進入蒐集模式後蒐集（主要入口：「開始新問題」回覆上的 Quick Reply「多照片辨識」；文字「多照片辨識」只是相容捷徑；不依時間推測分組）；蒐集期間圖片靜默保存（不跑 Vision / Controller / Researcher、不回任何訊息；控制卡內的「開始分析 / 取消」按鈕深色 #333333），最多 3 張、第 4 張拒絕；「開始分析」（>= 1 張）才以一個 logical request 交給既有 pipeline：1 張 → 既有單張流程（不走 bundle）；2–3 張 → bundle Vision / fusion / Controller / Researcher / Reporter 各一次；取消 / TTL / 新問題 / 結束 / 分析成功都清除；所有步驟在 per-user FIFO job 內，開始分析不會早於前面的存圖。蒐集模式外的單張圖片流程不變。 | `test_multi_image_line.py` |
| FC-I3 | （2026-10-05 新增，additive；2026-10-05 後續：按鈕改為「檢視包裝宣稱」，只 deterministic 呈現已保存的圖片觀察、不跑 pipeline / 查詢，見 FC-I4；以下結構化路徑保留給未來的官方查證動作）**`package_claim_check` request 不回到通用 planner**：產品已確認時，沒有核准功效 / 警語是預期情況（登記 ≠ 保健功效核准，不能由 approved_claim = 0 推論合法 / 違法），沿用 PACKAGE_CLAIM 結構化路徑（claim-policy → RAG 一次 → assess → 官方網站最多一次）；不重跑 Controller。未經 postback 的包裝宣稱行為不變。 | `test_package_claim_postback.py` |
| FC-I4 | （2026-10-05 新增）**「檢視包裝宣稱」是包裝觀察，不是官方查證**：只用該題保存的結構化觀察（observed claims / 成分 / 警語 / 其他文字 / 來源圖片），不重跑 Vision / Controller、不查資料庫 / RAG / 官方網站、不與核准功效比對；明示「以下為照片中辨識到的文字，尚未與官方資料逐項比對」；完整觀察含照片上的產品名稱 / 品牌 / 公司 / 許可證字號（只是辨識到的文字，不標示為官方資料），只列有值的區塊並去除重複文字；一般成分中性、一般功效宣稱橘色「建議多留意」、疾病療效說法沿用既有 deterministic 條件標示高風險說法（只標那一句，不升級整個產品）。來源圖片編號（圖1 / 圖2…）只保留在結構化資料與 log，任何使用者介面（含身份衝突說明）都不顯示。 | `test_package_observation.py`, `test_image_routing.py::test_on_demand_package_observation_uses_stored_claims_without_any_pipeline` |
| FC-I5 | （2026-10-05 新增，additive）**Automatic Image Burst Gate**：不在明確蒐集模式時，image webhook 先進 debounce gate（`AUTO_IMAGE_BURST_WINDOW_MS`，預設 1800；0 = 停用），window 內只緩衝 event 識別碼，不在 per-user FIFO queue 裡等待；finalize 後只建立一個一般分析 job（1 張 = 單張流程；2–3 張 = bundle，各一次）；burst_id / generation 保護舊 timer；第 4 張拒絕並輕量提示；明確蒐集模式優先；其他事件到達先 flush（維持順序）、新問題 / 結束丟棄 pending burst；分組不代表同一產品（fusion / identity 規則不變）。 | `test_auto_image_burst.py` |
| FC-I6 | （2026-10-05 新增，經使用者要求擴充 Interaction identity 狀態）**UNKNOWN_TERM 與 IDENTITY_NOT_IN_INTERACTION_CORPUS 分開**：Controller 的 `concept_recognized`（語意判斷，預設 false）為 true 且成分 / 藥品 identity 不在 Interaction corpus → `IDENTITY_NOT_IN_INTERACTION_CORPUS`（資料涵蓋缺口）：不請使用者改寫法、明說「結構化交互作用資料尚未收錄」、允許受控補查（RAG 最多一次、官方網站最多一次），補查結果只是參考，不產生 CONCLUSION_ELIGIBLE / interaction / identity，也不暗示沒有交互作用或可以放心；無法辨識（UNKNOWN_TERM）、產品未確認、藥品無法對應有效成分、歧義 → 維持 clarification，不上網猜 identity。corpus 內的 deterministic 路徑與 eligibility 規則不變。 | `test_interaction_coverage_gap.py` |
| FC-I7 | （2026-10-05 新增，additive）**Sequential image follow-up**：沿用 per-user FIFO + TaskContext，不合併進處理中的圖片 request；`TaskContext.last_visual_request_id` 只是已完成圖片 request 的參照（不複製圖片，隨 new / end / TTL 清除）；Controller 的結構化 `context_reference`（CURRENT_PRODUCT / PREVIOUS_IMAGE / NONE，語意判斷、無關鍵字規則）為 PREVIOUS_IMAGE 且守門通過（有前文圖片、本題無自己的圖片 / 網址 / 新產品識別）→ 取回該題保存的結構化觀察，依既有 intent 只顯示相關區塊（成分 / 宣稱 / 警語 / 全部），明示是「照片上辨識到的內容」而非官方資料（不重跑 Vision / Researcher / RAG / 網路）；視覺參照獨立於產品 context：任何已完成圖片都寫入，未唯一確認產品的圖片不建立 current product、並清除更早的產品指稱；指稱「目前產品」卻無已確認產品、前文有可用圖片觀察 → 改由圖片觀察回答（不進通用 planner / 網路）；產品追問沿用既有 confirmed_product 綁定，沒有唯一確認的圖片產品不替使用者選。 | `test_image_followup.py` |

---

## Frozen modules（修改前需說明 extension point 為何不足）

- `app/risk/risk_engine.py`、`app/risk/assessment_rules.py`（resolver / claim / evidence rules）、`app/risk/claim_policy.py`
- `app/agents/verification_agent.py` 的 resolver 鎖定、fast path / early-stop 條件
- `app/agents/reporter_agent.py` 的 `select_response_type`、`compose_payload`、推測關聯 guard
- `app/services/context_store.py`、`app/services/context_binding.py`、`app/services/clarification.py`、`app/services/bare_name_routing.py`
- `app/response/service.py`、`app/response/actions.py`、`app/line/`（webhook、queue、handler）
- `app/tools/input_tools.py` 的 SSRF / 時限、`app/tools/official_web_tools.py` 的網域限制
- `app/schemas/`（既有欄位的語意；可新增欄位）、`migrations/`（只可新增 migration）

## Safe extension points

- **新的 Evidence Tool**：在 `app/agents/evidence_tools.py` 註冊（input / output 結構化、可單獨測試），由 Agent B 既有 planner 呼叫。
- **RAG corpus**：新增官方文件經 `app/ingestion/rag_ingest.py` 進 pgvector（需使用者同意寫入；DATA FREEZE）；
  `search_rag` / `CLAIM_POLICY_RAG_TEMPLATE` 自動使用。
- **Claim policy 來源**：`ClaimPolicyDecision.sources` 接受新的已通過 Relevance Gate 的法規文件；新增 `ClaimPolicyType` enum 值。
- **Response 內容**：新增 `ResponseAction` / `DETAIL_FOLLOW_UPS` / `DetailPayload` 型態（沿用 request-scoped postback）。
- **Schema**：新增可選欄位（有預設值）；新增 migration 檔。
- **Official domains**：`TW_OFFICIAL_DOMAINS` 只可加入台灣官方網域。
