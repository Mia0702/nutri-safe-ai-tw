# 呷安心 — 全新 Greenfield 專案起始包

這個資料夾是全新的專案骨架，不含任何舊版「呷安心」程式碼、資料表、Agent、測試或 workaround。

## 建議開發方式

使用：

- Windows
- VS Code
- VS Code「Claude Code」擴充套件
- Python virtual environment
- Git

你不需要先安裝 Claude Desktop。
若只使用 VS Code 裡的 Claude Code 面板，也不必先安裝 standalone Claude Code CLI。

---

## 第 1 步：解壓縮後用 VS Code 打開

把整個 `ji-anxin-starter` 資料夾放到你要開發的位置。

例如：

```text
C:\Users\你的名字\Documents\ji-anxin
```

建議解壓縮後把資料夾重新命名成：

```text
ji-anxin
```

在 VS Code 選：

```text
File → Open Folder → ji-anxin
```

---

## 第 2 步：安裝 VS Code 擴充套件

VS Code 應該會依 `.vscode/extensions.json` 建議你安裝：

- Claude Code
- Python
- Pylance

如果沒有跳出提示：

1. 按 `Ctrl + Shift + X`
2. 搜尋 `Claude Code`
3. 安裝 Anthropic 的 Claude Code 擴充套件
4. 登入你的 Claude 帳號

---

## 第 3 步：初始化 Python 環境

在 VS Code Terminal 執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

這個腳本會：

- 檢查 Python
- 建立 `.venv`
- 安裝 `requirements.txt`
- 建立 `.env`（若尚未存在）
- 初始化 Git（若尚未初始化）
- 若找得到 VS Code CLI，嘗試安裝建議的擴充套件

---

## 第 4 步：填入環境變數

打開：

```text
.env
```

將之後需要的金鑰填入。

注意：

- `.env` 已經被 `.gitignore` 排除。
- 不要把 API key 貼進 `CLAUDE.md`
- 不要把 API key 貼進 Prompt
- 不要 commit `.env`

第一輪沒有全部 credentials 也沒關係。
Claude 應該把缺少外部連線的 integration tests 標示為 skipped，而不是停止整個開發。

---

## 第 5 步：啟動 Claude Code

在 VS Code 開啟 Claude Code 面板。

把 `PROMPTS/00_FIRST_MESSAGE_TO_CLAUDE.md` 裡的第一段訊息貼給 Claude。

Claude 應該先閱讀：

```text
CLAUDE.md
PROMPTS/01_GREENFIELD_PHASE1.md
```

然後才開始寫程式。

---

## 第 6 步：你要怎麼監督 Claude

這次不要只看 Claude 說「完成」。

每個階段都確認：

1. 左側檔案有真的建立。
2. Source Control 有顯示它改了哪些檔案。
3. 測試有真的執行。
4. 失敗的測試要留下原因。
5. 不要讓 Claude 為了讓測試過關把特定產品、問題或答案 hard-code 進 production code。
6. `.env` 不可以進 Git。
7. 正式資料不可使用假的產品 fixture。

建議每完成一個穩定階段就 commit 一次。

---

## 第一個今日目標

今天不是匯入全台灣資料。

今天先做到一個最小但完整的 vertical slice：

```text
輸入
→ OpenAI 理解
→ Tool 決定
→ Supabase / Neo4j / RAG / 官方來源其中需要的資料來源
→ Evidence Bundle
→ Risk Engine
→ Reporter
→ 回覆
```

先以少量台灣官方真實資料驗證架構，再擴大資料量。

---

## 專案原則

完整規則請看：

```text
CLAUDE.md
```

完整 Phase 1 開發任務請看：

```text
PROMPTS/01_GREENFIELD_PHASE1.md
```
