"""Interaction retrieval service（deterministic、唯讀；Supabase 為 canonical source）。

三類查詢，全部以 canonical entity ID 進行（不用 display name 猜 identity）：
A. Ingredient ↔ Ingredient、B. Ingredient ↔ DrugSubstance：pairwise interaction（只查 `record_kind = PAIRWISE_INTERACTION`）。
C. Product ↔ Product 重複成分：產品先各自經 Bridge 對應到已確認的 IngredientForm / NutrientGroup，再比對營養素群
   （form → group 只經 CONTRIBUTES_TO_NUTRIENT lineage）。**不建立、不查詢 Product↔Product interaction。**

結論資格（`conclusion_status`）完全由 corpus 資料欄位與 identity 對應方式決定，不呼叫 LLM、不產生句子：
* 只有 `deterministic_conclusion_allowed`（READY ＋ DECISION_ELIGIBLE ＋ 非 CONFLICTING）且有支持的 DECISION_ELIGIBLE evidence
  ＋ identity 直接對應，才是 CONCLUSION_ELIGIBLE；仍附 applicability 條件原文（`evaluated = False`）。
* 只經 form → group 對應、而 Bridge link 的 `automatic_interaction_inheritance` 不是 true → LINEAGE_NOT_INHERITED（只說明）。
* EVIDENCE_CONFLICT / NEEDS_REVIEW / SUPPORTING / CONTEXT_ONLY 永遠不是肯定結論。
沒有 corpus 紀錄的組合 → `PairStatus.NO_CORPUS_RECORD`（不是「沒有交互作用」）。
重複成分只代表「重複存在」：dose_evaluation = NOT_EVALUATED、severity = DOSE_DEPENDENT_NOT_ASSIGNED，並列出禁止推論事項。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from app.schemas.enums import IngredientQueryResolution
from app.schemas.evidence import ProductIngredientEvidence
from app.schemas.interaction import (
    SUPPORTING_ROLES,
    Applicability,
    DuplicateCoverage,
    DuplicateFinding,
    DuplicateProductRef,
    EntityRefOut,
    InteractionConclusionStatus,
    InteractionEvidenceItem,
    InteractionFinding,
    InteractionParticipant,
    InteractionSubjectKind,
    PairCoverage,
    PairStatus,
    SubjectMatch,
    SubjectResolutionStatus,
    direction_role,
)
from app.tools.ingredient_tools import IngredientReference, query_key

PAIRWISE = "PAIRWISE_INTERACTION"
DUPLICATE_RULE = "DUPLICATE_INTAKE_RULE"
DIRECT, VIA_FORM = "DIRECT", "VIA_FORM_TO_GROUP"
GROUP_KIND, FORM_KIND, DRUG_KIND = "NutrientGroup", "IngredientForm", "DrugSubstance"
DISPLAY_ONLY_POLICY = "DISPLAY_ONLY_NOT_AUTOMERGE"
EXACT_POLICY = "EXACT_AT_DECLARED_IDENTITY_LEVEL"
MAPPED_REVIEWED = "MAPPED_REVIEWED"


class InteractionRepository(Protocol):
    """Interaction corpus 的唯讀資料存取（Supabase 實作見 app.services.supabase_service）。"""

    def interaction_corpus_version(self) -> str | None:
        """完成匯入的 corpus 版本（app_schema_meta 完成標記）；沒有 → None（尚未可用，不是查詢失敗）。"""

    def get_drug_reference(self) -> dict[str, Any]:
        """{"drugs": [drug_substances 列], "aliases": [drug_substance_aliases 列]}。"""

    def get_interactions_involving(self, entity_ids: list[str]) -> list[dict[str, Any]]:
        """含任一 entity 作為參與者的 interaction 列，每列附 `participants`（該 interaction 的全部參與者列）。"""

    def get_interaction_evidence(self, interaction_ids: list[str]) -> list[dict[str, Any]]: ...

    def get_form_nutrient_links(self, form_entity_ids: list[str]) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class EntityHandle:
    """一個已確認的 canonical entity，以及它是怎麼對應到使用者對象的。"""

    entity_id: str
    kind: str                       # NutrientGroup | IngredientForm | DrugSubstance
    match_basis: str = DIRECT       # DIRECT | VIA_FORM_TO_GROUP
    via_entity_id: str | None = None


@dataclass
class Side:
    """interaction 比較的一邊（一個使用者對象的全部已確認 entity）。"""

    label: str
    handles: list[EntityHandle] = field(default_factory=list)

    def best_handles(self) -> dict[str, EntityHandle]:
        """同一 entity 同時有 DIRECT 與 VIA 時以 DIRECT 為準。"""
        out: dict[str, EntityHandle] = {}
        for h in self.handles:
            cur = out.get(h.entity_id)
            if cur is None or (cur.match_basis != DIRECT and h.match_basis == DIRECT):
                out[h.entity_id] = h
        return out


class InteractionService:
    def __init__(self, repo: InteractionRepository, bridge_reference: dict[str, Any] | None = None):
        self._repo = repo
        self._ref = IngredientReference(bridge_reference or {})
        self._drug_ref: dict[str, Any] | None = None
        self.corpus_version: str | None = repo.interaction_corpus_version()

    @property
    def available(self) -> bool:
        return self.corpus_version is not None

    # ------------------------------------------------------------------ identity

    def _drugs(self) -> dict[str, Any]:
        if self._drug_ref is None:
            raw = self._repo.get_drug_reference() or {}
            drugs = {d["drug_substance_id"]: d for d in raw.get("drugs") or []}
            formal: dict[str, set[str]] = {}
            for d in drugs.values():
                keys = [d.get("canonical_name_en")]
                if d.get("taiwan_code_mapping_status") == MAPPED_REVIEWED:  # 官方中文名只有 TFDA 對應完成後才可用於比對
                    keys.append(d.get("official_name_zh"))
                for k in keys:
                    if query_key(k):
                        formal.setdefault(query_key(k), set()).add(d["drug_substance_id"])
            for a in raw.get("aliases") or []:
                if a.get("match_policy") == EXACT_POLICY and a.get("drug_substance_id") in drugs and query_key(a.get("alias_text")):
                    formal.setdefault(query_key(a["alias_text"]), set()).add(a["drug_substance_id"])
            self._drug_ref = {"drugs": drugs, "formal": formal}
        return self._drug_ref

    def drug_ref(self, drug_id: str) -> EntityRefOut | None:
        d = self._drugs()["drugs"].get(drug_id)
        if d is None:
            return None
        return EntityRefOut(entity_id=drug_id, kind=DRUG_KIND, canonical_name_en=d["canonical_name_en"],
                            display_name_zh=d.get("official_name_zh"), identity_level=d.get("identity_level"))

    def ingredient_ref(self, entity_id: str) -> EntityRefOut | None:
        r = self._ref.ref(entity_id)
        if r is None:
            return None
        return EntityRefOut(entity_id=entity_id, kind=r.entity_type, canonical_name_en=r.canonical_name_en,
                            display_name_zh=r.display_name_zh, identity_level=r.identity_level)

    def entity_ref(self, entity_id: str) -> EntityRefOut | None:
        return self.drug_ref(entity_id) or self.ingredient_ref(entity_id)

    def known_entity_ids(self) -> set[str]:
        return set(self._ref.entities) | set(self._drugs()["drugs"])

    def resolve_named(self, text: str, kind: InteractionSubjectKind) -> tuple[SubjectResolutionStatus, list[EntityRefOut]]:
        """使用者寫法 → canonical identity（成分 Bridge ＋ 藥品 substance；exact，不看相似度、不翻譯、不猜）。

        kind 只是 Agent A 的線索：成分與藥品兩邊都查，同一寫法對到兩邊 / 多個 identity → AMBIGUOUS。
        """
        resolution, ingredient_id = self._ref.resolve_query(text) if self._ref.available else (
            IngredientQueryResolution.NOT_IN_CORPUS, None)
        drug_ids = sorted(self._drugs()["formal"].get(query_key(text), set()))
        if resolution == IngredientQueryResolution.AMBIGUOUS or len(drug_ids) > 1 or (
                resolution == IngredientQueryResolution.RESOLVED and drug_ids):
            return SubjectResolutionStatus.AMBIGUOUS, []
        if resolution == IngredientQueryResolution.RESOLVED and ingredient_id:
            return SubjectResolutionStatus.RESOLVED, [self.ingredient_ref(ingredient_id)]
        if drug_ids:
            return SubjectResolutionStatus.RESOLVED, [self.drug_ref(drug_ids[0])]
        if kind == InteractionSubjectKind.DRUG:  # 沒有可靠的藥品 → 活性成分對應（商品名 / 譯名）：不翻譯、不猜
            return SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED, []
        return SubjectResolutionStatus.UNRESOLVED, []

    @staticmethod
    def side_from_entities(label: str, entities: Iterable[EntityRefOut]) -> Side:
        return Side(label, [EntityHandle(e.entity_id, e.kind) for e in entities])

    def side_from_product(self, label: str, evidence: ProductIngredientEvidence) -> tuple[Side, dict[str, list[str]]]:
        """已確認產品 → 已確認的 IngredientForm / NutrientGroup（直接對應為 DIRECT；form → group 推導為 VIA_FORM_TO_GROUP）。

        只用正式 mapping（VERIFIED_EXACT / SUPPORTED_ALIAS / FORM_TO_GROUP）；候選與未解析成分不參與。
        """
        handles, via_forms = [], {}
        for c in evidence.confirmed_ingredients:
            eid = c.entity.entity_id
            if c.is_direct:
                handles.append(EntityHandle(eid, c.entity.entity_type))
            else:
                via = c.via_entity.entity_id if c.via_entity else None
                handles.append(EntityHandle(eid, c.entity.entity_type, VIA_FORM, via))
                if via:
                    via_forms.setdefault(eid, []).append(via)
        return Side(label, handles), via_forms

    # ------------------------------------------------------------------ pairwise

    def _inherit_allowed(self, form_ids: list[str], links: dict[str, dict[str, Any]]) -> bool:
        return bool(form_ids) and all(bool((links.get(f) or {}).get("automatic_interaction_inheritance")) for f in form_ids)

    def pairwise(self, sides: list[Side]) -> tuple[list[InteractionFinding], list[PairCoverage]]:
        """所有兩兩 side 組合的 pairwise interaction（只用 canonical entity ID）。"""
        ids = sorted({h.entity_id for s in sides for h in s.handles})
        rows = [r for r in self._repo.get_interactions_involving(ids) if r.get("record_kind") == PAIRWISE] if ids else []
        pair_coverage: list[PairCoverage] = []
        matched_forms: set[str] = set()
        matches: list[tuple[dict[str, Any], tuple[int, int], list[tuple[EntityHandle, EntityHandle]]]] = []
        for i, j in itertools.combinations(range(len(sides)), 2):
            a_handles, b_handles = sides[i].best_handles(), sides[j].best_handles()
            for row in rows:
                parts = {p["role"]: (p.get("ingredient_entity_id") or p.get("drug_substance_id")) for p in row["participants"]}
                ends = set(parts.values())
                if len(ends) != 2:
                    continue
                hits = [(a, b) for a in a_handles.values() for b in b_handles.values() if {a.entity_id, b.entity_id} == ends]
                if hits:
                    matches.append((row, (i, j), hits))
                    matched_forms.update(h.via_entity_id for pair in hits for h in pair if h.via_entity_id)
        links = {l["form_entity_id"]: l for l in self._repo.get_form_nutrient_links(sorted(matched_forms))} if matched_forms else {}
        evidence = self._evidence([m[0]["interaction_id"] for m in matches])
        findings: dict[str, InteractionFinding] = {}
        found_for_pair: dict[tuple[int, int], list[str]] = {}
        for row, pair, hits in matches:
            iid = row["interaction_id"]
            direct_hits = [h for h in hits if h[0].match_basis == DIRECT and h[1].match_basis == DIRECT]
            used = direct_hits or hits
            identity_match = DIRECT if direct_hits else VIA_FORM
            via_forms = [h.via_entity_id for pair_hits in used for h in pair_hits if h.via_entity_id]
            ev = evidence.get(iid, [])
            status = self.conclusion_status(row, ev, identity_match, self._inherit_allowed(via_forms, links))
            subject_matches = [
                SubjectMatch(subject_text=sides[idx].label, entity_id=h.entity_id, match_basis=h.match_basis, via_entity_id=h.via_entity_id)
                for (a, b) in used[:1] for idx, h in ((pair[0], a), (pair[1], b))]
            found_for_pair.setdefault(pair, []).append(iid)
            if iid in findings:  # 同一 interaction 被不同 side 組合命中：保留 identity 較直接的那一筆
                cur = findings[iid]
                if cur.applicability.identity_match == DIRECT or identity_match != DIRECT:
                    continue
            findings[iid] = self._finding(row, ev, status, identity_match, subject_matches)
        for (i, j) in itertools.combinations(range(len(sides)), 2):
            found = sorted(set(found_for_pair.get((i, j), [])))
            pair_coverage.append(PairCoverage(subject_a=sides[i].label, subject_b=sides[j].label,
                                              status=PairStatus.RECORDS_FOUND if found else PairStatus.NO_CORPUS_RECORD,
                                              interaction_ids=found))
        return sorted(findings.values(), key=lambda f: f.interaction_id), pair_coverage

    @staticmethod
    def conclusion_status(row: dict[str, Any], evidence: list[InteractionEvidenceItem], identity_match: str,
                          inherit_allowed: bool) -> InteractionConclusionStatus:
        """優先序：衝突 > 待審 > 資料欄位不允許 / 無支持的 eligible evidence > lineage 不繼承 > eligible。"""
        if row.get("review_status") == "EVIDENCE_CONFLICT" or row.get("certainty") == "CONFLICTING":
            return InteractionConclusionStatus.EVIDENCE_CONFLICT
        if row.get("review_status") == "NEEDS_REVIEW":
            return InteractionConclusionStatus.NEEDS_REVIEW
        if row.get("record_kind") != PAIRWISE or not row.get("deterministic_conclusion_allowed") \
                or row.get("evidence_use") != "DECISION_ELIGIBLE":
            return InteractionConclusionStatus.INSUFFICIENT_FOR_CONCLUSION
        if not any(e.evidence_use == "DECISION_ELIGIBLE" and e.counts_as_support for e in evidence):
            return InteractionConclusionStatus.INSUFFICIENT_FOR_CONCLUSION
        if identity_match != DIRECT and not inherit_allowed:
            return InteractionConclusionStatus.LINEAGE_NOT_INHERITED
        return InteractionConclusionStatus.CONCLUSION_ELIGIBLE

    def _finding(self, row: dict[str, Any], evidence: list[InteractionEvidenceItem], status: InteractionConclusionStatus,
                 identity_match: str, subject_matches: list[SubjectMatch]) -> InteractionFinding:
        participants = [
            InteractionParticipant(role=p["role"], entity=self.entity_ref(p.get("ingredient_entity_id") or p.get("drug_substance_id"))
                                   or EntityRefOut(entity_id=p.get("ingredient_entity_id") or p.get("drug_substance_id") or "",
                                                   kind=p.get("participant_kind") or "", canonical_name_en=""))
            for p in sorted(row["participants"], key=lambda p: p["role"])]
        return InteractionFinding(
            interaction_id=row["interaction_id"], record_kind=row["record_kind"], interaction_type=row["interaction_type"],
            directionality=row["directionality"], participants=participants, subject_matches=subject_matches,
            severity=row["severity"], certainty=row["certainty"], evidence_level=row["evidence_level"],
            evidence_use=row["evidence_use"], review_status=row["review_status"], decision_mode=row["decision_mode"],
            deterministic_conclusion_allowed=bool(row.get("deterministic_conclusion_allowed")), conclusion_status=status,
            applicability=Applicability(conditions=row.get("applicability_conditions") or "",
                                        dose_context=row.get("dose_context"), population_context=row.get("population_context"),
                                        timing_advice=row.get("timing_advice"), identity_match=identity_match),
            clinical_effect=row.get("clinical_effect"), mechanism_summary=row.get("mechanism_summary"),
            recommendation=row.get("recommendation"), evidence=evidence,
            supporting_evidence_count=sum(e.counts_as_support for e in evidence),
            non_supporting_evidence_count=sum(not e.counts_as_support for e in evidence),
            corpus_version=row.get("corpus_version"))

    # ------------------------------------------------------------------ evidence

    def _evidence(self, interaction_ids: list[str]) -> dict[str, list[InteractionEvidenceItem]]:
        out: dict[str, list[InteractionEvidenceItem]] = {}
        if not interaction_ids:
            return out
        for e in self._repo.get_interaction_evidence(sorted(set(interaction_ids))):
            role = direction_role(e.get("supports_direction"))
            out.setdefault(e["interaction_id"], []).append(InteractionEvidenceItem(
                evidence_id=e["evidence_id"], source_id=e["source_id"], source_title=e.get("title"), authority=e.get("authority"),
                source_type=e["source_type"], is_official=bool(e.get("is_official")), study_type=e.get("study_type"),
                supports_direction=e["supports_direction"], direction_role=role, counts_as_support=role in SUPPORTING_ROLES,
                evidence_use=e["evidence_use"], evidence_level=e["evidence_level"], certainty=e["certainty"],
                retrieved_at=str(e["retrieved_at"]) if e.get("retrieved_at") else None,
                source_retrieval_status=e["source_retrieval_status"], source_url=e.get("source_url"),
                article_or_section=e.get("article_or_section"), population=e.get("population"),
                applicability_scope=e.get("applicability_scope"), result_summary_zh=e.get("result_summary_zh"),
                limitations_zh=e.get("limitations_zh")))
        for items in out.values():
            items.sort(key=lambda i: i.evidence_id)
        return out

    def evidence_for(self, interaction_ids: list[str]) -> dict[str, list[InteractionEvidenceItem]]:
        return self._evidence(interaction_ids)

    # ------------------------------------------------------------------ duplicate ingredients

    def _group_of(self, entity: EntityRefOut) -> str:
        if entity.kind == GROUP_KIND:
            return entity.entity_id
        return self._ref.parent_id(entity.entity_id) or entity.entity_id

    def duplicates(self, products: list[tuple[str, str | None, ProductIngredientEvidence]],
                   focus_entity_ids: list[str] | None = None) -> tuple[list[DuplicateFinding], list[DuplicateCoverage]]:
        """兩個以上已確認產品對應到同一營養素群（只用正式 mapping；form → group 依 Bridge lineage）。"""
        per_group: dict[str, dict[str, set[str]]] = {}
        names: dict[str, str | None] = {}
        coverage = []
        for pid, name, ev in products:
            names[pid] = name
            coverage.append(DuplicateCoverage(product_id=pid, official_count=ev.coverage.official_count,
                                              confirmed_count=ev.coverage.confirmed_count,
                                              unresolved_count=ev.coverage.unresolved_count))
            for c in ev.confirmed_ingredients:
                ref = self.ingredient_ref(c.entity.entity_id)
                if ref is None:
                    continue
                group = self._group_of(ref)
                via = c.via_entity.entity_id if c.via_entity else c.entity.entity_id
                per_group.setdefault(group, {}).setdefault(pid, set()).add(via)
        focus_groups = None
        if focus_entity_ids:
            focus_groups = {self._group_of(r) for r in (self.ingredient_ref(i) for i in focus_entity_ids) if r}
        rules = self._duplicate_rules(sorted(g for g, p in per_group.items() if len(p) >= 2))
        out = []
        for group in sorted(per_group):
            by_product = per_group[group]
            if len(by_product) < 2 or (focus_groups is not None and group not in focus_groups):
                continue
            nutrient = self.ingredient_ref(group)
            if nutrient is None:
                continue
            via_sets = [frozenset(v) for v in by_product.values()]
            rule = rules.get(group)
            rule_evidence = self._evidence([rule["interaction_id"]]).get(rule["interaction_id"], []) if rule else []
            out.append(DuplicateFinding(
                nutrient=nutrient,
                products=[DuplicateProductRef(product_id=pid, product_name=names.get(pid),
                                              via_entities=[r for r in (self.ingredient_ref(e) for e in sorted(via)) if r])
                          for pid, via in sorted(by_product.items())],
                same_identity_level_across_products=len(set(via_sets)) == 1,
                shared_entity_ids=sorted(frozenset.intersection(*via_sets)),
                duplicate_rule_id=rule["interaction_id"] if rule else None, duplicate_rule_evidence=rule_evidence,
                deterministic_conclusion_allowed=bool(rule and rule.get("deterministic_conclusion_allowed")),
            ))
        return out, coverage

    def _duplicate_rules(self, group_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not group_ids:
            return {}
        out = {}
        for row in self._repo.get_interactions_involving(group_ids):
            if row.get("record_kind") != DUPLICATE_RULE:
                continue
            for p in row["participants"]:
                if p.get("role") == "SELF" and p.get("ingredient_entity_id") in group_ids:
                    out[p["ingredient_entity_id"]] = row
        return out
