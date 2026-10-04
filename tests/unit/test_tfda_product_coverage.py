"""TFDA 產品登記覆蓋擴充 importer（虛構資料 TEST_ONLY；最後一組以研究 snapshot 驗證筆數，檔案不存在時 skip）。

涵蓋：canonical key（前導零、deterministic）、民國日期、三種法律身分分開、official_status = NULL、
NEEDS_REVIEW 不阻擋登記匯入、隔離（缺字號 / 缺品名 / 未知類別 / key 碰撞 / DB ID 碰撞）、不以品名合併、
不產生核准功效 / 成分 KG、idempotent、version-aware、既有公司只沿用、--apply 需 confirm-project。
"""

import copy
import csv
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from app.ingestion import tfda_product_coverage as cov
from app.normalization import stable_id

ROOT = Path(__file__).resolve().parents[2]
REAL_SNAPSHOT = ROOT / "data" / "tfda_product_coverage" / "2026-10-04"
DOMESTIC_HEADER = ["查驗登記證號", "中文品名", "英文品名", "有效日期", "發證日期", "申請商名稱", "申請商電話", "申請商地址", "成分"]
IMPORTED_HEADER = ["核備函字號", "中文品名", "英文品名", "有效日期", "發證日期", "申請商名稱", "申請商電話", "申請商地址", "成分",
                   "簽審文件編號"]
RETRIEVED = {"24": "2026-10-04T03:13:44.070804+00:00", "23": "2026-10-04T03:13:51.254225+00:00"}
AS_OF = date(2026, 10, 4)


# ---------------------------------------------------------------------------- 虛構 snapshot


def dom(number="9900000001", name="TEST_ONLY 維生素甲錠", review="READY", ingredients="TEST_ONLY成分甲;;TEST_ONLY成分乙;;",
        issue="1100101", expiry="1200101", applicant="TEST_ONLY 甲生技股份有限公司"):
    return {"row": {"查驗登記證號": number, "中文品名": name, "英文品名": "", "有效日期": expiry, "發證日期": issue,
                    "申請商名稱": applicant, "申請商電話": "00-0000000", "申請商地址": "TEST_ONLY 地址", "成分": ingredients},
            "review": review}


def imp(number="0099000001", name="TEST_ONLY 魚油膠囊", review="NEEDS_REVIEW", issue="890411", expiry="1190424",
        applicant="TEST_ONLY 乙貿易有限公司", ingredients="TEST_ONLY FISH OIL;;GELATIN;;"):
    return {"row": {"核備函字號": number, "中文品名": name, "英文品名": "TEST ONLY FISH OIL", "有效日期": expiry, "發證日期": issue,
                    "申請商名稱": applicant, "申請商電話": "00-0000001", "申請商地址": "TEST_ONLY 地址二", "成分": ingredients,
                    "簽審文件編號": f"TESTDOC{number}"},
            "review": review}


def _write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def _normalized(info_id: str, spec_rows: list[dict], number_field: str, category: str) -> list[dict]:
    out = []
    for i, item in enumerate(spec_rows, start=1):
        raw = item["row"]
        tokens = [t.strip() for t in (raw["成分"] or "").split(";;") if t.strip()]

        def iso(v):
            return cov.parse_roc_date(v) or "NULL"

        out.append({
            "source_row_key": f"TFDA:{info_id}:{raw[number_field]}", "product_category": category,
            "raw_row_number": str(i), "raw_row_sha256": cov.raw_row_sha256(raw),
            "raw_source_fields_json": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            "issue_date": iso(raw["發證日期"]), "expiry_date": iso(raw["有效日期"]), "official_status": "NULL",
            "ingredients_normalized": json.dumps({"format": "source_tokens_v1", "delimiter": ";;", "tokens": tokens,
                                                  "chemical_identification": "NOT_PERFORMED"}, ensure_ascii=False)
            if tokens else "NULL",
            "review_status": item["review"],
            "review_reasons": "[]" if item["review"] == "READY" else '["REPEATED_SOURCE_TOKENS"]',
        })
    return out


NORM_HEADER = ["source_row_key", "product_category", "raw_row_number", "raw_row_sha256", "raw_source_fields_json",
               "issue_date", "expiry_date", "official_status", "ingredients_normalized", "review_status", "review_reasons"]


