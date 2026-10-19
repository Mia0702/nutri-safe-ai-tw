# scripts/eval — READ-ONLY 評估框架（Phase B）

目的：讓之後的任何修改都能和「凍結 baseline」比較。**不改 runtime、不寫任何 production 資料**
（Supabase 只經由 `ReadOnlySupabase`：只放行 select 與 `search_products` / `search_entity_aliases` RPC；
Neo4j、OpenAI 在預設流程中完全不碰）。報告只含計數、ID 與指標，沒有 secrets 或使用者原文。

| 評估 | 指令 | 需要 |
|---|---|---|
| Resolver identity safety | `python -m scripts.eval.resolver_eval --source live --sample-size 150 --seed 20261019` | Supabase（唯讀） |
| Resolver（離線） | `python -m scripts.eval.resolver_eval --source json --catalog-json <目錄.json>` | 無 |
| Claim verdict | `python -m scripts.eval.claim_eval` | 無（完全離線、deterministic） |
| Latency | `python -m scripts.eval.latency_report --log app.log` | 結構化 log 檔 |
| Controller 抽取 | `python -m scripts.eval.controller_eval --live --confirm-live --max-cases 5` | OpenAI（**opt-in，會產生費用**） |
| GPT vs 呷安心 | `python -m scripts.eval.gpt_baseline`（只產生空白答案檔） | 無（不呼叫 GPT） |

## 輸出

預設寫到 `eval_baselines/<name>-<UTC時間>.json`。**已存在的檔案不會被覆蓋**（需要時明確加 `--overwrite`）。
格式：

```json
{
  "evaluation_version": "1.0",
  "evaluation": "resolver_identity_safety",
  "created_at": "...", "git_commit": "...", "git_dirty": false,
  "dataset_versions": {"product_catalog": {"product_count": 9677, "sha256": "...", "by_category": {}}},
  "parameters": {"seed": 20261019, "sample_size": 150, "variants": []},
  "metrics": {"WRONG_AUTO_CONFIRM_RATE": 0.0}
}
```

記錄正式 baseline 時請在乾淨的 git 工作樹（`git_dirty: false`）、固定 seed 與樣本數下執行。

## Resolver 指標

最重要：`WRONG_AUTO_CONFIRM_RATE` = 自動確認成「不是原本那個產品」的案例 / 全部案例（分母固定，跨版本可比）。
另有 `wrong_auto_confirm_rate_among_auto_confirms`、`auto_confirm_precision`、`exact_match_correctness`、
`clarification_*`、`not_found_*` 與每種變形（完整品名、字號、省略一字、替換一字、截尾）的拆分。
ground truth = 官方品名相同的產品群；變形後剛好等於另一個官方品名的案例會被排除並計數（`skipped_cases`）。

## 限制

* Resolver 評估只涵蓋 deterministic resolver 階段（等同 Controller 完美抽取）；不含 Controller / planner / 圖片 / 語音誤差。
* Claim 案例是虛構 SYNTHETIC 資料，期望值取自 `docs/FROZEN_CONTRACTS.md`（FC-C1~C4）；不含 RAG / 官方網站 / 結構化 policy 證據路徑。
* Latency 只讀 `analysis_done` log，依 `planner_mode` 分組；沒有 response_type、LINE 排隊 / 送出與端到端耗時（見報告的 `known_missing_fields`）。
* Controller 評估的圖片案例（`requires_media`）本階段不執行。
