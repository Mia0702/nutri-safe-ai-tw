# FROZEN_CONTRACTS — 呷安心（nutri_safe_ai）

> Stability Checkpoint（2026-10-03）。這裡列的是**已完成、且新功能不得破壞**的行為契約。
> 規則以 `CLAUDE.md` 為準；目前狀態見 `docs/CURRENT_STATE.md`。
>
> - 這份文件只記「行為」，不記實作細節；實作可以重構，只要行為與對應的 regression test 不變。
> - 修改任何 frozen contract 需要使用者明確同意（見 CLAUDE.md §17）。
> - 每個 contract 至少有一個 regression test；測試全部使用虛構資料（TEST_ONLY），不含真實產品。

---

## A. Agent / Tool 邊界

| ID | Contract | Regression tests |
|---|---|---|
| FC-A1 | Agent A 只用 Input Tools（analyze_image / transcribe_audio / fetch_url_content / reinspect_image_identity），碰不到 Evidence Tools 與資料服務；文字輸入不呼叫任何 Input Tool。 | `test_tool_boundaries.py::test_agent_a_cannot_reach_evidence_tools_or_data_services`, `::test_text_input_uses_no_input_tool`, `::test_input_and_evidence_tools_are_disjoint_and_complete` |
| FC-A2 | Agent B 只用 Evidence Tools，不使用 Input Tools。 | `test_tool_boundaries.py::test_agent_b_does_not_use_input_tools` |
| FC-A3 | Agent C / Renderer 不查資料、不呼叫 Agent；primary response（`compose`）deterministic、不呼叫 LLM；只有「查看完整分析」才呼叫 LLM。 | `test_tool_boundaries.py::test_agent_c_and_renderer_do_not_query_data`, `::test_renderer_does_not_call_agents`, `test_adaptive_response.py::test_primary_response_is_one_message_without_llm_report`, `::test_detail_generates_full_analysis_on_demand_only` |
| FC-A4 | Agent C 不新增 EvidenceBundle 中沒有的官方結論；推測關聯不得寫成官方確認或擁有關係。 | `test_frozen_contracts.py::test_reporter_adds_no_official_conclusion_without_official_evidence`, `test_brand_inference.py::test_reporter_rejects_body_that_calls_inference_official`, `::test_reporter_rejects_body_that_describes_inference_as_ownership` |
| FC-A5 | 所有非文字輸入經 Input Tool 轉成文字後走同一段語意理解；Input Tool 全失敗且無文字時如實報錯，不假裝理解。 | `test_tool_boundaries.py::test_image_and_audio_go_through_input_tools_into_one_request`, `::test_all_input_tools_failed_without_text_raises`, `::test_failed_input_tool_with_text_continues_with_note` |

## B. Product Verification（deterministic resolver）

