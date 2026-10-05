"""TEST_ONLY — Interaction corpus 的虛構資料（TI_ / TD_ / TE_ 前綴；與 test_ingredient_retrieval_runtime 的 TN_ / TF_ entity 對接）。

不含任何真實藥品 / 交互作用。欄位與 0006 schema（interactions / interaction_participants / interaction_evidence /
drug_substances / interaction_form_nutrient_links）一致，讓 service 的判斷邏輯可以離線測試。
"""

CORPUS_VERSION = "T1.0"

DRUGS = [
    {"drug_substance_id": "TD_A", "canonical_name_en": "Testdrug-A", "official_name_zh": None, "identity_level": "ACTIVE_MOIETY",
     "parent_active_moiety_id": None, "taiwan_code_mapping_status": "NEEDS_REVIEW"},
    {"drug_substance_id": "TD_ANTICOAG", "canonical_name_en": "Testdrug-Anticoag", "official_name_zh": None,
     "identity_level": "ACTIVE_MOIETY", "parent_active_moiety_id": None, "taiwan_code_mapping_status": "NEEDS_REVIEW"},
    {"drug_substance_id": "TD_B", "canonical_name_en": "Testdrug-B", "official_name_zh": None, "identity_level": "ACTIVE_MOIETY",
     "parent_active_moiety_id": None, "taiwan_code_mapping_status": "NEEDS_REVIEW"},
]
DRUG_ALIASES = [
    {"alias_id": "TDA1", "drug_substance_id": "TD_A", "alias_text": "Testdrug-A", "language": "en", "match_policy": "EXACT_AT_DECLARED_IDENTITY_LEVEL"},
    {"alias_id": "TDA2", "drug_substance_id": "TD_A", "alias_text": "測試藥甲", "language": "zh-Hant", "match_policy": "DISPLAY_ONLY_NOT_AUTOMERGE"},
]

PAIRWISE, DUPLICATE = "PAIRWISE_INTERACTION", "DUPLICATE_INTAKE_RULE"


def _interaction(iid, a, b, *, kind=PAIRWISE, itype="ABSORPTION", direction="A_AFFECTS_B", severity="MODERATE",
                 certainty="MODERATE", evidence_use="DECISION_ELIGIBLE", review="READY", mode="CONDITIONAL_MESSAGE_ONLY",
                 allowed=True, conditions="TEST_ONLY 條件：僅在同時服用且間隔不足時適用。"):
    def part(role, entity):
        drug = entity.startswith("TD_")
        label = "DrugSubstance" if drug else ("NutrientGroup" if entity.startswith("TN_") else "IngredientForm")
        return {"interaction_id": iid, "role": role, "participant_kind": label,
                "ingredient_entity_id": None if drug else entity, "drug_substance_id": entity if drug else None}

    participants = [part("SELF", a)] if kind == DUPLICATE else [part("AFFECTING", a), part("AFFECTED", b)]
    return {
        "interaction_id": iid, "record_kind": kind, "interaction_type": "DUPLICATE_INTAKE" if kind == DUPLICATE else itype,
        "directionality": "SELF_ACCUMULATION" if kind == DUPLICATE else direction, "clinical_effect": "TEST_ONLY 效果",
        "severity": "DOSE_DEPENDENT_NOT_ASSIGNED" if kind == DUPLICATE else severity, "certainty": certainty,
        "evidence_level": "OFFICIAL_SYNTHESIS", "evidence_use": evidence_use, "decision_mode": mode, "review_status": review,
        "mechanism_summary": None, "recommendation": "TEST_ONLY 建議", "timing_advice": None, "dose_context": "TEST_ONLY 劑量背景",
        "population_context": None, "applicability_conditions": conditions,
        "dose_evaluation_default": "NOT_EVALUATED" if kind == DUPLICATE else "NOT_APPLICABLE",
        "deterministic_conclusion_allowed": allowed, "corpus_version": CORPUS_VERSION, "participants": participants,
    }


