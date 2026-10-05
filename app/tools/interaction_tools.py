"""Interaction Evidence Tools（唯讀、輸入 canonical ID、輸出結構化；不產生最終使用者回答）。

* lookup_interactions：兩個以上「已確認 identity」的對象之間的 pairwise interaction（Ingredient↔Ingredient、Ingredient↔Drug；
  產品先經 Ingredient Bridge 對應到已確認的 entity 再比較，沒有 Product↔Product interaction）。
* lookup_duplicate_ingredients：兩個以上已確認產品是否對應到同一營養素群（重複成分存在；不評估劑量）。
* lookup_interaction_evidence：已查到的 interaction 的逐筆證據（supports_direction 原樣保留）。
安全規則（結論資格、lineage、conflict、NO_CORPUS_RECORD）在 app.services.interaction_service，tool 只負責呼叫與包裝。
"""

from pydantic import BaseModel, Field

from app.schemas.evidence import ProductIngredientEvidence
from app.schemas.interaction import (
    DuplicateCoverage,
    DuplicateFinding,
    InteractionEvidenceItem,
    InteractionFinding,
    PairCoverage,
)
from app.services.interaction_service import InteractionService, Side


class InteractionLookupResult(BaseModel):
    findings: list[InteractionFinding] = Field(default_factory=list)
    pairs: list[PairCoverage] = Field(default_factory=list)

    @property
    def result_count(self) -> int:
        return len(self.findings)


class DuplicateLookupResult(BaseModel):
    duplicates: list[DuplicateFinding] = Field(default_factory=list)
    coverage: list[DuplicateCoverage] = Field(default_factory=list)
    products_without_ingredient_evidence: list[str] = Field(default_factory=list)

    @property
    def result_count(self) -> int:
        return len(self.duplicates)


class InteractionEvidenceResult(BaseModel):
    evidence: dict[str, list[InteractionEvidenceItem]] = Field(default_factory=dict)

    @property
    def result_count(self) -> int:
        return sum(len(v) for v in self.evidence.values())


def lookup_interactions(service: InteractionService, sides: list[Side]) -> InteractionLookupResult:
    findings, pairs = service.pairwise(sides)
    return InteractionLookupResult(findings=findings, pairs=pairs)


def lookup_duplicate_ingredients(
    service: InteractionService, products: list[tuple[str, str | None, ProductIngredientEvidence | None]],
    focus_entity_ids: list[str] | None = None,
) -> DuplicateLookupResult:
    usable = [(pid, name, ev) for pid, name, ev in products if ev is not None]
    duplicates, coverage = service.duplicates(usable, focus_entity_ids)
    return DuplicateLookupResult(duplicates=duplicates, coverage=coverage,
                                 products_without_ingredient_evidence=[pid for pid, _, ev in products if ev is None])


def lookup_interaction_evidence(service: InteractionService, interaction_ids: list[str]) -> InteractionEvidenceResult:
    return InteractionEvidenceResult(evidence=service.evidence_for(interaction_ids))
