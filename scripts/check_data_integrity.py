"""Real DB Integrity Gate（唯讀）：比對 Supabase（canonical）與 Neo4j（graph）。

用法：
    python -m scripts.check_data_integrity --expect-products 565

任一 blocker → exit code 1。不寫入任何資料。
"""

import argparse
import sys
from collections import Counter

from app.config.settings import get_settings
from app.ingestion.graph_sync import _fetch_all
from app.schemas.evidence import CLAIM_KIND_UNCLASSIFIED

# (Neo4j label, id property, Supabase table)
NODE_TABLES = [
    ("Product", "product_id", "products"),
    ("Company", "company_id", "companies"),
    ("Permit", "permit_id", "permits"),
    ("Ingredient", "ingredient_id", "ingredients"),
    ("Claim", "claim_id", "approved_claims"),
    ("Warning", "warning_id", "warnings"),
    ("Brand", "brand_id", "brands"),
    ("Source", "source_id", "source_registry"),
]
REL_TYPES = [
    "SUPPORTED_BY", "OWNS_OR_APPLIES_FOR", "REGISTERED_AS", "CONTAINS", "HAS_APPROVED_CLAIM",
    "HAS_WARNING", "HAS_BRAND", "ASSOCIATED_WITH_BRAND", "OWNS_BRAND",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="呷安心 Real DB Integrity Gate（唯讀）")
    parser.add_argument("--expect-products", type=int, default=565)
    args = parser.parse_args()

    from app.services.neo4j_service import Neo4jService
    from app.services.supabase_service import SupabaseService

    settings = get_settings()
    sb = SupabaseService(settings)
    sb.ensure_schema()
    neo = Neo4jService(settings)
    blockers: list[str] = []

    def check(ok: bool, label: str) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            blockers.append(label)

    def fetch(table: str, cols: str) -> list[dict]:
        return _fetch_all(sb, table, cols)

    try:
        neo.ensure_schema()
        print("[nodes] Supabase vs Neo4j canonical IDs")
        sb_ids: dict[str, set[str]] = {}
        for label, prop, table in NODE_TABLES:
            ids = [r["id"] for r in fetch(table, "id")]
            sb_ids[label] = set(ids)
            g = neo.read(f"MATCH (n:{label}) RETURN n.{prop} AS id")
            gids = [r["id"] for r in g]
            dup_sb = len(ids) - len(set(ids))
            dup_g = sum(c - 1 for c in Counter(gids).values() if c > 1)
            check(set(ids) == set(gids) and dup_sb == 0 and dup_g == 0 and None not in gids,
                  f"{label}: supabase {len(ids)} / neo4j {len(gids)}；duplicate {dup_sb}/{dup_g}；"
                  f"only_sb {len(set(ids) - set(gids))} only_neo4j {len(set(gids) - set(ids))}")
        check(len(sb_ids["Product"]) == args.expect_products, f"Product = {args.expect_products}")

        print("[relationships] counts / duplicates")
        products = fetch("products", "id,company_id,permit_id,raw_record_id,source_record_key")
        joins = {
            "CONTAINS": len(fetch("product_ingredients", "product_id")),
            "HAS_APPROVED_CLAIM": len(fetch("product_claims", "id")),
            "HAS_WARNING": len(fetch("product_warnings", "product_id")),
            "HAS_BRAND": len(fetch("product_brands", "product_id")),
            "OWNS_OR_APPLIES_FOR": sum(1 for p in products if p["company_id"]),
            "REGISTERED_AS": sum(1 for p in products if p["permit_id"]),
        }
        brands = fetch("brands", "id,company_id,company_relation,evidence_status,is_inferred")
        joins["ASSOCIATED_WITH_BRAND"] = sum(1 for b in brands if b["company_id"] and b["company_relation"] == "ASSOCIATED_WITH_BRAND")
        joins["OWNS_BRAND"] = sum(1 for b in brands if b["company_id"] and b["company_relation"] == "OWNS_BRAND")
        for rel in REL_TYPES:
            n = neo.read(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c")[0]["c"]
            dup = neo.read(
                f"MATCH (a)-[r:{rel}]->(b) WITH a, b, count(r) AS c WHERE c > 1 RETURN count(*) AS d"
            )[0]["d"]
            expected = joins.get(rel)
            label = f"{rel}: neo4j {n}" + (f" / supabase {expected}" if expected is not None else "") + f"；duplicate pairs {dup}"
            check(dup == 0 and (expected is None or n == expected), label)

        print("[orphans]")
        orphan_queries = {
            "Product 無 SUPPORTED_BY": "MATCH (p:Product) WHERE NOT (p)-[:SUPPORTED_BY]->(:Source) RETURN count(p) AS c",
            "Product 無 Permit": "MATCH (p:Product) WHERE NOT (p)-[:REGISTERED_AS]->(:Permit) RETURN count(p) AS c",
            "Product 無 Company": "MATCH (p:Product) WHERE NOT (:Company)-[:OWNS_OR_APPLIES_FOR]->(p) RETURN count(p) AS c",
            "Ingredient 無 Product": "MATCH (i:Ingredient) WHERE NOT (:Product)-[:CONTAINS]->(i) RETURN count(i) AS c",
            "Claim 無 Product": "MATCH (c:Claim) WHERE NOT (:Product)-[:HAS_APPROVED_CLAIM]->(c) RETURN count(c) AS c",
            "Warning 無 Product": "MATCH (w:Warning) WHERE NOT (:Product)-[:HAS_WARNING]->(w) RETURN count(w) AS c",
            "Brand 無 Product": "MATCH (b:Brand) WHERE NOT (:Product)-[:HAS_BRAND]->(b) RETURN count(b) AS c",
            "Company 無 Product": "MATCH (c:Company) WHERE NOT (c)-[:OWNS_OR_APPLIES_FOR]->(:Product) RETURN count(c) AS c",
        }
        for label, q in orphan_queries.items():
            n = neo.read(q)[0]["c"]
            check(n == 0, f"{label} = {n}")
        used = {
            "companies": {p["company_id"] for p in products},
            "permits": {p["permit_id"] for p in products},
            "ingredients": {r["ingredient_id"] for r in fetch("product_ingredients", "ingredient_id")},
            "approved_claims": {r["approved_claim_id"] for r in fetch("product_claims", "approved_claim_id")},
            "warnings": {r["warning_id"] for r in fetch("product_warnings", "warning_id")},
        }
        table_label = {t: lbl for lbl, _, t in NODE_TABLES}
        for table, ids in used.items():
            n = len(sb_ids[table_label[table]] - ids)
            check(n == 0, f"Supabase {table} 未被任何產品引用 = {n}")

        print("[brand semantics] inferred 不得變 official")
        bad_brand = [b for b in brands if b["is_inferred"] and b["evidence_status"] != "INFERRED"]
        bad_owns = [b for b in brands if b["is_inferred"] and b["company_relation"] == "OWNS_BRAND"]
        bad_pb = [r for r in fetch("product_brands", "evidence_status,is_inferred,derivation")
                  if r["derivation"] == "DERIVED_BRAND_PREFIX" and r["evidence_status"] != "INFERRED"]
        g_bad = neo.read(
            "MATCH (b:Brand) WHERE b.is_inferred = true AND b.evidence_status <> 'INFERRED' RETURN count(b) AS c"
        )[0]["c"]
        g_owns = neo.read("MATCH (:Company)-[r:OWNS_BRAND]->(b:Brand) WHERE b.is_inferred = true RETURN count(r) AS c")[0]["c"]
        check(not (bad_brand or bad_owns or bad_pb or g_bad or g_owns),
              f"inferred→official：brands {len(bad_brand)}、OWNS {len(bad_owns)}、product_brands {len(bad_pb)}、"
              f"neo4j brand {g_bad}、neo4j OWNS {g_owns}；brands evidence {dict(Counter(b['evidence_status'] for b in brands))}")

        print("[traceability]")
        raw = fetch("raw_import_records", "id,source_record_key,raw_data")
        raw_ids = {r["id"] for r in raw}
        no_raw = [p for p in products if not p["raw_record_id"] or p["raw_record_id"] not in raw_ids]
        check(len(raw) == len(products) and not no_raw, f"raw_import_records {len(raw)}；產品缺 raw link {len(no_raw)}")
        check(all(r["raw_data"] for r in raw), "raw_data 皆非空")
        claims = fetch("approved_claims", "id,claim_kind")
        unclassified = {c["id"] for c in claims if c["claim_kind"] == CLAIM_KIND_UNCLASSIFIED}
        pcs = fetch("product_claims", "approved_claim_id,claim_type,claim_text,source_record_key")
        check(all(pc["claim_type"] == "APPROVED_OFFICIAL" and pc["claim_text"] and pc["source_record_key"] for pc in pcs),
              f"product_claims 皆為 APPROVED_OFFICIAL 且有原文與來源（{len(pcs)}）；"
              f"未分類官方宣稱 {sum(1 for pc in pcs if pc['approved_claim_id'] in unclassified)}")

        print("[rag]")
        docs = fetch("rag_documents", "id,is_official,source_url")
        chunks = fetch("rag_chunks", "id,document_id,embedding_model")
        doc_ids = {d["id"] for d in docs}
        check(bool(docs) and bool(chunks) and all(c["document_id"] in doc_ids for c in chunks)
              and all(d["is_official"] for d in docs),
              f"rag_documents {len(docs)} / rag_chunks {len(chunks)}；embedding_model {dict(Counter(c['embedding_model'] for c in chunks))}")
    finally:
        neo.close()

    print(f"\n[gate] {'PASS' if not blockers else 'FAIL'}：blockers {len(blockers)}")
    for b in blockers:
        print(f"  - {b}")
    return 0 if not blockers else 1


if __name__ == "__main__":
    sys.exit(main())