def build_snapshot(tmp_path: Path, domestic: list[dict], imported: list[dict], *, retrieved=None,
                   normalized_overrides=None) -> Path:
    retrieved = retrieved or RETRIEVED
    d = tmp_path / "snap"
    d.mkdir(parents=True, exist_ok=True)
    specs = cov.DATASETS
    datasets = {}
    for info_id, rows, header in (("24", domestic, DOMESTIC_HEADER), ("23", imported, IMPORTED_HEADER)):
        spec = specs[info_id]
        _write_csv(d / spec.raw_file, header, [r["row"] for r in rows])
        norm = _normalized(info_id, rows, spec.number_field, spec.dataset_category)
        for (oid, index), patch in (normalized_overrides or {}).items():
            if oid == info_id:
                norm[index].update(patch)
        _write_csv(d / spec.normalized_file, NORM_HEADER, norm)
        datasets[info_id] = {"raw_rows": len(rows), "raw_sha256": cov.file_sha256(d / spec.raw_file),
                             "raw_field_names": header}
    _write_csv(d / cov.FILES["sources"], ["source_id", "source_info_id", "source_role", "source_url", "retrieved_at",
                                          "retrieval_status", "sha256"], [
        {"source_id": f"SRC_{i}", "source_info_id": i, "source_role": "CORE_FULL_PRODUCT_EXPORT",
         "source_url": f"https://data.fda.gov.tw/data/opendata/export/{i}/csv", "retrieved_at": retrieved[i],
         "retrieval_status": "SUCCESS", "sha256": datasets[i]["raw_sha256"]} for i in ("24", "23")])
    _write_csv(d / cov.FILES["duplicates"], ["duplicate_group_id", "source_row_key"], [])
    _write_csv(d / cov.FILES["candidates"], ["candidate_id", "comparison_scope", "classification"], [
        {"candidate_id": "MATCH:test", "comparison_scope": "DOMESTIC_VS_IMPORTED", "classification": "CONFIRMATION_REQUIRED"}])
    (d / cov.FILES["validation"]).write_text(json.dumps({"research_date": "2026-10-04", "datasets": datasets},
                                                        ensure_ascii=False), encoding="utf-8")
    names = [specs["24"].raw_file, specs["24"].normalized_file, specs["23"].raw_file, specs["23"].normalized_file,
             cov.FILES["sources"], cov.FILES["validation"], cov.FILES["candidates"], cov.FILES["duplicates"]]
    (d / cov.FILES["checksums"]).write_text(
        "".join(f"{hashlib.sha256((d / n).read_bytes()).hexdigest()}  {n}\n" for n in names), encoding="utf-8")
    return d


class FakeStore:
    """記憶體 store：以 on_conflict 欄位 upsert；記錄寫入次數。"""

    def __init__(self, tables=None):
        self.tables: dict[str, list[dict]] = copy.deepcopy(tables or {})
        self.writes: dict[str, int] = {}

    def fetch(self, table, columns, eq=None):
        return [{c: row.get(c) for c in columns} for row in self.tables.get(table, [])]

    def upsert(self, table, rows, on_conflict):
        keys = on_conflict.split(",")
        existing = self.tables.setdefault(table, [])
        for row in rows:
            match = next((r for r in existing if all(r.get(k) == row.get(k) for k in keys)), None)
            if match:
                match.update(copy.deepcopy(row))
            else:
                existing.append(copy.deepcopy(row))
        self.writes[table] = self.writes.get(table, 0) + len(rows)


def plan_for(path, store=None, **kw):
    return cov.plan_coverage_import(cov.load_snapshot(path), store, as_of=AS_OF, **kw)


# ---------------------------------------------------------------------------- 一般規則


def test_canonical_key_preserves_leading_zero_and_ids_are_deterministic():
    key = cov.canonical_key("23", "0099000001")
    assert key == "TFDA:23:0099000001"
    assert cov.product_id_for(key) == cov.product_id_for("TFDA:23:0099000001")
    # 不同資料集的同一字號是不同鍵；與 health_food 的 id 命名空間（TW:<字號>）不同
    assert cov.product_id_for(key) != cov.product_id_for("TFDA:24:0099000001")
    assert cov.product_id_for(key) != stable_id("product", "TW:0099000001")


