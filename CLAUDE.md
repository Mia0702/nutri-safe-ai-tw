# 呷安心 — Project Rules

## 1. Project Identity

專案名稱：呷安心

主題方向：

> 基於 Multi-Agent 協作架構之保健食品安全辨識平台

這是一個全新的 Greenfield Project。

---

## 2. Absolute Greenfield Rule

禁止沿用、複製、搬移或參考任何舊版專題中的：

- production code
- Agent design
- prompts
- database schema
- Neo4j schema
- tests
- workaround
- hard-coded bug fixes
- old project folder structure
- old risk logic

若工作區以外存在舊專案，不要主動搜尋或讀取。

所有新設計都必須從本文件與目前 Prompt 的需求重新推導。

---

## 3. Phase 1 Scope

第一階段只處理：

```text
Taiwan / TW
```

目前不要加入美國、日本、韓國、澳洲、歐盟或其他國家的產品資料。

可以在資料結構預留：

```text
country_code
region
source_country
```

但目前功能與 seed data 僅以台灣為主。

---

## 4. Product Philosophy

系統目的：

1. 將複雜的保健食品資訊整理成一般人與長者可以理解的白話繁體中文。
2. 協助使用者查證網路、廣告、圖片、連結與口語中的保健食品說法是否有官方證據支持。
3. 透過 LINE Official Account 提供低學習成本的一對一聊天介面。

使用者輸入可能為：

- text
- image
- audio
- URL
- mixed input

---

## 5. AI Rule

AI 能力統一優先使用 OpenAI API。

預計包含：

- semantic understanding
- intent classification
- structured extraction
- image understanding
- speech transcription
- embeddings
- answer generation
- restricted official web fallback（如實作條件允許）

除非使用者之後明確要求，禁止加入：

- Hugging Face model
- local LLM
- sentence-transformers
- 額外 OCR model
- 額外 NLP classifier
- 額外 image classifier

---

## 6. Core Architecture Rule

第一階段只有三個主要 Agent：

### Agent A — Intake / Understanding

負責：

- 理解輸入
- 正規化文字
- 解析使用者意圖
- 抽取產品、品牌、公司、成分、宣稱、問題
- 判斷後續需要哪些工具

不得自行當資料庫回答事實。

### Agent B — Evidence / Verification

負責：

- 根據 Agent A 的結構化結果選擇工具
- 收集證據
- 統一 EvidenceBundle
- 記錄缺失、衝突與來源

不得憑模型記憶假裝官方查證成功。

### Agent C — Reporter

負責：

- 接收 NormalizedUserRequest
- 接收 EvidenceBundle
- 接收 RiskAssessment
- 用簡短、白話、繁體中文整理回答

不得重新搜尋資料。
不得新增 EvidenceBundle 中不存在的官方結論。

---

## 7. Agent vs Tool

Agent 負責：

> 決定現在應該做什麼。

Tool 負責：

> 真正執行查詢或轉換。

不要把每一個功能都做成 Agent。

Tool 必須：

- input 明確
- output 結構化
- 可單獨測試
- 不直接產生最終使用者回答

---

## 8. Data Ownership

### Supabase

Supabase 是 canonical source of truth，負責：

- structured official data
- raw source records
- RAG documents/chunks
- line user records
- request/evidence logs

### Neo4j

Neo4j 專門負責：

- knowledge relationships
- graph traversal

Neo4j 不要成為第二套完整產品主資料庫。

### RAG

RAG 使用 Supabase PostgreSQL + pgvector。

精確欄位優先 SQL 查詢。
非結構化官方文本才使用 RAG。

---

## 9. Risk Rule

禁止直接使用：

```text
有 DB = 低風險
沒 DB = 中風險
沒 DB + 宣稱 = 高風險
```

必須先分開判斷：

1. Product Verification
2. Claim Assessment
3. Evidence Strength

最後才由 deterministic Risk Engine 合成：

```text
LOW
MEDIUM
HIGH
```

重要：

- `NOT_FOUND` 不等於假貨。
- 官方有登記的產品，也可能出現不當廣告宣稱。
- LLM 負責語意抽取，不應自由決定最後風險等級。
- Risk Engine 必須可測試且規則透明。

---

## 10. No Hard-coding

禁止針對個別測試題、產品名稱、過去 Bug 或預期答案在 production code hard-code。

例如禁止用：

```python
if "某個測試產品" in text:
    return "固定答案"
```

也不要以特定完整問句作為 business logic。

允許：

- 正規化的一般性規則
- enum
- schema validation
- 通用風險條件
- tests/fixtures 中明確標示的 synthetic data

---

## 11. Source Traceability

所有可作為證據的重要資料盡量保留：

- source_name
- source_type
- source_url
- source_record_id
- retrieved_at
- raw_data / raw metadata
- official flag

系統不得把模型既有知識冒充成已查到的官方資料。

---

## 12. Official Web Scope

Phase 1 外部查證只允許台灣官方來源。

至少以這類官方來源為目標：

```text
fda.gov.tw
data.fda.gov.tw
mohw.gov.tw
data.gov.tw
```

不要把：

- 部落格
- 論壇
- 購物網站
- 社群貼文
- 品牌宣傳頁

當成主要官方證據。

如果使用者提供的是廣告或社群 URL，可以抽取「對方宣稱了什麼」，但查證仍必須回到官方證據。

---

## 13. Language

所有：

- 開發回報
- README
- 重要註解
- 使用者可見回答

以繁體中文為主。

程式識別字、class、function、enum、schema 欄位維持清楚一致的英文命名。

---

## 14. Development Behavior

每個階段：

1. 先理解需求。
2. 再修改程式。
3. 執行 relevant tests。
4. 顯示實際測試結果。
5. 不要只宣稱「完成」。
6. 若外部 credentials 缺失，相關 integration test 可 skip，但其餘工作繼續。
7. 不要因單一失敗案例就往 production code 塞特例。

---

## 15. Security

禁止：

- hard-code API keys
- commit `.env`
- log 完整 token
- log passwords
- 將 service role key 回傳給 client
- 將 LINE token 放入測試 fixture

`.env.example` 只能放變數名稱或安全 placeholder。

---

## 16. Current Goal

目前目標不是資料量最大化。

目前目標是先讓最小 vertical slice 正確運作：

```text
Input
→ Agent A
→ Agent B + Tools
→ EvidenceBundle
→ Risk Engine
→ Agent C
→ Response
```

然後逐步加入：

```text
text
image
audio
URL
LINE
Supabase
Neo4j
RAG
Taiwan official fallback
```

先求架構正確，再擴大台灣資料。

---

## 17. Stability Rules（Frozen Contracts）

已完成的行為契約列在 `docs/FROZEN_CONTRACTS.md`，每項都有 regression test。

- New features must extend through existing interfaces.
- Frozen contracts cannot be changed without explicit approval.
- Real-world failed examples are regression cases, never production rules.
- If a new feature breaks frozen behavior, fix or revert the new feature first.
- Before editing frozen files, explain why the extension point is insufficient.
