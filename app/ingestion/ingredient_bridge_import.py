"""Ingredient Bridge importer：corpus / overlay entity＋Product 成分 entity mapping → 0005 表（＋Neo4j）。

additive identity layer：
- 只寫 0005 的五張表與 `app_schema_meta` 完成標記；**不寫** products / ingredients / product_ingredients
  （既有 565 health_food Product、770 ProductIngredient、Ingredient ID 原樣保留）。
- 不刪除：資料庫已有、但本次計畫沒有的列（stale）→ 計畫錯誤，拒絕寫入（版本切換需另行處理）。
- 寫入順序：A entity → B source＋alias → C form→nutrient → D 正式 mapping → E 候選 mapping → 完成標記；
  Neo4j 只在完成標記寫入後同步。任一階段失敗：已寫入的列都已通過 DB CHECK（候選不可能是正式），
  沒有完成標記，Neo4j 不同步；重跑以相同 ID upsert 補齊（idempotent）。
- mapping 只存正式（VERIFIED_EXACT / SUPPORTED_ALIAS / FORM_TO_GROUP）與候選（AMBIGUOUS / NEEDS_REVIEW）；
  NO_MATCH 不存（可由 replay 重算）。
- 不建立 SAME_AS、INTERACTS_WITH、Product→Interaction、Product↔Product；不判斷風險、不計算累積劑量。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.ingestion.ingredient_entity_mapping import (
    BASE_ORIGIN,
    CANDIDATE_STATUSES,
    CONTRIBUTES_TO_NUTRIENT,
    FORMAL_STATUSES,
    INGREDIENT_FORM_LABEL,
    NUTRIENT_GROUP_LABEL,
    OVERLAY_CANDIDATE_STATUSES,
    OVERLAY_FORMAL_STATUSES,
    InteractionCorpus,
    MappingStatus,
    ProductMappingResult,
    _read_csv,
    _verified_sha256,
    graph_plan,
)

MIGRATION_0005_MARKER = "migration_0005_product_ingredient_entity_mapping"
BRIDGE_MARKER_PREFIX = "ingredient_bridge_import"
NEO4J_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "migrations" / "neo4j" / "0004_ingredient_entity_bridge.cypher"
BASE_SOURCES_FILE = "interaction_sources.csv"
WRITE_BATCH = 500

# 寫入順序（A → E）與主鍵
STAGES: tuple[tuple[str, str, str], ...] = (
    ("A_entities", "interaction_corpus_entities", "entity_id"),
    ("B_sources", "interaction_corpus_sources", "source_id"),
    ("B_aliases", "interaction_corpus_entity_aliases", "alias_id"),
    ("C_form_to_nutrient", "interaction_form_nutrient_links", "relationship_id"),
    ("D_formal_mappings", "product_ingredient_entity_mappings", "id"),
    ("E_candidate_mappings", "product_ingredient_entity_mappings", "id"),
)
WRITABLE_TABLES = frozenset(t for _, t, _ in STAGES) | {"app_schema_meta"}
PROTECTED_TABLES = frozenset({"products", "ingredients", "product_ingredients"})
STORED_STATUSES = FORMAL_STATUSES | CANDIDATE_STATUSES


class BridgeStore(Protocol):
    def fetch(self, table: str, columns: list[str], eq: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None: ...


class EmptyStore:
    """dry-run 預設：視為尚未匯入；任何寫入都拒絕。"""

    def fetch(self, table, columns, eq=None):
        return []

    def upsert(self, table, rows, on_conflict):
        raise RuntimeError("dry-run store 不允許寫入")


# ----------------------------------------------------------------------------- 列

def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def source_rows(corpus: InteractionCorpus) -> tuple[list[dict[str, Any]], list[str]]:
    """base corpus interaction_sources.csv（驗 SHA256SUMS）＋overlay reviewed_alias_sources.csv。同 ID 只留 base，回報碰撞。"""
    directory = Path(corpus.directory)
    rows: dict[str, dict[str, Any]] = {}
    collisions: list[str] = []
    if (directory / BASE_SOURCES_FILE).is_file():
        _verified_sha256(directory, (BASE_SOURCES_FILE,), "interaction corpus")
        for r in _read_csv(directory / BASE_SOURCES_FILE):
            rows[r["source_id"]] = {
                "source_id": r["source_id"], "authority": r.get("authority"), "source_tier": r.get("source_tier"),
                "title": r.get("title"), "source_url": _null(r.get("source_url")), "source_type": r.get("source_type"),
                "retrieved_at": _null(r.get("retrieved_at")), "last_updated_date": _null(r.get("last_updated")),
                "sha256": _null(r.get("snapshot_sha256")), "corpus_version": corpus.version, "overlay_version": None,
                "raw_data": {"origin": BASE_ORIGIN},
            }
    for r in (corpus.overlay.sources.values() if corpus.overlay else ()):
        if r["source_id"] in rows:
            collisions.append(r["source_id"])
            continue
        rows[r["source_id"]] = {
            "source_id": r["source_id"], "authority": r.get("authority"), "source_tier": r.get("source_tier"),
            "title": r.get("title"), "source_url": _null(r.get("source_url")), "source_type": r.get("source_type"),
            "retrieved_at": _null(r.get("retrieved_at")), "last_updated_date": _null(r.get("last_updated_date")),
            "sha256": _null(r.get("sha256")), "corpus_version": corpus.version, "overlay_version": corpus.overlay_version,
            "raw_data": {"origin": "REVIEWED_OVERLAY"},
        }
    return sorted(rows.values(), key=lambda r: r["source_id"]), collisions


def _null(value: Any) -> Any:
    if isinstance(value, str) and value.strip() in ("", "NULL"):
        return None
    return value


def entity_rows(corpus: InteractionCorpus) -> list[dict[str, Any]]:
    # 營養素群先寫（form 的 parent FK）
    ordered = sorted(corpus.entities.values(), key=lambda e: (e.parent_nutrient_entity_id is not None, e.entity_id))
    return [{
        "entity_id": e.entity_id, "entity_type": e.entity_type, "graph_label": e.graph_label,
        "canonical_name_en": e.canonical_name_en, "display_name_zh": e.display_name_zh,
        "identity_level": e.identity_level, "overlay_identity_level": e.overlay_identity_level,
        "parent_nutrient_entity_id": e.parent_nutrient_entity_id, "entity_origin": e.origin,
        "source_id": e.source_id, "source_url": e.source_url, "review_status": e.review_status,
        "corpus_version": corpus.version,
        "overlay_version": corpus.overlay_version if (e.origin != BASE_ORIGIN or e.overlay_identity_level) else None,
        "raw_data": {},
    } for e in ordered]


def alias_rows(corpus: InteractionCorpus) -> list[dict[str, Any]]:
    rows = [{
        "alias_id": a.alias_id, "entity_id": a.entity_id, "candidate_entity_id": None, "alias_text": a.alias_text,
        "alias_normalized": a.normalized, "language": a.language, "alias_origin": BASE_ORIGIN,
        "alias_relation": a.alias_relation, "alias_status": None, "identity_level": None, "match_policy": a.match_policy,
        "mapping_context": None, "precedence": None, "source_id": a.source_id, "source_url": None, "source_locator": None,
        "retrieved_at": None, "review_status": a.review_status, "corpus_version": corpus.version, "overlay_version": None,
    } for a in corpus.aliases]
    for a in (corpus.overlay.aliases if corpus.overlay else ()):
        rows.append({
            "alias_id": a.alias_id, "entity_id": a.entity_id, "candidate_entity_id": a.candidate_entity_id,
            "alias_text": a.alias_text, "alias_normalized": a.key, "language": a.language,
            "alias_origin": "REVIEWED_OVERLAY", "alias_relation": None, "alias_status": a.alias_status,
            "identity_level": a.identity_level, "match_policy": a.match_policy, "mapping_context": a.mapping_context,
            "precedence": a.precedence, "source_id": a.source_id, "source_url": a.source_url,
            "source_locator": a.source_locator, "retrieved_at": a.retrieved_at,
            "review_status": "READY" if a.is_formal else a.alias_status, "corpus_version": corpus.version,
            "overlay_version": a.overlay_version,
        })
    return sorted(rows, key=lambda r: r["alias_id"])


def link_rows(corpus: InteractionCorpus) -> list[dict[str, Any]]:
    rows = []
    for c in graph_plan(corpus, [])["contributes_to_nutrient"]:
        link = corpus.form_link(c["form_entity_id"])
        rows.append({
            "relationship_id": c["relationship_id"], "form_entity_id": c["form_entity_id"],
            "nutrient_entity_id": c["nutrient_entity_id"], "relationship_type": c["relationship_type"],
            "basis": c["basis"], "source_id": c["source_id"], "source_url": c["source_url"],
            "source_locator": c["source_locator"], "evidence_text": None, "review_status": c["review_status"],
            "retrieved_at": link.retrieved_at if link else None,
            "amount_conversion_allowed": False, "automatic_interaction_inheritance": False,
            "corpus_version": c["corpus_version"], "overlay_version": c["overlay_version"] if link else None,
        })
    return rows


def mapping_rows(results: Iterable[ProductMappingResult]) -> list[dict[str, Any]]:
    rows = []
    for r in results:
        for m in r.mappings:
            if m.mapping_status not in STORED_STATUSES:
                continue
            rows.append({
                "id": m.mapping_id, "product_id": m.product_id, "ingredient_source": m.ingredient_source.value,
                "source_ingredient_id": m.source_ingredient_id, "token_index": m.token_index, "raw_text": m.raw_text,
                "mapping_status": m.mapping_status.value, "entity_id": m.entity_id, "entity_type": m.entity_type,
                "via_entity_id": m.via_entity_id, "relationship_id": m.relationship_id,
                "candidate_entity_ids": list(m.candidate_entity_ids), "match_method": m.match_method.value,
                "matched_alias_ids": list(m.matched_alias_ids), "alias_evidence_status": m.alias_evidence_status,
                "overlay_precedence": m.overlay_precedence, "confidence": round(m.confidence, 3),
                "review_status": m.review_status.value, "source_parse_status": r.source.source_parse_status,
                "amount_raw": m.amount_raw, "amount_normalized": None, "unit": m.unit,
                "amount_basis": m.amount_basis.value, "source_id": m.source_id,
                "source_record_key": m.source_record_key, "source_url": m.source_url, "retrieved_at": m.retrieved_at,
                "corpus_version": m.corpus_version, "overlay_version": m.overlay_version,
            })
    return rows


# ----------------------------------------------------------------------------- 安全檢查

def invalid_mapping_reasons(row: dict[str, Any], entities: dict[str, dict[str, Any]],
                            relationships: dict[str, dict[str, Any]]) -> list[str]:
    """與 0005 CHECK 同義的 import gate（寫入前就擋，不靠 DB 才發現）。"""
    out = []
    status, formal = row["mapping_status"], row["mapping_status"] in {s.value for s in FORMAL_STATUSES}
    if formal:
        if not row["entity_id"] or row["entity_id"] not in entities:
            out.append("FORMAL_WITHOUT_KNOWN_ENTITY")
        if row["alias_evidence_status"] in OVERLAY_CANDIDATE_STATUSES:
            out.append("CANDIDATE_ALIAS_AS_FORMAL")
        if row["review_status"] not in ("AUTO_ACCEPTED_RULE", "MANUAL_REVIEWED", "SOURCE_REVIEWED"):
            out.append("FORMAL_PENDING_REVIEW")
        if row["candidate_entity_ids"]:
            out.append("FORMAL_WITH_CANDIDATES")
        if row["entity_id"] in entities and row["entity_type"] != entities[row["entity_id"]]["graph_label"]:
            out.append("ENTITY_TYPE_MISMATCH")
    else:
        if row["entity_id"] or row["entity_type"]:
            out.append("CANDIDATE_WITH_FORMAL_ENTITY")
        if row["review_status"] != "PENDING_REVIEW":
            out.append("CANDIDATE_NOT_PENDING")
        if row["alias_evidence_status"] in OVERLAY_FORMAL_STATUSES:
            out.append("FORMAL_ALIAS_AS_CANDIDATE")
        if not row["candidate_entity_ids"] and not row["matched_alias_ids"]:
            out.append("CANDIDATE_WITHOUT_EVIDENCE")
        if any(c not in entities for c in row["candidate_entity_ids"]):
            out.append("CANDIDATE_UNKNOWN_ENTITY")
    if status == MappingStatus.FORM_TO_GROUP.value:
        link = relationships.get(row["relationship_id"] or "")
        if row["entity_type"] != NUTRIENT_GROUP_LABEL or not row["via_entity_id"]:
            out.append("FORM_TO_GROUP_TARGET_NOT_GROUP")
        if (link is None or link["relationship_type"] != CONTRIBUTES_TO_NUTRIENT
                or link["form_entity_id"] != row["via_entity_id"] or link["nutrient_entity_id"] != row["entity_id"]):
            out.append("FORM_TO_GROUP_WITHOUT_RELATIONSHIP")
    elif row["via_entity_id"] or row["relationship_id"]:
        out.append("RELATIONSHIP_ON_DIRECT_MAPPING")
    if row["amount_normalized"] is not None:
        out.append("AMOUNT_NORMALIZED_WITHOUT_BASIS")
    if not row["raw_text"]:
        out.append("MISSING_RAW_TEXT")
    return out


# ----------------------------------------------------------------------------- 計畫

def _cmp(value: Any) -> Any:
    """比對用正規化：Decimal / float → 3 位小數、UUID → str、timestamp → UTC ISO（DB 與計畫格式不同但值相同）。"""
    from datetime import UTC, datetime

    if isinstance(value, (Decimal, float)):
        return round(float(value), 3)
    if isinstance(value, (list, tuple)):
        return [_cmp(v) for v in value]
    if isinstance(value, str) and len(value) >= 19 and value[4:5] == "-" and value[10:11] in ("T", " "):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, datetime):
        return (value.astimezone(UTC) if value.tzinfo else value).isoformat()
    if value is not None and not isinstance(value, (str, int, bool, dict)):
        return str(value)
    return value


def _same(new: dict[str, Any], old: dict[str, Any]) -> bool:
    return all(_cmp(new[c]) == _cmp(old.get(c)) for c in new)


@dataclass
class StageDiff:
    insert: list[dict[str, Any]] = field(default_factory=list)
    update: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0
    stale: int = 0                       # 資料庫有、計畫沒有（importer 不刪除 → 計畫錯誤）

    def counts(self) -> dict[str, int]:
        return {"insert": len(self.insert), "update": len(self.update), "unchanged": self.unchanged,
                "stale_would_delete": self.stale}


@dataclass
class BridgePlan:
    corpus_version: str
    overlay_version: str | None
    stages: dict[str, list[dict[str, Any]]]
    diffs: dict[str, StageDiff]
    errors: list[str]
    collisions: dict[str, int]
    invalid_formal: Counter
    invalid_candidate: Counter
    results: list[ProductMappingResult]
    corpus: InteractionCorpus
    notes: list[str] = field(default_factory=list)

    @property
    def marker_key(self) -> str:
        return f"{BRIDGE_MARKER_PREFIX}:{self.corpus_version}:{self.overlay_version or 'none'}"

    @property
    def marker_value(self) -> str:
        ids = sorted(r["id"] for s in ("D_formal_mappings", "E_candidate_mappings") for r in self.stages[s])
        return f"complete:{_sha(ids)[:16]}"

    def graph_preview(self) -> dict[str, Any]:
        try:
            plan = graph_plan(self.corpus, self.results)
        except KeyError as exc:  # 計畫含未知 entity（已列為 error）→ 不產生 preview
            return {"error": f"UNKNOWN_ENTITY:{exc.args[0]}"}
        return {
            "nodes": plan["nodes"], "relationships": plan["relationship_counts"],
            "interaction_nodes": plan["interaction_nodes"],
            "contains_by_target_label": dict(Counter(c["target_label"] for c in plan["contains"])),
        }

    def report(self) -> dict[str, Any]:
        by_cat: dict[str, Counter] = defaultdict(Counter)
        for r in self.results:
            cat = by_cat[r.source.product_category]
            cat["products_scanned"] += 1
            cat["products_with_formal_mapping"] += bool(r.formal())
            for m in r.mappings:
                if m.mapping_status in FORMAL_STATUSES:
                    cat["formal_mapping_units"] += 1
                elif m.mapping_status in CANDIDATE_STATUSES:
                    cat["candidate_mapping_units"] += 1
        stage_counts = {s: d.counts() for s, d in self.diffs.items()}
        total = lambda key: sum(c[key] for c in stage_counts.values())  # noqa: E731
        return {
            "corpus_version": self.corpus_version, "overlay_version": self.overlay_version,
            "stages": stage_counts,
            "would_insert": {s: c["insert"] for s, c in stage_counts.items()},
            "would_update": total("update"), "would_delete": total("stale_would_delete"),
            "collisions": self.collisions,
            "invalid_formal_mappings": dict(self.invalid_formal), "invalid_candidate_mappings": dict(self.invalid_candidate),
            "by_category": {c: dict(v) for c, v in sorted(by_cat.items())},
            "mapping_status_units": dict(Counter(r["mapping_status"] for s in ("D_formal_mappings", "E_candidate_mappings")
                                                 for r in self.stages[s])),
            "neo4j_preview": self.graph_preview(),
            "marker": {self.marker_key: self.marker_value},
            "protected_tables_written": 0,
            "errors": self.errors, "notes": self.notes,
        }


def plan_bridge_import(corpus: InteractionCorpus, results: list[ProductMappingResult], store: BridgeStore | None = None,
                       *, product_ids: set[str] | None = None, ingredient_ids: set[str] | None = None) -> BridgePlan:
    """計算 A → E 各階段的列與 insert / update / unchanged / stale；任何不安全情況 → errors（apply 拒絕）。"""
    store = store or EmptyStore()
    errors: list[str] = []
    sources, source_collisions = source_rows(corpus)
    entities = entity_rows(corpus)
    links = link_rows(corpus)
    mappings = mapping_rows(results)
    stages = {
        "A_entities": entities, "B_sources": sources, "B_aliases": alias_rows(corpus), "C_form_to_nutrient": links,
        "D_formal_mappings": [m for m in mappings if m["mapping_status"] in {s.value for s in FORMAL_STATUSES}],
        "E_candidate_mappings": [m for m in mappings if m["mapping_status"] in {s.value for s in CANDIDATE_STATUSES}],
    }

    # 計畫內的 ID / 自然鍵碰撞
    collisions = {"source_id_base_vs_overlay": len(source_collisions)}
    for stage, _, pk in STAGES:
        ids = Counter(r[pk] for r in stages[stage])
        collisions[f"{stage}_duplicate_ids"] = sum(n - 1 for n in ids.values() if n > 1)
    natural = Counter((m["product_id"], m["ingredient_source"], m["source_ingredient_id"], m["token_index"], m["raw_text"],
                       m["mapping_status"], m["entity_id"], m["via_entity_id"]) for m in mappings)
    collisions["mapping_natural_key_duplicates"] = sum(n - 1 for n in natural.values() if n > 1)
    overlay_formal = Counter((a["alias_normalized"], a["overlay_version"]) for a in stages["B_aliases"]
                             if a["alias_origin"] == "REVIEWED_OVERLAY" and a["entity_id"])
    collisions["overlay_formal_alias_key_duplicates"] = sum(n - 1 for n in overlay_formal.values() if n > 1)
    for name, n in collisions.items():
        if n and name != "source_id_base_vs_overlay":
            errors.append(f"collision {name} = {n}")

    entity_map = {e["entity_id"]: e for e in entities}
    rel_map = {r["relationship_id"]: r for r in links}
    invalid_formal, invalid_candidate = Counter(), Counter()
    for m in stages["D_formal_mappings"]:
        invalid_formal.update(invalid_mapping_reasons(m, entity_map, rel_map))
    for m in stages["E_candidate_mappings"]:
        invalid_candidate.update(invalid_mapping_reasons(m, entity_map, rel_map))
    if invalid_formal:
        errors.append(f"invalid formal mappings：{dict(invalid_formal)}")
    if invalid_candidate:
        errors.append(f"invalid candidate mappings：{dict(invalid_candidate)}")
    for link in links:
        if link["relationship_type"] != CONTRIBUTES_TO_NUTRIENT or link["form_entity_id"] == link["nutrient_entity_id"]:
            errors.append(f"form→nutrient 非 CONTRIBUTES_TO_NUTRIENT：{link['relationship_id']}")
        if entity_map[link["form_entity_id"]]["graph_label"] != INGREDIENT_FORM_LABEL \
                or entity_map[link["nutrient_entity_id"]]["graph_label"] != NUTRIENT_GROUP_LABEL:
            errors.append(f"form→nutrient 端點類型錯誤：{link['relationship_id']}")

    # FK：產品 / 既有 Ingredient 必須已存在（importer 不建立、不修改它們）
    if product_ids is not None:
        missing = {m["product_id"] for m in mappings} - product_ids
        if missing:
            errors.append(f"mapping 指向不存在的 product：{len(missing)}")
    if ingredient_ids is not None:
        missing = {m["source_ingredient_id"] for m in mappings if m["source_ingredient_id"]} - ingredient_ids
        if missing:
            errors.append(f"mapping 指向不存在的 Ingredient：{len(missing)}")

    # 與資料庫既有列比對
    diffs: dict[str, StageDiff] = {}
    existing_cache: dict[str, dict[str, dict[str, Any]]] = {}
    for stage, table, pk in STAGES:
        rows = stages[stage]
        cols = list(rows[0]) if rows else [pk]
        if table not in existing_cache:
            all_cols = sorted({c for s, t, _ in STAGES if t == table for r in stages[s][:1] for c in r} or {pk})
            existing_cache[table] = {str(r[pk]): r for r in store.fetch(table, all_cols)}
        existing = existing_cache[table]
        diff = StageDiff()
        for row in rows:
            old = existing.get(str(row[pk]))
            if old is None:
                diff.insert.append(row)
            elif _same({c: row[c] for c in cols}, old):
                diff.unchanged += 1
            else:
                diff.update.append(row)
        diffs[stage] = diff
    for table in {t for _, t, _ in STAGES}:
        planned = {str(r[pk]) for s, t, pk in STAGES if t == table for r in stages[s]}
        stale = [k for k in existing_cache[table] if k not in planned]
        last_stage = [s for s, t, _ in STAGES if t == table][-1]
        diffs[last_stage].stale = len(stale)
        if stale:
            errors.append(f"{table}：資料庫有 {len(stale)} 列不在本次計畫（importer 不刪除；版本切換需另行處理）")
    versions = {(str(r.get("corpus_version")), str(r.get("overlay_version")))
                for r in existing_cache["product_ingredient_entity_mappings"].values()}
    if versions - {(corpus.version, str(corpus.overlay_version))}:
        errors.append(f"資料庫已有其他版本的 mapping：{sorted(versions)}")

    notes = []
    if source_collisions:
        notes.append(f"base 與 overlay 同 source_id（保留 base 列）：{source_collisions}")
    return BridgePlan(corpus.version, corpus.overlay_version, stages, diffs, errors, collisions, invalid_formal,
                      invalid_candidate, results, corpus, notes)


# ----------------------------------------------------------------------------- 寫入

def _batched(store: BridgeStore, table: str, rows: list[dict[str, Any]], on_conflict: str) -> int:
    if table in PROTECTED_TABLES or table not in WRITABLE_TABLES:
        raise RuntimeError(f"ingredient bridge importer 不允許寫入 {table}")
    for i in range(0, len(rows), WRITE_BATCH):
        store.upsert(table, rows[i : i + WRITE_BATCH], on_conflict)
    return len(rows)


_NODE_SET = """
        SET n.canonical_name_en = r.canonical_name_en, n.display_name_zh = r.display_name_zh,
            n.identity_level = r.identity_level, n.entity_origin = r.entity_origin,
            n.corpus_version = r.corpus_version, n.overlay_version = r.overlay_version"""
_CONTAINS = """
        UNWIND $rows AS r
        MATCH (p:Product {{product_id: r.product_id}}), (e:{label} {{entity_id: r.entity_id}})
        MERGE (p)-[rel:CONTAINS]->(e)
        SET rel.source = r.source, rel.raw_text = r.raw_text, rel.raw_texts = r.raw_texts,
            rel.mapping_status = r.mapping_status, rel.match_method = r.match_method, rel.confidence = r.confidence,
            rel.review_status = r.review_status, rel.alias_ids = r.alias_ids,
            rel.alias_evidence_status = r.alias_evidence_status, rel.amount_raw = r.amount_raw, rel.unit = r.unit,
            rel.amount_basis = r.amount_basis, rel.corpus_version = r.corpus_version,
            rel.overlay_version = r.overlay_version"""
NEO4J_QUERIES = {
    "nutrient_groups": "UNWIND $rows AS r MERGE (n:NutrientGroup {entity_id: r.entity_id})" + _NODE_SET,
    "ingredient_forms": "UNWIND $rows AS r MERGE (n:IngredientForm {entity_id: r.entity_id})" + _NODE_SET,
    "contributes": """
        UNWIND $rows AS r
        MATCH (f:IngredientForm {entity_id: r.form_entity_id}), (g:NutrientGroup {entity_id: r.nutrient_entity_id})
        MERGE (f)-[rel:CONTRIBUTES_TO_NUTRIENT]->(g)
        SET rel.basis = r.basis, rel.relationship_id = r.relationship_id, rel.source_id = r.source_id,
            rel.source_url = r.source_url, rel.source_locator = r.source_locator, rel.review_status = r.review_status,
            rel.amount_conversion_allowed = false, rel.automatic_interaction_inheritance = false,
            rel.corpus_version = r.corpus_version, rel.overlay_version = r.overlay_version""",
    "contains_nutrient_group": _CONTAINS.format(label=NUTRIENT_GROUP_LABEL),
    "contains_ingredient_form": _CONTAINS.format(label=INGREDIENT_FORM_LABEL),
}
FORBIDDEN_GRAPH_TOKENS = ("SAME_AS", "INTERACTS_WITH", ":Interaction", "DELETE", "DETACH", "REMOVE")


def graph_payloads(plan: BridgePlan) -> dict[str, list[dict[str, Any]]]:
    gp = graph_plan(plan.corpus, plan.results)
    ents = plan.stages["A_entities"]
    return {
        "nutrient_groups": [e for e in ents if e["graph_label"] == NUTRIENT_GROUP_LABEL],
        "ingredient_forms": [e for e in ents if e["graph_label"] == INGREDIENT_FORM_LABEL],
        "contributes": gp["contributes_to_nutrient"],
        "contains_nutrient_group": [c for c in gp["contains"] if c["target_label"] == NUTRIENT_GROUP_LABEL],
        "contains_ingredient_form": [c for c in gp["contains"] if c["target_label"] == INGREDIENT_FORM_LABEL],
    }


def write_bridge_graph(neo4j, plan: BridgePlan, batch_size: int = 500) -> dict[str, int]:
    """Neo4j：只有 MERGE / SET；不刪除、不建立 SAME_AS / INTERACTS_WITH / Interaction。"""
    from app.services.neo4j_service import load_schema_statements

    for query in NEO4J_QUERIES.values():
        if any(tok in query for tok in FORBIDDEN_GRAPH_TOKENS):
            raise RuntimeError("bridge graph query 含禁止的關係 / 操作")
    for stmt in load_schema_statements(NEO4J_SCHEMA_FILE):
        neo4j.write(stmt)
    payloads = graph_payloads(plan)
    for stage, rows in payloads.items():
        for i in range(0, len(rows), batch_size):
            neo4j.write(NEO4J_QUERIES[stage], {"rows": rows[i : i + batch_size]})
    return {k: len(v) for k, v in payloads.items()}


def bridge_marker_present(store: BridgeStore, plan: BridgePlan) -> bool:
    rows = store.fetch("app_schema_meta", ["key", "value"])
    return any(r["key"] == plan.marker_key and r["value"] == plan.marker_value for r in rows)


def apply_bridge_import(plan: BridgePlan, store: BridgeStore, *, neo4j=None) -> dict[str, Any]:
    """依 A → E 寫入；計畫有錯誤時拒絕。完成標記最後寫；Neo4j 只在標記寫入後同步。"""
    if plan.errors:
        raise RuntimeError(f"bridge 匯入計畫有 {len(plan.errors)} 個錯誤，拒絕寫入：{plan.errors[:3]}")
    written = {}
    for stage, table, pk in STAGES:
        d = plan.diffs[stage]
        written[stage] = _batched(store, table, d.insert + d.update, pk)
    _batched(store, "app_schema_meta", [{"key": plan.marker_key, "value": plan.marker_value}], "key")
    report = plan.report()
    report["written"] = written
    if neo4j is not None:
        if not bridge_marker_present(store, plan):
            raise RuntimeError("bridge 完成標記不存在，不同步 Neo4j")
        report["graph_written"] = write_bridge_graph(neo4j, plan)
    return report