def test_roc_dates_strict():
    assert cov.parse_roc_date("890411") == "2000-04-11"
    assert cov.parse_roc_date("1100101") == "2021-01-01"
    assert cov.parse_roc_date("") is None
    for bad in ("1150230", "11001", "2021-01-01"):
        with pytest.raises(ValueError):
            cov.parse_roc_date(bad)


def test_derived_temporal_status_is_date_comparison_only():
    assert cov.derived_temporal_status("2026-10-04", AS_OF) == "CURRENT_BY_DATE"
    assert cov.derived_temporal_status("2026-10-03", AS_OF) == "EXPIRED_BY_DATE"
    assert cov.derived_temporal_status(None, AS_OF) is None


# ---------------------------------------------------------------------------- dry-run


def test_dry_run_imports_ready_and_needs_review_as_separate_categories(tmp_path):
    path = build_snapshot(tmp_path, [dom(), dom("9900000002", review="NEEDS_REVIEW")], [imp(), imp("0099000002", review="READY")])
    plan = plan_for(path)
    report = plan.report()
    assert plan.errors == []
    assert report["products"] == {"would_insert": 4, "would_update": 0, "would_remain_unchanged": 0, "would_quarantine": 0}
    assert report["review_status_importable"] == {"READY": 2, "NEEDS_REVIEW": 2}
    cats = {r.product["product_category"] for r in plan.importable}
    assert cats == {"domestic_vitamin_tablet_capsule", "imported_tablet_capsule"}
    assert "health_food" not in cats


def test_registration_rows_keep_official_status_null_and_full_permit_number(tmp_path):
    plan = plan_for(build_snapshot(tmp_path, [dom()], [imp()]))
    rec = next(r for r in plan.importable if r.info_id == "23")
    assert rec.key == "TFDA:23:0099000001"
    assert rec.permit["permit_number"] == rec.permit["permit_code"] == "0099000001"
    assert rec.permit["permit_type"] == "核備函"
    assert rec.permit["status"] == rec.product["status"] == "UNKNOWN"
    assert rec.permit["status_raw"] is None
    reg = rec.raw_data["registration"]
    assert reg["official_status"] is None
    assert reg["issue_date"] == "2000-04-11" and reg["expiry_date"] == "2030-04-24"
    assert reg["signing_document_no"] == "TESTDOC0099000001"
    assert rec.raw_data["raw_source_fields"] == rec.raw
    assert rec.raw_hash == cov.raw_row_sha256(rec.raw)
    assert "derived_temporal_status" not in json.dumps(rec.raw_data, ensure_ascii=False)


def test_no_health_claims_or_ingredient_graph(tmp_path):
    plan = plan_for(build_snapshot(tmp_path, [dom()], [imp()]))
    report = plan.report()
    assert set(report["tables"]) == {"source_registry", "raw_import_records", "companies", "permits", "products"}
    assert all(v == 0 for v in report["derived_rows_written"].values())
    for key in ("Ingredient", "CONTAINS", "HAS_APPROVED_CLAIM", "INTERACTS_WITH"):
        assert report["graph_plan"][key] == 0
    staging = plan.importable[0].raw_data["ingredient_staging"]
    assert staging["ingredients_raw"] == "TEST_ONLY成分甲;;TEST_ONLY成分乙;;"
    assert staging["parser_draft"]["tokens"] == ["TEST_ONLY成分甲", "TEST_ONLY成分乙"]
    assert staging["interaction_eligible"] is False
    assert staging["chemical_identification"] == "NOT_PERFORMED"
    queries = " ".join(cov.GRAPH_QUERIES.values())
    assert "Ingredient" not in queries and "Claim" not in queries and "product_category" in queries


def test_quarantine_missing_number_name_and_unknown_category(tmp_path):
    path = build_snapshot(
        tmp_path, [dom(), dom(number=""), dom("9900000003", name="")], [imp()],
        normalized_overrides={("23", 0): {"product_category": "SOMETHING_ELSE"}})
    plan = plan_for(path)
    report = plan.report()
    assert report["products"]["would_insert"] == 1
    assert report["products"]["would_quarantine"] == 3
    assert report["missing_required_fields"] == 2
    assert report["quarantine_reasons"]["UNKNOWN_CATEGORY"] == 1


