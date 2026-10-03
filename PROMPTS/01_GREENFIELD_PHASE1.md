# 呷安心 Greenfield Rebuild — Phase 0 / Phase 1

## 目標

建立一個全新的「呷安心」後端骨架與第一條可測試 vertical slice。

主題：

> 基於 Multi-Agent 協作架構之保健食品安全辨識平台

目前僅限台灣資料。

不要讀取或沿用任何舊版專案。

---

# A. 系統需求摘要

呷安心的核心目的：

1. 將複雜的保健食品資訊轉為一般人容易理解的繁體中文。
2. 協助查證廣告、網路說法、圖片、語音與連結中的保健食品資訊是否有官方證據支持。
3. 以 LINE Official Account 作為主要 UI，降低長者與一般使用者的操作門檻。

使用者未來可輸入：

- 文字
- 圖片
- 語音
- 網址

輸入內容可能是：

- 保健食品名稱
- 包裝
- 產品照片
- 成分標示
- 許可字號
- 廣告截圖
- 網路貼文
- 問句
- 宣稱

---

# B. 技術方向

Backend：

```text
Python
FastAPI
```

主要服務：

```text
OpenAI API
Supabase PostgreSQL
Supabase pgvector
Neo4j
LINE Messaging API
```

不加入其他 AI model。

不使用 Hugging Face、local LLM、sentence-transformers 或額外 OCR/NLP/image model。

第一版優先不用 LangChain / LlamaIndex。
使用普通 Python services、Pydantic schemas 與 OpenAI 官方 SDK。

---

# C. 三個 Agent

## Agent A — Intake / Understanding Agent

輸入：

```text
text | image | audio | url | mixed
```

輸出：

`NormalizedUserRequest`

至少包含：

```json
{
  "input_type": "text|image|audio|url|mixed",
  "original_text": "",
  "normalized_text": "",
  "language": "zh-TW",
  "intent": "",
  "product_candidates": [],
  "brand_candidates": [],
  "company_candidates": [],
  "ingredient_candidates": [],
  "claims": [],
  "question": "",
  "needs_product_lookup": false,
  "needs_claim_check": false,
  "needs_graph_lookup": false,
  "needs_rag": false,
  "needs_official_web_search": false
}
```

語意分析與結構化抽取使用 OpenAI。

不要讓後續 Agent 重複重新解析同一份原始輸入。

---

## Agent B — Evidence / Verification Agent

根據 Agent A 的結果決定需要呼叫哪些 Tools。

證據優先級：

```text
1. Supabase structured official data
2. Neo4j graph relationships
3. Supabase RAG
4. Taiwan official web fallback
```

輸出：

`EvidenceBundle`

至少包含：

```json
{
  "product_match": {},
  "graph_evidence": [],
  "rag_evidence": [],
  "official_web_evidence": [],
  "claim_assessment": [],
  "evidence_strength": "strong|medium|weak|none",
  "conflicts": [],
  "missing_information": []
}
```

保留來源追蹤。

---

## Agent C — Reporter Agent

輸入：

```text
NormalizedUserRequest
EvidenceBundle
RiskAssessment
```

輸出：

- 繁體中文
- 白話
- 簡潔
- 結論優先
- 說明原因
- 能顯示來源時附上來源
- 不把保健食品當藥物治療建議

Reporter 不重新查資料，也不自行新增 evidence。

---

# D. Tools

先規劃 interface，逐步實作：

```text
lookup_product()
lookup_company()
lookup_permit()
lookup_ingredient()
lookup_approved_claim()
search_illegal_ad_cases()

query_knowledge_graph()
search_rag()
search_tw_official_web()

fetch_line_message_content()
transcribe_audio()
analyze_image()
fetch_public_url_content()
```

每個 Tool 要：

- typed input
- structured output
- 可單元測試
- 清楚 error state
- 不直接生成最終聊天答案

---

# E. Supabase

Supabase 是 canonical source of truth。

第一版 migration 規劃至少包含：

