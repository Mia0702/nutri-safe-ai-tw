# 第一個貼給 Claude Code 的訊息

請將下面整段貼到 VS Code 的 Claude Code：

---

請先完整閱讀 `CLAUDE.md` 與 `PROMPTS/01_GREENFIELD_PHASE1.md`。

這是一個全新的 Greenfield 專案。不要搜尋、參考、搬移或沿用任何舊版呷安心專案內容。

請先檢查目前這個 workspace 的檔案，確認它只有新的 starter scaffold。接著依照 `PROMPTS/01_GREENFIELD_PHASE1.md` 從 Phase 0 開始實作。

執行原則：

1. 不要只給我設計建議，直接在目前 workspace 建立與修改檔案。
2. 每完成一個小階段就執行對應測試。
3. 不要為了通過測試 hard-code 個別測試問句、產品名稱或答案。
4. 缺少 OpenAI、Supabase、Neo4j 或 LINE credentials 時，不要停止開發；將需要外部服務的測試明確標示 skipped，先完成可離線驗證的部分。
5. 回報全部使用繁體中文。
6. 不要一次無限制擴張功能；先完成 Phase 0 → Phase 1 的最小 vertical slice，再繼續 multimodal 與 LINE。
7. 每一輪回報：實際修改檔案、實際執行的測試、成功/失敗/skipped 數量，以及下一步。

現在開始。