| ID | Contract | Regression tests |
|---|---|---|
| FC-B1 | 證據優先序：使用者提供的許可證字號（唯一）> 官方品名 / 官方別名 exact > 基本品名 exact > 修正寫法＋原始寫法佐證 > 品名＋公司 / 品牌 > 名稱相似 > 公司 / 推測品牌（只能 PARTIAL）。（2026-10-04 經使用者同意調整）**Exact strong evidence 可確認**：唯一字號 / 官方品名 / 官方別名完全相同 → VERIFIED；同一字號對應多筆 → 候選確認。 | `test_resolver_parity.py`, `test_p1_fastpath_dedup.py`, `test_image_identity_robustness.py::test_name_with_company_picks_the_corroborated_candidate`, `test_resolver_safety_audit.py`::test_exact_canonical_name_verifies`, `::test_exact_official_alias_verifies`, `::test_exact_registration_number_verifies`, `::test_name_company_corroboration_does_not_regress`, `test_product_coverage_runtime.py::test_registration_number_matching_multiple_records_asks_for_choice` |
| FC-B2 | 兩個以上真正合理的候選（ambiguous）不猜：PARTIAL_MATCH、不設 best_match；資訊類問題走 `structured_clarification`（不跑 planner / RAG / web / assess）。（2026-10-04）**Fuzzy name similarity 在 ambiguity 時必須 clarification**：純名稱相似只有在 A 使用者文字只靠省略字即可由官方品名得到（沒有替換字）、B 沒有其他候選品名也包含同一段文字、C 第二名 < 0.80 或領先 >= 0.10 全部成立才自動確認；有對手 → 候選卡，只有替換字 → 單一候選確認。同名、跨類別同名 / 相似名、一般品名一律候選。 | `test_resolver_parity.py`, `test_p1_fastpath_dedup.py`, `test_resolver_safety_audit.py`, `test_product_selection.py::test_fuzzy_typo_goes_to_candidate_card_and_selection_resumes_original_intent` |
| FC-B3 | 名稱相似度一律以使用者原始寫法計分；planner 查詢名稱 / 抄來的字號不能確認產品；resolver 鎖定的結果 planner 不可改寫。（2026-10-04）**不得為提高命中率犧牲 identity safety**：分數高於固定門檻不能覆蓋 ambiguity；wrong auto-confirm 比多一次確認更危險。 | `test_resolver_parity.py`, `test_p1_fastpath_dedup.py`, `test_resolver_safety_audit.py`::test_typo_closer_to_a_wrong_shorter_product_asks_for_choice`, `::test_top_gap_0057_asks_for_choice` |
| FC-B4 | 圖片品名＋字號唯一命中：OCR 差異只標 `OCR_UNCERTAIN` / `IMAGE_TEXT_CONFLICT`（VERIFIED，不單獨造成 HIGH）；只有清楚完整的官方品名指向另一產品才是 CONFLICT。使用者打字的字號＋不同品名仍是 CONFLICT。 | `test_image_identity_robustness.py::test_permit_exact_with_ocr_name_typo_is_verified_with_flags`, `::test_permit_a_with_clear_official_name_b_is_identity_conflict`, `::test_typed_text_permit_with_different_name_keeps_conflict` |
| FC-B5 | 認證標章是視覺證據，只能輔助；標章文字不能當品名 / 品牌、不能單獨確認產品。 | `test_image_identity_robustness.py::test_permit_exact_with_unreadable_certification_mark`, `::test_certification_mark_alone_cannot_confirm_product` |
| FC-B6 | 局部二次 Vision 只在圖片產品身份未確認、且字號未命中時做，最多一次；readable=false 的欄位不採用。 | `test_image_identity_robustness.py::test_no_second_pass_when_identity_resolved`, `::test_no_permit_uncertain_name_reread_*`, `::test_reread_ignores_unreadable_fields` |

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

## F. Task Context

| ID | Contract | Regression tests |
|---|---|---|
| FC-F1 | Task context per user、TTL 30 分鐘、最近 5 題；開始新問題 / 結束本次查證清除；舊 session 的結果不寫回。 | `test_line_webhook.py::test_start_new_question_clears_previous_product`, `::test_end_session_clears_context`, `::test_task_context_ttl_and_stale_session_result_is_dropped`, `::test_task_context_keeps_only_recent_requests` |
| FC-F2 | 已確認產品是代名詞 follow-up 的 authoritative referent（以字號查證）；明確提出的新產品 / 字號優先；無法解析指稱 → 要求補資訊，不猜。 | `test_context_binding_fonts.py`, `test_line_webhook.py::test_pronoun_without_context_asks_for_product` |
| FC-F3 | Pending clarification：短回覆 CONFIRM 以候選續上一題（不重跑 Agent A）、REJECT 清候選、明確新產品優先；確認只代表 USER_CONFIRMED_TARGET，衝突保留；new / end / TTL 清除。 | `test_pending_clarification.py` |

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

---

## Frozen modules（修改前需說明 extension point 為何不足）

- `app/risk/risk_engine.py`、`app/risk/assessment_rules.py`（resolver / claim / evidence rules）、`app/risk/claim_policy.py`
- `app/agents/verification_agent.py` 的 resolver 鎖定、fast path / early-stop 條件
- `app/agents/reporter_agent.py` 的 `select_response_type`、`compose_payload`、推測關聯 guard
- `app/services/context_store.py`、`app/services/context_binding.py`、`app/services/clarification.py`
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