```text
source_registry
raw_import_records

companies
products
permits
ingredients
product_ingredients

approved_claims
product_claims

warnings
product_warnings

illegal_ad_cases

rag_documents
rag_chunks

line_users

analysis_requests
evidence_logs
```

重要官方資料盡量保留：

```text
source_id
source_record_key
source_url
retrieved_at
raw_data
```

`raw_data` 可使用 jsonb。

不要讓 Neo4j 與 Supabase 分別維護兩套互相獨立的完整 Product 主檔。

---

# F. Neo4j

Neo4j 專門負責 Knowledge Relationships。

第一版 Nodes：

```text
Product
Ingredient
Company
Permit
Claim
Warning
Source
```

第一版 Relationships：

```text
(Company)-[:OWNS_OR_APPLIES_FOR]->(Product)
(Product)-[:CONTAINS]->(Ingredient)
(Product)-[:REGISTERED_AS]->(Permit)
(Product)-[:HAS_APPROVED_CLAIM]->(Claim)
(Product)-[:HAS_WARNING]->(Warning)
(Product)-[:SUPPORTED_BY]->(Source)
(Claim)-[:SUPPORTED_BY]->(Source)
```

Supabase 與 Neo4j 共用 canonical IDs：

```text
product_id
ingredient_id
company_id
claim_id
permit_id
```

不要為了讓圖看起來複雜而建立沒有清楚語意的關係。

---

# G. RAG

RAG：

```text
Supabase PostgreSQL + pgvector
```

精確欄位優先 structured SQL。

非結構化官方文件的解釋與比對使用 RAG。

至少建立：

```text
rag_documents
rag_chunks
```

Document metadata 至少：

```text
title
source_url
source_name
document_type
country_code
published_at
retrieved_at
```

Chunk 至少：

```text
document_id
chunk_index
content
embedding
metadata
```

Embedding 使用 OpenAI API。

---

# H. Risk Model

三個中間維度：

## Product Verification

```text
VERIFIED
PARTIAL_MATCH
NOT_FOUND
CONFLICT
NOT_APPLICABLE
```

## Claim Assessment

```text
CONSISTENT_WITH_OFFICIAL
UNSUPPORTED
POTENTIALLY_EXAGGERATED
MEDICAL_TREATMENT_CLAIM
CONFLICT_WITH_OFFICIAL
UNKNOWN
NOT_APPLICABLE
```

## Evidence Strength

```text
STRONG
MEDIUM
WEAK
NONE
```

最後 Risk：

```text
LOW
MEDIUM
HIGH
```

Risk Engine 優先 deterministic。

LLM 負責抽取語意與 claim，不直接自由產生 LOW/MEDIUM/HIGH。

重要規則：

- `NOT_FOUND` 不等於假貨。
- 找不到資料時應說「目前未找到官方紀錄，不代表產品一定是假貨或違法」。
- 已登記產品仍可能因廣告宣稱而形成較高風險。
- evidence 不足要明確表示不確定，而不是猜答案。

---

# I. 四種輸入

## Text

直接進 Agent A。

## Image

OpenAI image understanding 抽取：

- product
- brand
- company
- ingredient
- visible claim
- permit candidate
- other visible information

不要加額外 OCR model。

## Audio

LINE/local audio：

```text
audio
→ OpenAI transcription
→ 與 Text 相同的 Agent A pipeline
```

不要建立第二套語意分析邏輯。

## URL

建立 URL ingestion。

成功時回傳：

```text
title
domain
visible_text
metadata
page_accessible = true
```

無法存取時：

```text
page_accessible = false
```

禁止假造網頁內容。

---

# J. Debug API

先建立：

```text
POST /api/debug/analyze
```

development only。

輸入範例：

```json
{
  "type": "text",
  "content": "這個產品可以治療糖尿病嗎？"
}
```

回傳 debug structure：

```text
request_id
normalized_request
tools_called
product_verification
evidence
claim_assessment
risk
final_answer
errors
```

這是第一階段最重要的測試入口，不要一開始所有測試都依賴 LINE。

---

# K. Project Structure