def test_key_collision_and_cross_dataset_number_collision_quarantine_all_rows(tmp_path):
    path = build_snapshot(tmp_path, [dom("9900000001"), dom("9900000001", name="TEST_ONLY 另一品名"), dom("0099000001")],
                          [imp("0099000001")])
    report = plan_for(path).report()
    assert report["key_collisions"] == 4
    assert report["products"]["would_insert"] == 0
    assert report["quarantine_reasons"] == {"KEY_COLLISION": 2, "REGISTRATION_NUMBER_COLLISION": 2}


def test_same_name_distinct_numbers_are_not_merged(tmp_path):
    plan = plan_for(build_snapshot(tmp_path, [dom("9900000001"), dom("9900000002")], [imp(name="TEST_ONLY 維生素甲錠")]))
    ids = {r.product["id"] for r in plan.importable}
    assert len(ids) == 3
    assert plan.report()["products"]["would_insert"] == 3


def test_normalized_mismatch_is_quarantined_not_guessed(tmp_path):
    path = build_snapshot(tmp_path, [dom()], [imp()], normalized_overrides={("24", 0): {"expiry_date": "2099-01-01"}})
    report = plan_for(path).report()
    assert report["quarantine_reasons"] == {"NORMALIZED_MISMATCH": 1}


def test_checksum_mismatch_blocks_apply(tmp_path):
    path = build_snapshot(tmp_path, [dom()], [imp()])
    (path / cov.FILES["checksums"]).write_text("0" * 64 + "  " + cov.DATASETS["24"].raw_file + "\n", encoding="utf-8")
    plan = plan_for(path)
    assert any("SHA256SUMS" in e for e in plan.errors)
    with pytest.raises(RuntimeError):
        cov.apply_coverage_import(plan, FakeStore())


# ---------------------------------------------------------------------------- apply / idempotent / version-aware


def test_apply_then_rerun_is_unchanged_and_writes_nothing(tmp_path):
    path = build_snapshot(tmp_path, [dom()], [imp()])
    store = FakeStore()
    cov.apply_coverage_import(plan_for(path, store), store)
    assert len(store.tables["products"]) == 2 and len(store.tables["raw_import_records"]) == 2
    store.writes.clear()
    plan = plan_for(path, store)
    report = plan.report()
    assert report["products"] == {"would_insert": 0, "would_update": 0, "would_remain_unchanged": 2, "would_quarantine": 0}
    assert all(c["insert"] == 0 and c["update"] == 0 for c in report["tables"].values())
    cov.apply_coverage_import(plan, store)
    assert sum(store.writes.values()) == 0


def test_changed_official_content_adds_raw_version_and_updates_product(tmp_path):
    store = FakeStore()
    cov.apply_coverage_import(plan_for(build_snapshot(tmp_path, [dom()], [imp()]), store), store)
    newer = {"24": "2026-12-01T00:00:00+00:00", "23": "2026-12-01T00:00:00+00:00"}
    path2 = build_snapshot(tmp_path / "v2", [dom(name="TEST_ONLY 維生素甲錠（新）")], [imp()], retrieved=newer)
    plan = plan_for(path2, store)
    report = plan.report()
    assert report["tables"]["raw_import_records"]["insert"] == 1      # 新內容 → 新 raw 版本
    assert report["products"]["would_update"] == 2                    # 改名 + retrieved_at 更新
    cov.apply_coverage_import(plan, store)
    assert len(store.tables["raw_import_records"]) == 3               # 舊 raw 版本保留


def test_older_snapshot_is_rejected_unless_allowed(tmp_path):
    store = FakeStore()
    newer = {"24": "2026-12-01T00:00:00+00:00", "23": "2026-12-01T00:00:00+00:00"}
    cov.apply_coverage_import(plan_for(build_snapshot(tmp_path, [dom()], [imp()], retrieved=newer), store), store)
    older = build_snapshot(tmp_path / "old", [dom()], [imp()])
    assert any("拒絕降版" in e for e in plan_for(older, store).errors)
    assert plan_for(older, store, allow_older_snapshot=True).errors == []