INTERACTIONS = [
    _interaction("TI_ZN_CU", "TN_ZN", "TN_CU"),
    _interaction("TI_ZN_DRUG", "TN_ZN", "TD_A"),
    # 魚油 vs 抗凝血藥：證據衝突（不可 deterministic）
    _interaction("TI_FISH_ANTICOAG", "TF_FISH_OIL", "TD_ANTICOAG", itype="PHARMACODYNAMIC", severity="POTENTIALLY_MAJOR",
                 certainty="CONFLICTING", evidence_use="NEEDS_REVIEW", review="EVIDENCE_CONFLICT",
                 mode="CONFLICT_EXPLANATION_ONLY", allowed=False),
    _interaction("TI_MG_DRUG", "TN_MG", "TD_A", evidence_use="SUPPORTING", review="NEEDS_REVIEW",
                 mode="LABEL_RECONCILIATION_REQUIRED", allowed=False),
    _interaction("TI_B9_CU", "TN_B9", "TN_CU", severity="CONTEXT_ONLY", certainty="CONTEXT_DEPENDENT", evidence_use="CONTEXT_ONLY",
                 mode="BACKGROUND_ONLY", allowed=False),
    # 群組層級宣告的 interaction：只經 form → group 對應的產品不自動繼承
    _interaction("TI_OMEGA3_DRUG", "TN_OMEGA3", "TD_B"),
    _interaction("TDUP_ZN", "TN_ZN", None, kind=DUPLICATE, certainty="CONDITIONAL_ON_VALID_INPUT",
                 mode="DUPLICATE_PRESENCE_OR_VALIDATED_ARITHMETIC_ONLY"),
    _interaction("TDUP_OMEGA3", "TN_OMEGA3", None, kind=DUPLICATE, certainty="CONDITIONAL_ON_VALID_INPUT",
                 mode="DUPLICATE_PRESENCE_OR_VALIDATED_ARITHMETIC_ONLY"),
]


def _evidence(eid, iid, direction="SUPPORTS", use="DECISION_ELIGIBLE", official=True, stype="OFFICIAL_FACT_SHEET"):
    return {
        "evidence_id": eid, "interaction_id": iid, "source_id": f"TS_{eid}", "source_type": stype, "authority": "TEST_ONLY 虛構機構",
        "is_official": official, "title": f"TEST_ONLY 來源 {eid}", "study_type": "OFFICIAL_EVIDENCE_SYNTHESIS", "pmid": None,
        "doi": None, "publication_date": None, "retrieved_at": "2026-01-01T00:00:00+00:00",
        "source_retrieval_status": "HTTP_200_CONTENT_VERIFIED", "population": None, "dose_context": None,
        "result_summary_zh": "TEST_ONLY 摘要", "limitations_zh": None, "supports_direction": direction,
        "evidence_level": "OFFICIAL_SYNTHESIS", "certainty": "NOT_FORMALLY_GRADED", "evidence_use": use, "review_status": "READY",
        "source_url": "https://example.invalid/interaction", "article_or_section": "TEST_ONLY", "applicability_scope": None,
    }


EVIDENCE = [
    _evidence("TE_1", "TI_ZN_CU"), _evidence("TE_2", "TI_ZN_DRUG"),
    _evidence("TE_3", "TI_FISH_ANTICOAG", "MIXED", "SUPPORTING", official=False, stype="PUBMED_ARTICLE"),
    _evidence("TE_4", "TI_FISH_ANTICOAG", "SUPPORTS", "SUPPORTING"),
    _evidence("TE_5", "TI_FISH_ANTICOAG", "NO_SIGNIFICANT_EFFECT", "SUPPORTING", official=False, stype="PUBMED_ARTICLE"),
    _evidence("TE_6", "TI_FISH_ANTICOAG", "CONTEXT_NOT_PAIR_SPECIFIC", "CONTEXT_ONLY"),
    _evidence("TE_7", "TI_MG_DRUG", "SUPPORTS", "SUPPORTING"), _evidence("TE_8", "TI_B9_CU", "SUPPORTS", "CONTEXT_ONLY"),
    _evidence("TE_9", "TI_OMEGA3_DRUG"), _evidence("TE_10", "TDUP_ZN"), _evidence("TE_11", "TDUP_OMEGA3"),
]

# form → group link：automatic_interaction_inheritance 目前一律 false（與 0005 CHECK 相同）
FORM_LINKS = [
    {"form_entity_id": "TF_FISH_OIL", "nutrient_entity_id": "TN_OMEGA3", "relationship_id": "TREL:TF_FISH_OIL",
     "automatic_interaction_inheritance": False, "amount_conversion_allowed": False},
    {"form_entity_id": "TF_EPA", "nutrient_entity_id": "TN_OMEGA3", "relationship_id": "TREL:TF_EPA",
     "automatic_interaction_inheritance": False, "amount_conversion_allowed": False},
    {"form_entity_id": "TF_ZN_GLUCONATE", "nutrient_entity_id": "TN_ZN", "relationship_id": "TREL:TF_ZN_GLUCONATE",
     "automatic_interaction_inheritance": False, "amount_conversion_allowed": False},
]