以目前 starter 為基礎，維持清楚結構：

```text
app/
  api/
  agents/
  tools/
  services/
  schemas/
  risk/
  config/

migrations/
scripts/
tests/
  unit/
  integration/
  fixtures/
```

不要過度拆分資料夾。

---

# L. Phase 0 — 先做這些

Phase 0 先完成：

1. 檢查 starter scaffold。
2. 建立設定管理。
3. 建立完整 Pydantic schemas。
4. 建立 enum。
5. 建立 service interfaces。
6. 建立 tool interfaces。
7. 建立 deterministic risk engine。
8. 建立 FastAPI app 與 `/health`。
9. 建立 `/api/debug/analyze` 的可測試骨架。
10. 建立 unit tests。
11. 建立 Supabase initial migration。
12. 建立 Neo4j schema / constraints script。
13. README architecture。

Phase 0 可以在缺少任何外部 API key 的情況下完成大部分測試。

Phase 0 驗收後，再開始 Phase 1。

---

# M. Phase 1 — 最小 Text Vertical Slice

Phase 1 優先只把文字跑通：

```text
text
→ Agent A
→ Verification Agent
→ available tools
→ EvidenceBundle
→ Risk Engine
→ Reporter
→ debug response
```

先完成：

- OpenAI semantic understanding
- structured output
- structured product lookup interface
- graph lookup interface
- RAG interface
- official fallback interface
- Reporter
- error handling
- integration boundaries

若 credentials 未填：

- unit tests 照跑
- external integration tests 明確 skip
- 不要 fake 成功

---

# N. 之後才做 Multimodal + LINE

Text vertical slice 穩定後，再依序：

1. image adapter
2. audio adapter
3. URL adapter
4. LINE webhook
5. 真實 end-to-end

不要四種輸入同時寫到一半。

---

# O. 今天希望測到的情境

資料量先少量即可，但架構最終要能涵蓋：

### A. 純文字產品查詢

「這個產品有官方資料嗎？」

### B. 功效問題

「這個保健食品可以補什麼？」

### C. 疾病治療宣稱

「吃這個可以治療糖尿病嗎？」

不能以完整句 hard-code 判定。

### D. 找不到產品

回傳 `NOT_FOUND`，但不能直接說是假貨。

### E. 圖片產品

抽取 product / brand / claim / ingredient / permit candidate。

### F. 廣告圖片

抽取 claim → verification → risk。

### G. 語音

transcription → same text pipeline。

### H. URL

fetch → extraction → same verification pipeline。

### I. Neo4j

至少可表達：

```text
Product → Ingredient
Product → Approved Claim
```

### J. RAG

至少一份台灣官方文件可被 retrieval。

### K. Official Web Fallback

本地 evidence 不足才啟動。

### L. LINE E2E

最後才做：

```text
LINE
→ webhook
→ pipeline
→ reply
```

---

# P. Error Handling

外部服務都可能失敗。

至少處理：

- OpenAI unavailable/timeout
- Supabase unavailable
- Neo4j unavailable
- RAG unavailable
- URL inaccessible
- official web unavailable
- LINE content download failed

不能 crash 後假裝成功。

使用 structured errors。

---

# Q. Observability

每個 request 產生：

```text
request_id
```

log 至少保留：

```text
request_id
input_type
intent
agent
tool_called
tool_duration
evidence_count
risk_result
error_code
```

不得 log secrets。

---

# R. 測試規則

tests/fixtures 可放 synthetic data，但必須標示 test only。

正式 Supabase / Neo4j 不允許使用假產品資料。

測試應驗證「能力」，不是把測試問句本身塞進 production code。

每一輪完成後執行：

```text
pytest
```

並回報：

```text
passed
failed
skipped
```

不要只說 All good。

---

# S. 完成回報格式

每一輪請回報：

A. 這輪完成內容  
B. 實際新增/修改檔案  
C. 實際執行 commands  
D. Tests：passed / failed / skipped  
E. External integrations 狀態  
F. 發現的架構問題  
G. 下一輪建議  

全部使用繁體中文。