def test_db_id_collision_with_other_source_is_quarantined(tmp_path):
    key = "TFDA:24:9900000001"
    store = FakeStore({
        "products": [{"id": cov.product_id_for(key), "source_id": "other-source", "product_category": "health_food",
                      "product_name_normalized": "x"}],
        "permits": [{"id": "other-permit", "permit_number": "0099000001", "source_id": "other-source"}],
    })
    report = plan_for(build_snapshot(tmp_path, [dom()], [imp()]), store).report()
    assert report["quarantine_reasons"] == {"DB_ID_COLLISION": 2}
    assert report["products"]["would_insert"] == 0


def test_existing_company_is_reused_not_updated_and_health_food_name_overlap_is_audit_only(tmp_path):
    path = build_snapshot(tmp_path, [dom()], [imp()])
    first = plan_for(path).importable[0]
    company_id = first.company["id"]
    hf = {"id": "hf-product", "source_id": "hf-source", "product_category": "health_food",
          "product_name_normalized": first.product["product_name_normalized"]}
    existing_company = {"id": company_id, "name_zh": "既有名稱（不應被改）", "source_id": "hf-source"}
    store = FakeStore({"companies": [existing_company], "products": [hf]})
    plan = plan_for(path, store)
    report = plan.report()
    assert report["tables"]["companies"]["reused_existing"] == 1
    assert report["cross_category_candidates"]["db_health_food_exact_name_overlap"] == 1
    cov.apply_coverage_import(plan, store)
    assert next(c for c in store.tables["companies"] if c["id"] == company_id)["name_zh"] == "既有名稱（不應被改）"
    assert next(p for p in store.tables["products"] if p["id"] == "hf-product") == hf


def test_graph_write_uses_canonical_rows_with_category(tmp_path):
    class FakeNeo4j:
        def __init__(self):
            self.calls = []

        def write(self, query, params):
            self.calls.append((query, params["rows"]))

    plan = plan_for(build_snapshot(tmp_path, [dom()], [imp()]))
    neo = FakeNeo4j()
    counts = cov.write_coverage_graph(neo, plan)
    assert counts == {"sources": 2, "companies": 2, "permits": 2, "products": 2, "links": 2}
    product_rows = next(rows for q, rows in neo.calls if q == cov.GRAPH_QUERIES["products"])
    assert {r["product_category"] for r in product_rows} == {"domestic_vitamin_tablet_capsule", "imported_tablet_capsule"}
    assert "ON CREATE SET" in cov.GRAPH_QUERIES["companies"]


def test_cli_apply_requires_confirm_project(monkeypatch, capsys):
    from scripts import import_tfda_product_coverage as cli

    monkeypatch.setattr("sys.argv", ["x", "--apply"])
    assert cli.main() == 2
    assert "--confirm-project" in capsys.readouterr().out


# ---------------------------------------------------------------------------- 研究 snapshot（真實檔案；不連線）


@pytest.mark.skipif(not (REAL_SNAPSHOT / cov.FILES["validation"]).is_file(), reason="研究 snapshot 不存在")
def test_real_snapshot_dry_run_counts():
    snapshot = cov.load_snapshot(REAL_SNAPSHOT)
    plan = cov.plan_coverage_import(snapshot, as_of=AS_OF)
    report = plan.report()
    assert plan.errors == []
    assert report["datasets"]["24"]["rows"] == 1705 and report["datasets"]["23"]["rows"] == 7407
    assert report["total_rows"] == 9112
    assert report["products"]["would_insert"] == 9112 and report["products"]["would_quarantine"] == 0
    assert report["key_collisions"] == 0 and report["missing_required_fields"] == 0
    assert report["review_status"] == {"READY": 5069, "NEEDS_REVIEW": 4043}
    assert report["cross_category_candidates"]["file_pairs"] == 103
    keys = [r.key for r in plan.records]
    assert len(set(keys)) == 9112
    raw_leading_zero = sum(r[f.spec.number_field].startswith("0") for f in snapshot.datasets.values() for r in f.raw_rows)
    assert sum(k.split(":")[2].startswith("0") for k in keys) == raw_leading_zero > 0
    assert {r.product["product_category"] for r in plan.importable} == {
        "domestic_vitamin_tablet_capsule", "imported_tablet_capsule"}
    assert all(r.raw_data["registration"]["official_status"] is None for r in plan.importable)
