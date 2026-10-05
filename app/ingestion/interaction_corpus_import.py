"""Interaction Corpus importer：drug substance / interaction / participants / evidence → 0006 表（＋Neo4j）。

additive、idempotent：
- 只寫 0006 的五張表與 `app_schema_meta` 完成標記；**不寫** 0001–0005 的任何表（ingredient identity、sources 由 Ingredient Bridge 提供）。
- ingredient participants 直接使用 Bridge 的 entity_id（N_* / F_*）；來源 source_id 直接使用 Bridge 的 interaction_corpus_sources。
  前置條件（apply 時檢查 store）：所有 participant entity、evidence source 都已在 Bridge 表中，否則拒絕寫入。
- Neo4j 的 NutrientGroup / IngredientForm 節點由 Ingredient Bridge 建立；本 importer 只 MATCH、不建立，apply 前先 preflight。
- 證據關係為中性 HAS_EVIDENCE（方向在 supports_direction），不使用 SUPPORTED_BY。
- 不刪除：資料庫有、但本次計畫沒有的列（stale）→ 計畫錯誤，拒絕寫入。
- 寫入順序：A drug → B drug alias → C interaction → D participants → E evidence → 完成標記；Neo4j 只在標記寫入後同步。
- 語意保護（import gate，與 0006 CHECK 同義，寫入前就擋）：
  * duplicate intake 是獨立 record_kind，只有一個 SELF participant，severity 不判定、dose_evaluation_default = NOT_EVALUATED。
  * EVIDENCE_CONFLICT / NEEDS_REVIEW / SUPPORTING / CONTEXT_ONLY 不得 deterministic_conclusion_allowed。
  * 不翻譯、不 auto-merge 藥品中文名；缺 TFDA 代碼 / 官方中文名 → NEEDS_REVIEW。
  * 取得失敗的來源不得作為 evidence。
- 不建立 SAME_AS、INTERACTS_WITH、Product→Interaction、Product↔Product；不判斷風險、不計算劑量。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ingestion.ingredient_bridge_import import (
    BridgeStore,
    EmptyStore,
    StageDiff,
    _cmp,
    _null,
    _same,
    _sha,
)
from app.ingestion.ingredient_entity_mapping import (
    CORPUS_DIR,
    DUPLICATE_INTAKE,
    INGREDIENT_ENTITY_TYPE,
    _read_csv,
    _verified_sha256,
)
from app.normalization import normalize_name

MIGRATION_0006_MARKER = "migration_0006_interaction_corpus"
IMPORT_MARKER_PREFIX = "interaction_corpus_import"
NEO4J_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "migrations" / "neo4j" / "0005_interaction_graph.cypher"
WRITE_BATCH = 500

ENTITIES_FILE = "interaction_entities.csv"
ALIASES_FILE = "interaction_entity_aliases.csv"
RECORDS_FILE = "interaction_records.csv"
EVIDENCE_FILE = "interaction_evidence.csv"
SOURCES_FILE = "interaction_sources.csv"
DRUGS_FILE = "drug_substances.csv"
VALIDATION_FILE = "interaction_validation.json"
CORPUS_INPUT_FILES = (ENTITIES_FILE, ALIASES_FILE, RECORDS_FILE, EVIDENCE_FILE, SOURCES_FILE, DRUGS_FILE, VALIDATION_FILE)

DRUG_ENTITY_TYPE = "DRUG_SUBSTANCE"
PAIRWISE, DUPLICATE_RULE = "PAIRWISE_INTERACTION", "DUPLICATE_INTAKE_RULE"
# 可做 deterministic 結論的唯一條件（與 0006 generated column 同義；規則透明、可測試）
ELIGIBLE_DECISION_MODES = frozenset({"CONDITIONAL_MESSAGE_ONLY", "DUPLICATE_PRESENCE_OR_VALIDATED_ARITHMETIC_ONLY"})
FAILED_SOURCE_PREFIXES = ("FETCH_FAILED", "BLOCKED", "UNVERIFIED")

STAGES: tuple[tuple[str, str, str], ...] = (
    ("A_drug_substances", "drug_substances", "drug_substance_id"),
    ("B_drug_aliases", "drug_substance_aliases", "alias_id"),
    ("C_interactions", "interactions", "interaction_id"),
    ("D_participants", "interaction_participants", "interaction_id,role"),
    ("E_evidence", "interaction_evidence", "evidence_id"),
)
WRITABLE_TABLES = frozenset(t for _, t, _ in STAGES) | {"app_schema_meta"}
PROTECTED_TABLES = frozenset({
    "products", "ingredients", "product_ingredients", "interaction_corpus_entities", "interaction_corpus_sources",
    "interaction_corpus_entity_aliases", "interaction_form_nutrient_links", "product_ingredient_entity_mappings"})
BRIDGE_PREREQUISITES = (("interaction_corpus_entities", "entity_id"), ("interaction_corpus_sources", "source_id"))


# ----------------------------------------------------------------------------- 讀取

@dataclass
class InteractionCorpusData:
    version: str
    directory: str
    file_sha256: dict[str, str]
    entities: dict[str, dict[str, str]]           # 全部 45（INGREDIENT 30 + DRUG_SUBSTANCE 15）
    aliases: list[dict[str, str]]
    drugs: list[dict[str, str]]
    records: list[dict[str, str]]
    evidence: list[dict[str, str]]
    sources: dict[str, dict[str, str]]
    validation: dict[str, Any]
    ingredient_graph_labels: dict[str, str] = field(default_factory=dict)   # base corpus ingredient entity → graph label

    def counts(self) -> dict[str, int]:
        return {"entities": len(self.entities), "aliases": len(self.aliases), "drug_substances": len(self.drugs),
                "interactions": len(self.records), "evidence": len(self.evidence), "sources": len(self.sources),
                "duplicate_intake_rules": sum(r["interaction_type"] == DUPLICATE_INTAKE for r in self.records),
                "pairwise_interactions": sum(r["interaction_type"] != DUPLICATE_INTAKE for r in self.records)}


def load_interaction_corpus_data(directory: str | Path = CORPUS_DIR) -> InteractionCorpusData:
    """讀 corpus；SHA256SUMS 不符 → ValueError（fail closed）。"""
    directory = Path(directory)
    file_sha = _verified_sha256(directory, CORPUS_INPUT_FILES, "interaction corpus", require_sums=True)
    validation = json.loads((directory / VALIDATION_FILE).read_text(encoding="utf-8"))
    entities = {r["entity_id"]: r for r in _read_csv(directory / ENTITIES_FILE)}
    labels = {eid: ("NutrientGroup" if e["identity_level"] == "NUTRIENT_GROUP" else "IngredientForm")
              for eid, e in entities.items() if e["entity_type"] == INGREDIENT_ENTITY_TYPE}
    return InteractionCorpusData(
        version=str(validation.get("corpus_version") or "unknown"), directory=str(directory), file_sha256=file_sha,
        entities=entities, aliases=_read_csv(directory / ALIASES_FILE), drugs=_read_csv(directory / DRUGS_FILE),
        records=_read_csv(directory / RECORDS_FILE), evidence=_read_csv(directory / EVIDENCE_FILE),
        sources={r["source_id"]: r for r in _read_csv(directory / SOURCES_FILE)}, validation=validation,
        ingredient_graph_labels=labels)


# ----------------------------------------------------------------------------- 列

def _json(value: str | None) -> Any:
    value = _null(value)
    return json.loads(value) if value else None


def drug_rows(data: InteractionCorpusData) -> list[dict[str, Any]]:
    rows = []
    for d in data.drugs:
        # official_name_zh / tfda_ingredient_code 只照 corpus 原樣；缺 → NEEDS_REVIEW，不翻譯、不補
        mapped = bool(_null(d.get("tfda_ingredient_code")) and _null(d.get("official_name_zh")))
        status = d["taiwan_code_mapping_status"]
        rows.append({
            "drug_substance_id": d["drug_substance_id"], "canonical_name_en": d["canonical_name_en"],
            "official_name_zh": _null(d.get("official_name_zh")), "identity_level": d["identity_level"],
            "parent_active_moiety_id": _null(d.get("parent_active_moiety_id")),
            "tfda_ingredient_code": _null(d.get("tfda_ingredient_code")), "cas": _null(d.get("cas")),
            "unii": _null(d.get("unii")), "rxnorm_id": _null(d.get("rxnorm_id")),
            "taiwan_code_mapping_status": status if (status != "MAPPED_REVIEWED" or mapped) else "NEEDS_REVIEW",
            "source_id": d["source_id"], "source_url": _null(d.get("source_url")),
            "retrieved_at": _null(d.get("retrieved_at")), "review_status": d["review_status"],
            "corpus_version": data.version, "raw_data": {"notes": _null(d.get("notes"))},
        })
    # parent 先寫（自我 FK）
    return sorted(rows, key=lambda r: (r["parent_active_moiety_id"] is not None, r["drug_substance_id"]))


def drug_alias_rows(data: InteractionCorpusData) -> list[dict[str, Any]]:
    drug_ids = {d["drug_substance_id"] for d in data.drugs}
    return sorted(({
        "alias_id": a["alias_id"], "drug_substance_id": a["entity_id"], "alias_text": a["alias_text"],
        "alias_normalized": normalize_name(a["alias_text"]), "language": a["language"],
        "alias_relation": _null(a.get("alias_relation")), "match_policy": a["match_policy"],
        "source_id": _null(a.get("source_id")), "review_status": a["review_status"], "corpus_version": data.version,
    } for a in data.aliases if a["entity_id"] in drug_ids), key=lambda r: r["alias_id"])


def eligibility(record: dict[str, Any]) -> bool:
    """與 0006 `deterministic_conclusion_allowed` 同義。"""
    return (record["review_status"] == "READY" and record["evidence_use"] == "DECISION_ELIGIBLE"
            and record["certainty"] != "CONFLICTING" and record["decision_mode"] in ELIGIBLE_DECISION_MODES)


def interaction_rows(data: InteractionCorpusData) -> list[dict[str, Any]]:
    rows = []
    for r in data.records:
        dup = r["interaction_type"] == DUPLICATE_INTAKE
        rows.append({
            "interaction_id": r["interaction_id"], "record_kind": DUPLICATE_RULE if dup else PAIRWISE,
            "interaction_type": r["interaction_type"], "directionality": r["directionality"],
            "clinical_effect": _null(r.get("clinical_effect")), "severity": r["severity"],
            "severity_basis": _null(r.get("severity_basis")), "certainty": r["certainty"],
            "evidence_level": r["evidence_level"], "evidence_use": r["evidence_use"], "decision_mode": r["decision_mode"],
            "review_status": r["review_status"], "mechanism_summary": _null(r.get("mechanism_summary")),
            "recommendation": _null(r.get("recommendation")), "timing_advice": _null(r.get("timing_advice")),
            "dose_context": _null(r.get("dose_context")), "population_context": _null(r.get("population_context")),
            "applicability_conditions": r["applicability_conditions"],
            "dose_evaluation_default": "NOT_EVALUATED" if dup else "NOT_APPLICABLE",
            "corpus_version": data.version, "retrieved_at": _null(r.get("retrieved_at")),
            "raw_data": {"version": r.get("version")},
        })
    return sorted(rows, key=lambda r: r["interaction_id"])


def _participant(data: InteractionCorpusData, interaction_id: str, role: str, entity_id: str) -> dict[str, Any]:
    drug = data.entities[entity_id]["entity_type"] == DRUG_ENTITY_TYPE
    return {
        "interaction_id": interaction_id, "role": role,
        "participant_kind": "DrugSubstance" if drug else data.ingredient_graph_labels[entity_id],
        "ingredient_entity_id": None if drug else entity_id, "drug_substance_id": entity_id if drug else None,
        "corpus_version": data.version,
    }


def participant_rows(data: InteractionCorpusData) -> list[dict[str, Any]]:
    """A_AFFECTS_B → A=AFFECTING / B=AFFECTED；B_AFFECTS_A 相反；SELF_ACCUMULATION → 單一 SELF（A == B 收斂為一個）。"""
    rows = []
    for r in data.records:
        iid, a, b = r["interaction_id"], r["entity_a_id"], r["entity_b_id"]
        if r["directionality"] == "SELF_ACCUMULATION":
            rows.append(_participant(data, iid, "SELF", a))
        elif r["directionality"] == "A_AFFECTS_B":
            rows += [_participant(data, iid, "AFFECTING", a), _participant(data, iid, "AFFECTED", b)]
        elif r["directionality"] == "B_AFFECTS_A":
            rows += [_participant(data, iid, "AFFECTING", b), _participant(data, iid, "AFFECTED", a)]
        else:
            raise ValueError(f"未知 directionality：{iid} {r['directionality']}")
    return sorted(rows, key=lambda r: (r["interaction_id"], r["role"]))


def evidence_rows(data: InteractionCorpusData) -> list[dict[str, Any]]:
    rows = []
    for e in data.evidence:
        src = data.sources.get(e["source_id"], {})
        rows.append({
            "evidence_id": e["evidence_id"], "interaction_id": e["interaction_id"], "source_id": e["source_id"],
            "source_type": e["source_type"], "authority": _null(e.get("authority")),
            "is_official": e["source_type"].startswith("OFFICIAL_"), "title": _null(e.get("title")),
            "study_type": _null(e.get("study_type")), "pmid": _null(e.get("pmid")), "doi": _null(e.get("doi")),
            "publication_date": _null(e.get("publication_date")), "retrieved_at": _null(e.get("retrieved_at")),
            "source_retrieval_status": src.get("retrieval_status") or "UNVERIFIED_MISSING_SOURCE",
            "population": _null(e.get("population")), "sample_size": _null(e.get("sample_size")),
            "dose_context": _null(e.get("dose_context")), "result_summary_zh": _null(e.get("result_summary_zh")),
            "limitations_zh": _null(e.get("limitations_zh")), "supports_direction": e["supports_direction"],
            "evidence_level": e["evidence_level"], "certainty": e["certainty"], "evidence_use": e["evidence_use"],
            "review_status": e["review_status"], "source_url": _null(e.get("source_url")),
            "article_or_section": _null(e.get("article_or_section")),
            "applicability_scope": _null(e.get("applicability_scope")),
            "publication_types": _json(e.get("publication_types_json")), "corpus_version": data.version,
            "raw_data": {},
        })
    return sorted(rows, key=lambda r: r["evidence_id"])


# ----------------------------------------------------------------------------- 安全檢查

def semantic_errors(data: InteractionCorpusData, stages: dict[str, list[dict[str, Any]]]) -> list[str]:
    """與 0006 CHECK / trigger 同義的 import gate。"""
    errors: list[str] = []
    drug_ids = {r["drug_substance_id"] for r in stages["A_drug_substances"]}
    interactions = {r["interaction_id"]: r for r in stages["C_interactions"]}
    parts: dict[str, list[dict[str, Any]]] = {}
    for p in stages["D_participants"]:
        parts.setdefault(p["interaction_id"], []).append(p)
    evidence: dict[str, list[dict[str, Any]]] = {}
    for e in stages["E_evidence"]:
        evidence.setdefault(e["interaction_id"], []).append(e)

    for d in stages["A_drug_substances"]:
        if d["taiwan_code_mapping_status"] == "MAPPED_REVIEWED" and not (d["tfda_ingredient_code"] and d["official_name_zh"]):
            errors.append(f"DRUG_MAPPED_WITHOUT_OFFICIAL_IDENTITY:{d['drug_substance_id']}")
        if d["parent_active_moiety_id"] and d["parent_active_moiety_id"] not in drug_ids:
            errors.append(f"DRUG_UNKNOWN_PARENT:{d['drug_substance_id']}")
    for a in stages["B_drug_aliases"]:
        if a["language"].startswith("zh") and a["match_policy"] != "DISPLAY_ONLY_NOT_AUTOMERGE":
            errors.append(f"DRUG_ZH_ALIAS_NOT_DISPLAY_ONLY:{a['alias_id']}")
    for iid, i in interactions.items():
        ps = parts.get(iid, [])
        roles = sorted(p["role"] for p in ps)
        if i["record_kind"] == DUPLICATE_RULE:
            if roles != ["SELF"] or any(p["participant_kind"] == "DrugSubstance" for p in ps):
                errors.append(f"DUPLICATE_BAD_PARTICIPANTS:{iid}")
            if i["severity"] != "DOSE_DEPENDENT_NOT_ASSIGNED" or i["directionality"] != "SELF_ACCUMULATION":
                errors.append(f"DUPLICATE_HAS_SEVERITY_SEMANTICS:{iid}")
        else:
            if roles != ["AFFECTED", "AFFECTING"]:
                errors.append(f"PAIRWISE_BAD_PARTICIPANTS:{iid}")
            elif len({(p["ingredient_entity_id"], p["drug_substance_id"]) for p in ps}) < 2:
                errors.append(f"PAIRWISE_SAME_PARTICIPANT:{iid}")
        for p in ps:
            if p["drug_substance_id"] and p["drug_substance_id"] not in drug_ids:
                errors.append(f"UNKNOWN_DRUG_PARTICIPANT:{iid}:{p['drug_substance_id']}")
        if not i["applicability_conditions"]:
            errors.append(f"MISSING_APPLICABILITY:{iid}")
        ev = evidence.get(iid, [])
        if not ev:
            errors.append(f"INTERACTION_WITHOUT_EVIDENCE:{iid}")
        if eligibility(i):
            if not any(e["evidence_use"] == "DECISION_ELIGIBLE" for e in ev):
                errors.append(f"ELIGIBLE_WITHOUT_ELIGIBLE_EVIDENCE:{iid}")
        if i["certainty"] == "CONFLICTING" and (i["review_status"] != "EVIDENCE_CONFLICT" or eligibility(i)):
            errors.append(f"CONFLICT_NOT_PRESERVED:{iid}")
        if i["review_status"] == "EVIDENCE_CONFLICT" and eligibility(i):
            errors.append(f"CONFLICT_ELIGIBLE:{iid}")
    for e in stages["E_evidence"]:
        if e["interaction_id"] not in interactions:
            errors.append(f"EVIDENCE_UNKNOWN_INTERACTION:{e['evidence_id']}")
        if e["source_retrieval_status"].startswith(FAILED_SOURCE_PREFIXES):
            errors.append(f"EVIDENCE_FROM_FAILED_SOURCE:{e['evidence_id']}:{e['source_id']}")
        if e["source_id"] not in data.sources:
            errors.append(f"EVIDENCE_UNKNOWN_SOURCE:{e['evidence_id']}:{e['source_id']}")
    # interaction 的 participant 必須是 corpus entity（ingredient 與 Bridge 同一套 ID）
    for p in stages["D_participants"]:
        if p["ingredient_entity_id"] and p["ingredient_entity_id"] not in data.ingredient_graph_labels:
            errors.append(f"UNKNOWN_INGREDIENT_PARTICIPANT:{p['interaction_id']}:{p['ingredient_entity_id']}")
    return errors


def bridge_compatibility_errors(data: InteractionCorpusData, bridge_entities: dict[str, str] | None,
                                bridge_source_ids: set[str] | None) -> list[str]:
    """participants / evidence 必須能對接 Ingredient Bridge 已匯入的 entity 與 source（不重建第二套 identity）。

    bridge_entities：entity_id → graph_label（來自 Bridge 的 interaction_corpus_entities，或 load_interaction_corpus 的 entities）。
    """
    errors = []
    if bridge_entities is not None:
        for p in participant_rows(data):
            eid = p["ingredient_entity_id"]
            if eid and eid not in bridge_entities:
                errors.append(f"BRIDGE_MISSING_ENTITY:{p['interaction_id']}:{eid}")
            elif eid and bridge_entities[eid] != p["participant_kind"]:
                errors.append(f"BRIDGE_LABEL_MISMATCH:{eid}:{bridge_entities[eid]}≠{p['participant_kind']}")
    if bridge_source_ids is not None:
        for e in data.evidence:
            if e["source_id"] not in bridge_source_ids:
                errors.append(f"BRIDGE_MISSING_SOURCE:{e['evidence_id']}:{e['source_id']}")
    return errors


# ----------------------------------------------------------------------------- 計畫

@dataclass
class InteractionPlan:
    corpus_version: str
    stages: dict[str, list[dict[str, Any]]]
    diffs: dict[str, StageDiff]
    errors: list[str]
    data: InteractionCorpusData
    notes: list[str] = field(default_factory=list)

    @property
    def marker_key(self) -> str:
        return f"{IMPORT_MARKER_PREFIX}:{self.corpus_version}"

    @property
    def marker_value(self) -> str:
        return f"complete:{_sha({s: [_cmp(r) for r in rows] for s, rows in self.stages.items()})[:16]}"

    def report(self) -> dict[str, Any]:
        interactions = self.stages["C_interactions"]
        pairwise = [r for r in interactions if r["record_kind"] == PAIRWISE]
        return {
            "corpus_version": self.corpus_version, "corpus_counts": self.data.counts(),
            "stages": {s: d.counts() for s, d in self.diffs.items()},
            "would_insert": {s: len(d.insert) for s, d in self.diffs.items()},
            "would_update": sum(len(d.update) for d in self.diffs.values()),
            "would_delete": sum(d.stale for d in self.diffs.values()),
            "interaction_kinds": dict(Counter(r["record_kind"] for r in interactions)),
            "pairwise_conclusion_eligible": sum(eligibility(r) for r in pairwise),
            "pairwise_explanation_or_review_only": sum(not eligibility(r) for r in pairwise),
            "pairwise_review_status": dict(Counter(r["review_status"] for r in pairwise)),
            "pairwise_severity": dict(Counter(r["severity"] for r in pairwise)),
            "evidence_use": dict(Counter(r["evidence_use"] for r in self.stages["E_evidence"])),
            "evidence_official": dict(Counter(r["is_official"] for r in self.stages["E_evidence"])),
            "conflicts": sorted(r["interaction_id"] for r in interactions if r["review_status"] == "EVIDENCE_CONFLICT"),
            "drug_taiwan_code_mapping": dict(Counter(r["taiwan_code_mapping_status"] for r in self.stages["A_drug_substances"])),
            "neo4j_preview": graph_preview(self),
            "notes": self.notes, "errors": self.errors,
        }


def plan_interaction_import(data: InteractionCorpusData, store: BridgeStore | None = None, *,
                            bridge_entities: dict[str, str] | None = None,
                            bridge_source_ids: set[str] | None = None) -> InteractionPlan:
    """計算 A → E 各階段的列與 insert / update / unchanged / stale；任何不安全情況 → errors（apply 拒絕）。"""
    store = store or EmptyStore()
    stages = {
        "A_drug_substances": drug_rows(data), "B_drug_aliases": drug_alias_rows(data),
        "C_interactions": interaction_rows(data), "D_participants": participant_rows(data),
        "E_evidence": evidence_rows(data),
    }
    errors = semantic_errors(data, stages)
    errors += bridge_compatibility_errors(data, bridge_entities, bridge_source_ids)
    for stage, _, pk in STAGES:
        keys = Counter(tuple(r[c] for c in pk.split(",")) for r in stages[stage])
        if any(n > 1 for n in keys.values()):
            errors.append(f"collision {stage}_duplicate_ids = {sum(n - 1 for n in keys.values() if n > 1)}")
    expected = data.validation.get("counts", {})
    for key, actual in (("interactions", len(stages["C_interactions"])), ("evidence_records", len(stages["E_evidence"])),
                        ("drug_substances", len(stages["A_drug_substances"]))):
        if key in expected and expected[key] != actual:
            errors.append(f"corpus 計數與 interaction_validation.json 不符：{key} {actual} ≠ {expected[key]}")

    diffs: dict[str, StageDiff] = {}
    for stage, table, pk in STAGES:
        rows, keys = stages[stage], pk.split(",")
        cols = list(rows[0]) if rows else keys
        existing = {tuple(str(r[k]) for k in keys): r for r in store.fetch(table, cols)}
        diff, planned = StageDiff(), set()
        for row in rows:
            key = tuple(str(row[k]) for k in keys)
            planned.add(key)
            old = existing.get(key)
            if old is None:
                diff.insert.append(row)
            elif _same(row, old):
                diff.unchanged += 1
            else:
                diff.update.append(row)
        diff.stale = sum(k not in planned for k in existing)
        if diff.stale:
            errors.append(f"{table}：資料庫有 {diff.stale} 列不在本次計畫（importer 不刪除；版本切換需另行處理）")
        diffs[stage] = diff
    return InteractionPlan(data.version, stages, diffs, errors, data)


def bridge_prerequisites(store: BridgeStore, data: InteractionCorpusData) -> tuple[dict[str, str], set[str]]:
    """從 Supabase 讀 Ingredient Bridge 已匯入的 entity（含 graph_label）與 source。"""
    entities = {r["entity_id"]: r["graph_label"] for r in store.fetch("interaction_corpus_entities", ["entity_id", "graph_label"])}
    sources = {r["source_id"] for r in store.fetch("interaction_corpus_sources", ["source_id"])}
    return entities, sources


# ----------------------------------------------------------------------------- Neo4j preflight

BRIDGE_LABELS = ("NutrientGroup", "IngredientForm")
_PREFLIGHT_NODES = ("MATCH (n) WHERE n.entity_id IN $ids AND (n:NutrientGroup OR n:IngredientForm) "
                    "RETURN n.entity_id AS entity_id, labels(n) AS labels")
_PREFLIGHT_COUNTS = tuple((label, f"MATCH (n:{label}) RETURN count(n) AS n") for label in BRIDGE_LABELS)


def required_ingredient_participants(data: InteractionCorpusData) -> dict[str, str]:
    """corpus 的 30 個 ingredient entity（實際被 interaction 使用的 20 個是其子集）：entity_id → 預期 graph label。

    全部檢查：未使用的 form 也是之後 interaction 可直接重用的 Bridge identity，缺漏同樣視為 Bridge 不完整。
    """
    return dict(data.ingredient_graph_labels)


def neo4j_bridge_preflight(neo4j, required: dict[str, str]) -> dict[str, Any]:
    """唯讀：Ingredient Bridge 的 Neo4j 節點必須已存在且 label 正確；缺 / 不符 / 重複 → errors（STOP，不自動建立節點）。"""
    rows = neo4j.read(_PREFLIGHT_NODES, {"ids": sorted(required)})
    seen: dict[str, list[list[str]]] = {}
    for r in rows:
        seen.setdefault(r["entity_id"], []).append(sorted(r["labels"]))
    missing = sorted(set(required) - set(seen))
    duplicates = sorted(i for i, found in seen.items() if len(found) > 1)
    mismatched = sorted(i for i, found in seen.items() if i in required and len(found) == 1
                        and [l for l in found[0] if l in BRIDGE_LABELS] != [required[i]])
    counts = {label: (neo4j.read(q) or [{"n": 0}])[0]["n"] for label, q in _PREFLIGHT_COUNTS}
    errors = ([f"NEO4J_MISSING_PARTICIPANT:{i}" for i in missing] + [f"NEO4J_LABEL_MISMATCH:{i}:{required[i]}" for i in mismatched]
              + [f"NEO4J_DUPLICATE_IDENTITY:{i}" for i in duplicates])
    return {"required": len(required), "found": len(required) - len(missing), "missing": missing,
            "label_mismatches": mismatched, "duplicates": duplicates, "node_counts": counts, "errors": errors,
            "reusable": not errors}


# ----------------------------------------------------------------------------- Neo4j

_INTERACTION_SET = """
        SET n.record_kind = r.record_kind, n.interaction_type = r.interaction_type, n.directionality = r.directionality,
            n.severity = r.severity, n.certainty = r.certainty, n.evidence_level = r.evidence_level,
            n.evidence_use = r.evidence_use, n.decision_mode = r.decision_mode, n.review_status = r.review_status,
            n.deterministic_conclusion_allowed = r.deterministic_conclusion_allowed,
            n.applicability_conditions = r.applicability_conditions, n.dose_evaluation_default = r.dose_evaluation_default,
            n.corpus_version = r.corpus_version"""
_PARTICIPATES = """
        UNWIND $rows AS r
        MATCH (p:{label} {{entity_id: r.entity_id}}), (i:{target} {{interaction_id: r.interaction_id}})
        MERGE (p)-[rel:PARTICIPATES_IN {{role: r.role}}]->(i)
        SET rel.corpus_version = r.corpus_version"""
_HAS_EVIDENCE = """
        UNWIND $rows AS r
        MATCH (i:{label} {{interaction_id: r.interaction_id}}), (e:InteractionEvidence {{evidence_id: r.evidence_id}})
        MERGE (i)-[rel:HAS_EVIDENCE]->(e)
        SET rel.supports_direction = r.supports_direction, rel.evidence_use = r.evidence_use"""
NEO4J_QUERIES = {
    "drug_substances": """UNWIND $rows AS r MERGE (n:DrugSubstance {entity_id: r.drug_substance_id})
        SET n.canonical_name_en = r.canonical_name_en, n.official_name_zh = r.official_name_zh,
            n.identity_level = r.identity_level, n.taiwan_code_mapping_status = r.taiwan_code_mapping_status,
            n.corpus_version = r.corpus_version""",
    "interactions": "UNWIND $rows AS r MERGE (n:Interaction {interaction_id: r.interaction_id})" + _INTERACTION_SET,
    "duplicate_intake_rules": "UNWIND $rows AS r MERGE (n:DuplicateIntakeRule {interaction_id: r.interaction_id})"
                              + _INTERACTION_SET,
    "sources": """UNWIND $rows AS r MERGE (n:InteractionSource {source_id: r.source_id})
        SET n.title = r.title, n.authority = r.authority, n.source_tier = r.source_tier, n.source_type = r.source_type,
            n.source_url = r.source_url, n.is_official = r.is_official""",
    "evidence": """UNWIND $rows AS r MERGE (n:InteractionEvidence {evidence_id: r.evidence_id})
        SET n.source_type = r.source_type, n.study_type = r.study_type, n.evidence_level = r.evidence_level,
            n.supports_direction = r.supports_direction, n.evidence_use = r.evidence_use, n.is_official = r.is_official,
            n.pmid = r.pmid, n.source_url = r.source_url, n.corpus_version = r.corpus_version""",
    "participates_nutrient_group": _PARTICIPATES.format(label="NutrientGroup", target="Interaction"),
    "participates_ingredient_form": _PARTICIPATES.format(label="IngredientForm", target="Interaction"),
    "participates_drug_substance": _PARTICIPATES.format(label="DrugSubstance", target="Interaction"),
    "duplicate_self_nutrient_group": _PARTICIPATES.format(label="NutrientGroup", target="DuplicateIntakeRule"),
    "duplicate_self_ingredient_form": _PARTICIPATES.format(label="IngredientForm", target="DuplicateIntakeRule"),
    "has_evidence_interaction": _HAS_EVIDENCE.format(label="Interaction"),
    "has_evidence_duplicate_rule": _HAS_EVIDENCE.format(label="DuplicateIntakeRule"),
    "from_source": """UNWIND $rows AS r
        MATCH (e:InteractionEvidence {evidence_id: r.evidence_id}), (s:InteractionSource {source_id: r.source_id})
        MERGE (e)-[:FROM_SOURCE]->(s)""",
}
FORBIDDEN_GRAPH_TOKENS = ("SAME_AS", "INTERACTS_WITH", ":Product", "DELETE", "DETACH", "REMOVE")


def graph_payloads(plan: InteractionPlan) -> dict[str, list[dict[str, Any]]]:
    interactions = [{**r, "deterministic_conclusion_allowed": eligibility(r)} for r in plan.stages["C_interactions"]]
    kind = {r["interaction_id"]: r["record_kind"] for r in interactions}
    parts = [{"entity_id": p["ingredient_entity_id"] or p["drug_substance_id"], "interaction_id": p["interaction_id"],
              "role": p["role"], "kind": p["participant_kind"], "corpus_version": p["corpus_version"]}
             for p in plan.stages["D_participants"]]
    src_ids = sorted({e["source_id"] for e in plan.stages["E_evidence"]})
    sources = [{"source_id": s, "title": plan.data.sources[s].get("title"), "authority": plan.data.sources[s].get("authority"),
                "source_tier": plan.data.sources[s].get("source_tier"), "source_type": plan.data.sources[s]["source_type"],
                "source_url": _null(plan.data.sources[s].get("source_url")),
                "is_official": plan.data.sources[s]["source_type"].startswith("OFFICIAL_")} for s in src_ids]
    pw = lambda p: kind[p["interaction_id"]] == PAIRWISE  # noqa: E731
    ev_pw = [e for e in plan.stages["E_evidence"] if kind[e["interaction_id"]] == PAIRWISE]
    ev_dup = [e for e in plan.stages["E_evidence"] if kind[e["interaction_id"]] == DUPLICATE_RULE]
    return {
        "drug_substances": plan.stages["A_drug_substances"],
        "interactions": [r for r in interactions if r["record_kind"] == PAIRWISE],
        "duplicate_intake_rules": [r for r in interactions if r["record_kind"] == DUPLICATE_RULE],
        "sources": sources, "evidence": plan.stages["E_evidence"],
        "participates_nutrient_group": [p for p in parts if pw(p) and p["kind"] == "NutrientGroup"],
        "participates_ingredient_form": [p for p in parts if pw(p) and p["kind"] == "IngredientForm"],
        "participates_drug_substance": [p for p in parts if pw(p) and p["kind"] == "DrugSubstance"],
        "duplicate_self_nutrient_group": [p for p in parts if not pw(p) and p["kind"] == "NutrientGroup"],
        "duplicate_self_ingredient_form": [p for p in parts if not pw(p) and p["kind"] == "IngredientForm"],
        "has_evidence_interaction": ev_pw, "has_evidence_duplicate_rule": ev_dup,
        "from_source": plan.stages["E_evidence"],
    }


def graph_preview(plan: InteractionPlan) -> dict[str, int]:
    return {k: len(v) for k, v in graph_payloads(plan).items()}


def write_interaction_graph(neo4j, plan: InteractionPlan, batch_size: int = 500) -> dict[str, int]:
    """Neo4j：只有 MERGE / SET；不刪除、不建立 SAME_AS / INTERACTS_WITH / Product 關係。"""
    from app.services.neo4j_service import load_schema_statements

    for query in NEO4J_QUERIES.values():
        if any(tok in query for tok in FORBIDDEN_GRAPH_TOKENS):
            raise RuntimeError("interaction graph query 含禁止的關係 / 操作")
    for stmt in load_schema_statements(NEO4J_SCHEMA_FILE):
        neo4j.write(stmt)
    payloads = graph_payloads(plan)
    for stage, query in NEO4J_QUERIES.items():       # dict 順序：節點 → 關係
        rows = payloads[stage]
        for i in range(0, len(rows), batch_size):
            neo4j.write(query, {"rows": rows[i : i + batch_size]})
    return {k: len(v) for k, v in payloads.items()}


_VERIFY = (
    ("participants", "MATCH (:NutrientGroup|IngredientForm|DrugSubstance)-[r:PARTICIPATES_IN]->(:Interaction|DuplicateIntakeRule) "
                     "RETURN count(r) AS n"),
    ("has_evidence", "MATCH (:Interaction|DuplicateIntakeRule)-[r:HAS_EVIDENCE]->(:InteractionEvidence) RETURN count(r) AS n"),
    ("from_source", "MATCH (:InteractionEvidence)-[r:FROM_SOURCE]->(:InteractionSource) RETURN count(r) AS n"),
)


def verify_graph(neo4j, plan: InteractionPlan) -> list[str]:
    """MATCH 找不到 Bridge 節點時 MERGE 會靜默略過；寫入後比對關係數量，少了 → 回報（不是靜默成功）。"""
    expected = {"participants": len(plan.stages["D_participants"]), "has_evidence": len(plan.stages["E_evidence"]),
                "from_source": len(plan.stages["E_evidence"])}
    problems = []
    for key, query in _VERIFY:
        rows = neo4j.read(query)
        actual = rows[0]["n"] if rows else 0
        if actual != expected[key]:
            problems.append(f"GRAPH_COUNT_MISMATCH:{key}:{actual}≠{expected[key]}")
    return problems


# ----------------------------------------------------------------------------- 寫入

def _batched(store: BridgeStore, table: str, rows: list[dict[str, Any]], on_conflict: str) -> int:
    if table in PROTECTED_TABLES or table not in WRITABLE_TABLES:
        raise RuntimeError(f"interaction corpus importer 不允許寫入 {table}")
    for i in range(0, len(rows), WRITE_BATCH):
        store.upsert(table, rows[i : i + WRITE_BATCH], on_conflict)
    return len(rows)


def import_marker_present(store: BridgeStore, plan: InteractionPlan) -> bool:
    return any(r["key"] == plan.marker_key and r["value"] == plan.marker_value
               for r in store.fetch("app_schema_meta", ["key", "value"]))


def apply_interaction_import(plan: InteractionPlan, store: BridgeStore, *, neo4j=None) -> dict[str, Any]:
    """依 A → E 寫入；計畫有錯誤、或 Ingredient Bridge 前置資料不在 → 拒絕。完成標記最後寫；Neo4j 只在標記寫入後同步。"""
    if plan.errors:
        raise RuntimeError(f"interaction 匯入計畫有 {len(plan.errors)} 個錯誤，拒絕寫入：{plan.errors[:3]}")
    entities, sources = bridge_prerequisites(store, plan.data)
    missing = bridge_compatibility_errors(plan.data, entities, sources)
    if missing:
        raise RuntimeError(f"Ingredient Bridge 前置資料不完整（先套用 / 匯入 0005 Bridge）：{missing[:3]}")
    preflight = None
    if neo4j is not None:     # 任何寫入之前：Bridge 節點缺 / label 不符 → STOP（不由本 importer 建立 ingredient 節點）
        preflight = neo4j_bridge_preflight(neo4j, required_ingredient_participants(plan.data))
        if preflight["errors"]:
            raise RuntimeError(f"Neo4j Ingredient Bridge preflight 失敗，停止（未寫入任何資料）：{preflight['errors'][:3]}")
    written = {}
    for stage, table, pk in STAGES:
        d = plan.diffs[stage]
        written[stage] = _batched(store, table, d.insert + d.update, pk)
    _batched(store, "app_schema_meta", [{"key": plan.marker_key, "value": plan.marker_value}], "key")
    report = plan.report()
    report["written"] = written
    report["neo4j_preflight"] = preflight
    if neo4j is not None:
        if not import_marker_present(store, plan):
            raise RuntimeError("interaction 完成標記不存在，不同步 Neo4j")
        report["graph_written"] = write_interaction_graph(neo4j, plan)
        problems = verify_graph(neo4j, plan)
        report["graph_problems"] = problems
        report["errors"] = report["errors"] + problems
    return report
