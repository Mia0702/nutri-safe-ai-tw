"""GPT vs 呷安心 比較評估 — 只有「結構」，本階段不呼叫任何 GPT / OpenAI。

之後的 Phase 會用同一份 gold cases 比較：
  * vanilla GPT 的回答（沒有官方資料）
  * 呷安心的回答
這裡只定義：gold case schema、答案紀錄 schema、評分欄位，以及產生「待填答案」的空白檔。
沒有任何 live 呼叫；答案欄位由後續 Phase 填入。

Gold case 的 `gold` 欄位只放「可從官方資料客觀驗證」的事實（例如：此名稱是否存在官方紀錄），
不放任何由模型判斷的結論。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from scripts.eval.common import build_report, default_output_path, sha256_of, write_report

EVAL_NAME = "gpt_vs_nutrisafe_template"
SYSTEMS = ("vanilla_gpt", "nutrisafe")


class GoldFacts(BaseModel):
    """可由官方資料客觀驗證的事實；None = 此案例不評這一項。"""

    product_exists_in_official_data: bool | None = None
    expected_official_product_id: str | None = None
    approved_function_names: list[str] | None = None
    claim_is_medical_treatment: bool | None = None
    must_not_state_as_official: list[str] = Field(default_factory=list)  # 沒有官方證據就不該斷言的句子重點


class GoldCase(BaseModel):
    case_id: str
    category: Literal["product_identity", "approved_function", "claim_verdict", "ingredient", "interaction", "other"]
    input_text: str
    gold: GoldFacts
    dataset_note: str = ""


class SystemAnswer(BaseModel):
    """後續 Phase 填入；這個階段全部為 None。"""

    system: Literal["vanilla_gpt", "nutrisafe"]
    case_id: str
    answer_text: str | None = None
    # 評分欄位（人工或規則評分，後續定義）
    fabricated_official_claim: bool | None = None  # 把沒有官方證據的內容說成官方結論
    identity_correct: bool | None = None
    cited_official_source: bool | None = None
    notes: str | None = None


def blank_answer_sheet(cases: list[GoldCase]) -> list[SystemAnswer]:
    return [SystemAnswer(system=s, case_id=c.case_id) for c in cases for s in SYSTEMS]


def build_template(cases: list[GoldCase]) -> dict[str, Any]:
    sheet = blank_answer_sheet(cases)
    return build_report(
        EVAL_NAME,
        dataset_versions={"gold_cases": {"case_count": len(cases),
                                         "sha256": sha256_of([c.model_dump(mode="json") for c in cases])}},
        parameters={"systems": list(SYSTEMS), "live_calls": False},
        metrics={"answers_pending": len(sheet), "answers_filled": 0},
        details={"cases": [c.model_dump(mode="json") for c in cases],
                 "answers": [a.model_dump(mode="json") for a in sheet]},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="產生 GPT-vs-呷安心 比較的空白答案檔（不呼叫任何 GPT）")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    example = [GoldCase(case_id="example_01", category="product_identity", input_text="虛構測試牌甲膠囊是什麼？",
                        gold=GoldFacts(product_exists_in_official_data=False), dataset_note="schema 範例（SYNTHETIC）")]
    out = write_report(build_template(example), args.output or default_output_path(EVAL_NAME), overwrite=args.overwrite)
    print(f"空白答案檔：{out}（未呼叫任何 GPT）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
