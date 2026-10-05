"""Product Ingredient Mapping Bridge — read-only dry-run（replay＋import gate）。

只讀 Supabase（products / ingredients / product_ingredients / entity_aliases / raw_import_records 的
ingredient_staging），以 Interaction Corpus＋reviewed overlay 計算 mapping；不寫 Supabase、不寫 Neo4j。
同一份快照另跑「只用 base corpus」做 baseline，逐成分單位比較 overlay 的增減（含 formal → candidate 降級）。

    python -m scripts.dry_run_ingredient_entity_mapping
    python -m scripts.dry_run_ingredient_entity_mapping --input-json <唯讀匯出的 JSON> --report-json <輸出>
    python -m scripts.dry_run_ingredient_entity_mapping --no-overlay        # 只用 base corpus

--input-json 的格式與 `load_production_inputs` 回傳值相同（products / ingredients / product_ingredients /
aliases / raw_staging），方便重跑同一份唯讀快照。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.ingestion.ingredient_entity_mapping import (
    CORPUS_DIR,
    FORMAL_STATUSES,
    OVERLAY_DIR,
    InteractionCorpus,
    MappingStatus,
    MatchMethod,
    ProductMappingResult,
    graph_plan,
    health_food_source,
    load_interaction_corpus,
    load_reviewed_overlay,
    map_product,
    registration_source,
)

CATEGORIES = ("health_food", "domestic_vitamin_tablet_capsule", "imported_tablet_capsule")
NUTRIENT_SCOPE = ("N_CA", "N_FE", "N_MG", "N_ZN", "N_CU", "N_D", "N_K", "N_C", "N_OMEGA3")
PAGE = 1000
ORDER_KEYS = {"product_ingredients": ("product_id", "ingredient_id")}  # 分頁需唯一排序
FORM_CONTEXT_PRECEDENCE = "PRODUCT_FORM_CONTEXT_OVER_BASE_NUTRIENT_ALIAS"


def load_production_inputs() -> dict[str, list[dict[str, Any]]]:
    """唯讀分頁讀取（只有 select）。"""
    from app.config.settings import get_settings
    from app.services.supabase_service import SupabaseService

    svc = SupabaseService(get_settings())
    svc.ensure_schema()

    def rows(table: str, cols: str, **eq: Any) -> list[dict[str, Any]]:
        out, start = [], 0
        while True:
            builder = svc.table(table).select(cols)
            for key, value in eq.items():
                builder = builder.eq(key, value)
            for column in ORDER_KEYS.get(table, ("id",)):
                builder = builder.order(column)
            page = svc.select(builder.range(start, start + PAGE - 1))
            out += page
            if len(page) < PAGE:
                return out
            start += PAGE

    return {
        "products": rows("products", "id,product_category,product_name_zh,source_id,source_record_key,source_url,retrieved_at,raw_record_id"),
        "ingredients": rows("ingredients", "id,name_zh,name_en,name_normalized"),
        "product_ingredients": rows("product_ingredients",
                                    "product_id,ingredient_id,raw_text,amount_text,source_url,retrieved_at"),
        "raw_staging": rows("raw_import_records", "id,source_record_key,staging:raw_data->ingredient_staging"),
        "aliases": rows("entity_aliases", "id,ingredient_id,alias,alias_normalized,alias_type,evidence_status",
                        entity_type="INGREDIENT"),
    }


def map_all(corpus: InteractionCorpus, data: dict[str, list[dict[str, Any]]]) -> list[ProductMappingResult]:
    ingredients = {r["id"]: r for r in data["ingredients"]}
    aliases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in data["aliases"]:
        aliases[a["ingredient_id"]].append(a)
    pis: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pi in data["product_ingredients"]:
        pis[pi["product_id"]].append(pi)
    staging = {r["id"]: r.get("staging") for r in data["raw_staging"]}
    results = []
    for product in sorted(data["products"], key=lambda p: p["id"]):
        if product["product_category"] == "health_food":
            src = health_food_source(product, pis.get(product["id"], []), ingredients, aliases)
        else:
            src = registration_source(product, staging.get(product.get("raw_record_id")))
        results.append(map_product(corpus, src))
    return results


def _primary(r: ProductMappingResult):
    return [m for m in r.mappings if m.mapping_status != MappingStatus.FORM_TO_GROUP]


def summarize(corpus: InteractionCorpus, results: list[ProductMappingResult]) -> dict[str, Any]:
    def block(rs: list[ProductMappingResult]) -> dict[str, Any]:
        units = Counter(m.mapping_status.value for r in rs for m in r.mappings)
        methods = Counter(m.match_method.value for r in rs for m in r.mappings)
        matched = [r for r in rs if r.formal()]
        candidates_only = [r for r in rs if not r.formal() and any(
            m.mapping_status in (MappingStatus.AMBIGUOUS, MappingStatus.NEEDS_REVIEW) for m in r.mappings)]
        scope = {}
        for gid in NUTRIENT_SCOPE:
            scope[gid] = {
                "formal_products": sum(1 for r in rs if gid in r.nutrient_groups()),
                "candidate_only_products": sum(1 for r in rs if gid not in r.nutrient_groups() and any(
                    gid in (e, corpus.parent(e)) for m in r.mappings for e in m.candidate_entity_ids)),
            }
        scope["F_FISH_OIL"] = {  # 魚油本身（F_*），與 Omega-3 營養素群分開計
            "formal_products": sum(1 for r in rs if "F_FISH_OIL" in r.contains_targets()),
            "candidate_only_products": sum(1 for r in rs if "F_FISH_OIL" not in r.contains_targets() and any(
                "F_FISH_OIL" in m.candidate_entity_ids for m in r.mappings)),
        }
        return {
            "products": len(rs),
            "products_without_source_ingredients": sum(1 for r in rs if not r.source.ingredients),
            "products_with_formal_match": len(matched),
            "products_with_candidates_only": len(candidates_only),
            "products_no_match_only": len(rs) - len(matched) - len(candidates_only)
            - sum(1 for r in rs if not r.source.ingredients),
            # 只因 corpus 中文譯名是 DISPLAY_ONLY 而停在候選的產品
            "candidate_only_products_with_display_translation_exact": sum(1 for r in candidates_only if any(
                m.match_method == MatchMethod.DISPLAY_TRANSLATION_EXACT for m in r.mappings)),
            "mapping_units": {s.value: units.get(s.value, 0) for s in MappingStatus},
            "exact_mappings": units.get(MappingStatus.VERIFIED_EXACT.value, 0),
            # exact 中同時有 overlay alias 佐證（同一 entity）
            "overlay_confirmed_exact_mappings": sum(1 for r in rs for m in _primary(r)
                                                    if m.mapping_status == MappingStatus.VERIFIED_EXACT
                                                    and m.alias_evidence_status),
            "reviewed_alias_mappings": methods.get(MatchMethod.REVIEWED_OVERLAY_ALIAS_EXACT.value, 0)
            + methods.get(MatchMethod.REVIEWED_ALIAS_EXACT.value, 0),
            "base_supported_alias_mappings": methods.get(MatchMethod.CORPUS_SOURCED_SYNONYM_EXACT.value, 0)
            + methods.get(MatchMethod.OFFICIAL_PARENTHETICAL.value, 0),
            "form_to_group_mappings": units.get(MappingStatus.FORM_TO_GROUP.value, 0),
            "overlay_candidate_units": methods.get(MatchMethod.REVIEWED_OVERLAY_CANDIDATE.value, 0),
            "overlay_form_context_precedence_units": sum(
                1 for r in rs for m in _primary(r) if m.overlay_precedence == FORM_CONTEXT_PRECEDENCE),
            "match_methods": dict(Counter(m.match_method.value for r in rs for m in r.mappings if m.is_formal)),
            "matched_products_by_source_parse_status": dict(Counter(
                r.source.source_parse_status or "N/A" for r in matched)),
            "source_parse_status": dict(Counter(r.source.source_parse_status or "N/A" for r in rs)),
            "nutrient_scope": scope,
        }

    by_cat = {c: [r for r in results if r.source.product_category == c] for c in CATEGORIES}
    formal_entities = Counter(m.entity_id for r in results for m in r.mappings if m.mapping_status in FORMAL_STATUSES)
    review_queue = Counter((m.raw_text, ",".join(m.candidate_entity_ids), m.mapping_status.value)
                           for r in results for m in r.mappings
                           if m.mapping_status in (MappingStatus.AMBIGUOUS, MappingStatus.NEEDS_REVIEW))
    plan = graph_plan(corpus, results)
    return {
        "corpus_version": corpus.version,
        "overlay_version": corpus.overlay_version,
        "corpus_dir": corpus.directory,
        "corpus_file_sha256": corpus.file_sha256,
        "overlay_file_sha256": corpus.overlay.file_sha256 if corpus.overlay else {},
        "reviewed_aliases": len(corpus.reviewed_aliases),
        "overlay_aliases": len(corpus.overlay.aliases) if corpus.overlay else 0,
        "total": block(results),
        "by_category": {c: block(rs) for c, rs in by_cat.items()},
        "formal_entity_counts": dict(formal_entities.most_common()),
        "review_queue_top": [
            {"raw_text": t, "candidates": c, "status": s, "occurrences": n} for (t, c, s), n in review_queue.most_common(40)
        ],
        "neo4j_plan": {
            "nodes": plan["nodes"], "relationships": plan["relationship_counts"],
            "interaction_nodes": plan["interaction_nodes"],
            "contains_by_target_label": dict(Counter(c["target_label"] for c in plan["contains"])),
            "contains_by_entity_origin": dict(Counter(c["entity_origin"] for c in plan["contains"])),
            "contributes_by_basis": dict(Counter(c["basis"] for c in plan["contributes_to_nutrient"])),
        },
        "writes": {"supabase": 0, "neo4j": 0},
    }


def compare(base: list[ProductMappingResult], overlay: list[ProductMappingResult]) -> dict[str, Any]:
    """同一份快照：base corpus vs base＋overlay。逐產品 / 逐成分單位比較（找出 overlay 造成的降級）。"""
    def units(r: ProductMappingResult) -> dict[tuple, Any]:
        return {(m.source_ingredient_id, m.token_index, m.raw_text): m for m in _primary(r)}

    by_id = {r.product_id: r for r in overlay}
    out: dict[str, Any] = {}
    for cat in CATEGORIES:
        stats = Counter()
        examples: list[dict[str, Any]] = []
        for b in (r for r in base if r.source.product_category == cat):
            o = by_id[b.product_id]
            stats["products_gained_formal"] += bool(o.formal()) and not b.formal()
            stats["products_lost_formal"] += bool(b.formal()) and not o.formal()
            ou = units(o)
            for key, bm in units(b).items():
                om = ou[key]
                if bm.is_formal and om.is_formal and bm.entity_id != om.entity_id:
                    stats["units_formal_entity_changed"] += 1
                if bm.is_formal and not om.is_formal:
                    stats["units_formal_to_candidate"] += 1
                    if len(examples) < 10:
                        examples.append({"raw_text": bm.raw_text, "base_entity": bm.entity_id,
                                         "overlay_status": om.mapping_status.value,
                                         "overlay_alias_ids": list(om.matched_alias_ids)})
                if om.is_formal and not bm.is_formal:
                    stats["units_not_formal_to_formal"] += 1
        out[cat] = {**{k: stats.get(k, 0) for k in (
            "products_gained_formal", "products_lost_formal", "units_not_formal_to_formal",
            "units_formal_to_candidate", "units_formal_entity_changed")},
            "formal_to_candidate_examples": examples}
    return out


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def health_food_protection(data: dict[str, list[dict[str, Any]]], results: list[ProductMappingResult],
                           input_digest_before: str) -> dict[str, Any]:
    """既有 health_food ProductIngredient / Ingredient 不被重建、覆蓋或刪除；只新增 entity mapping（另一張表）。"""
    hf_ids = {p["id"] for p in data["products"] if p["product_category"] == "health_food"}
    links = Counter((pi["product_id"], pi["ingredient_id"], pi.get("raw_text") or "")
                    for pi in data["product_ingredients"] if pi["product_id"] in hf_ids)
    carried = Counter()
    raw_ok = True
    for r in results:
        if r.product_id not in hf_ids:
            continue
        raw_by_ing = {i.source_ingredient_id: i.raw_text for i in r.source.ingredients}
        for ing in r.source.ingredients:
            carried[(r.product_id, ing.source_ingredient_id, ing.raw_text)] += 1
        raw_ok &= all(m.raw_text == raw_by_ing[m.source_ingredient_id] for m in r.mappings)
    return {
        "health_food_products": len(hf_ids),
        "health_food_product_ids_sha256": _digest(sorted(hf_ids)),
        "result_product_ids_equal_input": {r.product_id for r in results if r.product_id in hf_ids} == hf_ids,
        "existing_product_ingredient_links": sum(links.values()),
        "existing_products_with_ingredient_links": len({pid for pid, _, _ in links}),
        "links_carried_exactly_once": carried == links,
        "raw_text_unchanged": raw_ok,
        "input_snapshot_unchanged": _digest(data) == input_digest_before,
        "writes_to_product_ingredients_or_ingredients": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus-dir", default=str(CORPUS_DIR))
    parser.add_argument("--overlay-dir", default=str(OVERLAY_DIR))
    parser.add_argument("--no-overlay", action="store_true", help="只用 base corpus（不載入 reviewed overlay）")
    parser.add_argument("--input-json", help="唯讀匯出的輸入快照；未提供時直接唯讀查詢 Supabase")
    parser.add_argument("--dump-input", help="把唯讀查詢到的輸入存成快照 JSON（之後可用 --input-json 重跑）")
    parser.add_argument("--report-json", help="完整報告輸出路徑")
    args = parser.parse_args(argv)

    base_corpus = load_interaction_corpus(args.corpus_dir)
    if args.input_json:
        data = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    else:
        data = load_production_inputs()
        if args.dump_input:
            Path(args.dump_input).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    input_digest = _digest(data)
    base_results = map_all(base_corpus, copy.deepcopy(data))
    report: dict[str, Any] = {"input_snapshot_sha256": input_digest}
    if args.no_overlay:
        report.update(summarize(base_corpus, base_results))
        results = base_results
    else:
        corpus = load_interaction_corpus(args.corpus_dir, overlay=load_reviewed_overlay(args.overlay_dir))
        results = map_all(corpus, data)
        report.update(summarize(corpus, results))
        base = summarize(base_corpus, base_results)
        report["baseline_without_overlay"] = {"total": base["total"], "by_category": base["by_category"],
                                              "neo4j_plan": base["neo4j_plan"]}
        report["overlay_vs_baseline"] = compare(base_results, results)
    report["health_food_protection"] = health_food_protection(data, results, input_digest)
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    brief = {k: report[k] for k in ("corpus_version", "overlay_version", "neo4j_plan", "health_food_protection",
                                    "writes")}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
