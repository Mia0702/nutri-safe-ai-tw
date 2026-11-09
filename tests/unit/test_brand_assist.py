"""D5：已審核品牌（人工 ACCEPT）輔助「候選」取回。

全部使用虛構 TEST_ONLY 資料與離線 CatalogRepo；overlay 以真正的 D4 建構流程寫入暫存目錄（審核者為虛構真人姓名）。
不連 OpenAI / Supabase / Neo4j，不讀真實快照；不依賴任何真實品牌或產品名稱。
"""

import json
from pathlib import Path

import pytest

from app import brand_assist as ba
from app.ingestion import brand_alias_review as bar
from app.ingestion import product_alias_review as par
from app.ingestion.product_alias_review import OfficialRecord
from app.schemas.enums import ProductMatchType
from app.schemas.evidence import REVIEWED_BRAND_CANDIDATE_MATCHED_BY
from app.tools.product_tools import lookup_product
from scripts import review_brand_candidates as cli
from scripts.eval.resolver_eval import AUTO_CONFIRM, CLARIFY, NOT_FOUND, CatalogEntry, CatalogRepo, EvalCase, resolve_case

SHA = "a" * 64
HUMAN = "Lin Wenhua"
ROOT = Path(__file__).resolve().parents[2]

APPLICANT_A = "TEST_ONLY品牌公司甲"
APPLICANT_B = "TEST_ONLY品牌公司乙"
APPLICANT_C = "TEST_ONLY品牌公司丙"
APPLICANT_D = "TEST_ONLY品牌公司丁"
APPLICANT_E = "TEST_ONLY品牌公司戊"


def rec(i, zh, en, applicant):
    return OfficialRecord(
        product_id=f"pid-{i:03d}", source_row_key=f"TFDA:23:T{i:04d}", permit_number=f"T{i:05d}", name_zh=zh, name_en=en,
        category="IMPORTED_TABLET_CAPSULE", applicant=applicant, source_id="SRC_IMPORTED_CSV", source_dataset_id="9047",
        source_url="https://data.fda.gov.tw/data/opendata/export/23/csv", retrieved_at="2026-10-04T00:00:00+00:00",
        raw_row_sha256="b" * 64, raw_row_number=str(i), dataset_sha256=SHA, source_name="TFDA TEST_ONLY 資料集")


def group(token, prefix, applicant, start, items):
    """items：(中文品名後段, 英文品名後段)；中文品名以 prefix 開頭、英文品名以 token 開頭。"""
    return [rec(start + i, f"{prefix}{zh}", f"{token} {en}", applicant) for i, (zh, en) in enumerate(items)]


ITEMS_A = [("葉酸錠", "FOLIC TABS"), ("鈣鎂錠", "CALMAG TABS"), ("魚油軟膠囊", "FISHOIL CAPS"),
           ("維他命丙錠", "VITC TABS"), ("鋅膠囊", "ZINC CAPS"), ("鐵質錠", "IRON TABS")]
ITEMS_B = [("深海魚油", "DEEPSEA OIL"), ("蔓越莓錠", "CRAN TABS"), ("益生菌粉", "PROBIO POWDER"),
           ("葡萄糖胺錠", "GLUCO TABS"), ("膠原蛋白飲", "COLLAGEN DRINK")]
ITEMS_C = [(f"{chr(0x6a21 + i * 7)}組合", f"SET{i} CAPS") for i in range(5)]
ITEMS_D = [(f"{chr(0x7a31 + i * 5)}錠", f"UNIT{i} TABS") for i in range(5)]
ITEMS_E = [(f"{chr(0x8a11 + i * 5)}錠", f"UNIT{i} TABS") for i in range(5)]

RECORDS = (group("Zorvax", "佐瓦", APPLICANT_A, 1, ITEMS_A)          # 會被 ACCEPT（多個產品）
           + group("Quilmor", "奎莫", APPLICANT_B, 20, ITEMS_B)       # 會被 ACCEPT
           + group("Nyxbrand", "尼克", APPLICANT_C, 40, ITEMS_C)      # NEEDS_REVIEW
           + group("Genword", "泛字", APPLICANT_D, 60, ITEMS_D)       # REJECT（泛用詞）
           # 沒有進 overlay 的詞：只有 2 個產品，機械規則不收（未審核）
           + group("Unlisted", "未列", APPLICANT_E, 80, [("甲錠", "ALPHA TABS"), ("乙錠", "BETA TABS")]))

# 官方英文名完全相同的多詞 exact（D3）：品牌輔助不得影響
RECORDS.append(rec(100, "佐瓦官方英文錠", "TEST_ONLY OFFICIAL ENGLISH FORMULA", APPLICANT_A))
RECORDS.append(rec(101, "佐瓦單詞英文錠", "SOLOONLYWORD", APPLICANT_A))


def decisions(**over):
    base = {"zorvax": "ACCEPT", "quilmor": "ACCEPT", "nyxbrand": "NEEDS_REVIEW", "genword": "REJECT"}
    base.update(over)
    out = {}
    for token, value in base.items():
        flag = "GENERIC_ENGLISH_WORD" if value == "REJECT" else "NONE"
        out[token] = {"alias_text_normalized": token, "decision": value, "descriptor_flag": flag,
                      "decision_reason": "TEST_ONLY: 理由"}
    return out


def make_overlay(tmp_path, *, reviewer=HUMAN, records=RECORDS, decs=None, name="overlay") -> Path:
    cand_path = tmp_path / f"{name}_brand_candidates.csv"
    par.write_review_csv(cand_path, par.build_brand_candidates(records, "1.0"), par.BRAND_EXTRA_COLUMNS)
    rows = bar.build_reviewed_brand_rows(par.read_review_csv(cand_path), records, decs or decisions(),
                                         overlay_version="1.1", reviewer=reviewer, reviewed_at="2026-11-08")
    out = tmp_path / name
    out.mkdir()
    files = {bar.REVIEWED_BRAND_FILE: {"sha256": par.write_review_csv(out / bar.REVIEWED_BRAND_FILE, rows,
                                                                        bar.BRAND_REVIEW_EXTRA_COLUMNS), "rows": len(rows)}}
    ds = par.SourceDataset("SRC_IMPORTED_CSV", "TEST", "9047", "23", "https://data.fda.gov.tw/x", "2026-10-04",
                           "raw.csv", SHA, "norm.csv", SHA, 1)
    manifest = cli.build_review_manifest(
        overlay_version="1.1", created_at="2026-11-08T00:00:00+00:00", datasets=[ds], files=files,
        inputs={"brand_candidates.csv": {"path": str(cand_path), "sha256": par.sha256_file(cand_path)}},
        summary=bar.summarize_reviewed_rows(rows))
    par.write_manifest(out / cli.MANIFEST_FILE, manifest)
    return out


CATALOG = [CatalogEntry(product_id=r.product_id, name=r.name_zh, permit_number=r.permit_number,
                        category="imported_tablet_capsule", company_name=r.applicant, name_en=r.name_en) for r in RECORDS]
REPO = CatalogRepo(CATALOG)


@pytest.fixture
def overlay(tmp_path):
    ov = ba.load_reviewed_brand_overlay(make_overlay(tmp_path))
    assert ov.status.active, ov.status.issues
    return ov


def run(query, expected=(), *, overlay=None, permit=False, repo=REPO):
    case = EvalCase(case_id=f"t:{query}", product_id="x", variant="t", query=query, expected_ids=frozenset(expected),
                    is_permit_query=permit)
    return resolve_case(repo, case, [], brand_overlay=overlay)


def pids(*indexes):
    return {f"pid-{i:03d}" for i in indexes}


A_MEMBERS = pids(1, 2, 3, 4, 5, 6)


# ---------------------------------------------------------------- overlay 載入：只有人工 ACCEPT 生效


def test_only_accept_rows_with_human_reviewer_become_runtime_brands(overlay):
    assert {b.key for b in overlay.brands} == {"zorvax", "quilmor"}   # NEEDS_REVIEW / REJECT 不在其中
    assert overlay.status.accepted_brands == 2 and overlay.status.ignored_rows == 2
    assert all(b.reviewer == HUMAN and b.applicant for b in overlay.brands)


@pytest.mark.parametrize("reviewer", ["CLAUDE_D4_REVIEW", "claude", "GPT-5 reviewer", "AI_REVIEWER", "mechanical", "TEST_REVIEWER", ""])
def test_ai_or_automated_reviewer_is_rejected_from_runtime_overlay(tmp_path, reviewer):
    ov = ba.load_reviewed_brand_overlay(make_overlay(tmp_path, reviewer=reviewer) if reviewer else _blank_reviewer(tmp_path))
    assert not ov.active and not ov.brands and any("reviewer" in i for i in ov.status.issues)
    assert ov.find_mention("ZORVAX") is None


def _blank_reviewer(tmp_path):
    out = make_overlay(tmp_path, name="blank")
    path = out / bar.REVIEWED_BRAND_FILE
    rows = par.read_review_csv(path)
    for r in rows:
        r["reviewer"] = ""
    manifest = json.loads((out / cli.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["files"][bar.REVIEWED_BRAND_FILE]["sha256"] = par.write_review_csv(path, rows, bar.BRAND_REVIEW_EXTRA_COLUMNS)
    par.write_manifest(out / cli.MANIFEST_FILE, manifest)
    return out


def test_human_reviewer_detection_does_not_flag_ordinary_names():
    assert ba.is_human_reviewer("Lin Wenhua") and ba.is_human_reviewer("Abbott Chen") and ba.is_human_reviewer("王小明")
    assert not any(ba.is_human_reviewer(x) for x in ("CLAUDE_D4_REVIEW", "gpt", "openai-bot", "AI", "", None))


def test_committed_d4_overlay_is_fail_closed_until_a_human_signs_off():
    d4 = ROOT / "data/product_alias_overlay/2026-11-08"
    if not (d4 / cli.MANIFEST_FILE).exists():
        pytest.skip("D4 dataset not present")
    ov = ba.load_reviewed_brand_overlay(d4)
    rows = par.read_review_csv(d4 / bar.REVIEWED_BRAND_FILE)
    if any(r["reviewer"].strip().upper().startswith("CLAUDE") for r in rows if r["decision"] == "ACCEPT"):
        assert not ov.active and ov.find_mention("BLACKMORES") is None   # 還沒有真人簽核：不得生效
    else:
        assert ov.active and ov.status.accepted_brands == sum(1 for r in rows if r["decision"] == "ACCEPT")


def test_invalid_manifest_or_hash_fails_closed(tmp_path):
    out = make_overlay(tmp_path)
    assert ba.load_reviewed_brand_overlay(out).active
    reviewed = out / bar.REVIEWED_BRAND_FILE
    original = reviewed.read_bytes()
    reviewed.write_bytes(original + b"\n")  # reviewed csv 被改
    ov = ba.load_reviewed_brand_overlay(out)
    assert not ov.active and any("MANIFEST_HASH_MISMATCH" in i for i in ov.status.issues)
    reviewed.write_bytes(original)
    assert ba.load_reviewed_brand_overlay(out).active
    cand = next(tmp_path.glob("overlay_brand_candidates.csv"))
    cand.write_bytes(cand.read_bytes() + b"\n")  # 輸入候選檔被改
    assert not ba.load_reviewed_brand_overlay(out).active
    (out / cli.MANIFEST_FILE).write_text("{ not json", encoding="utf-8")        # manifest 損毀
    assert not ba.load_reviewed_brand_overlay(out).active
    assert not ba.load_reviewed_brand_overlay(tmp_path / "missing").active
    assert not ba.load_reviewed_brand_overlay(None).active


@pytest.mark.parametrize("column,value", [("match_policy", "EXACT_REVIEW_REQUIRED"), ("reviewed_at", "not-a-date"),
                                          ("proposed_by", "AI_SUGGESTED"), ("source_url", ""), ("target_kind", "PRODUCT")])
def test_invalid_accepted_row_disables_the_whole_overlay_and_reports_it(tmp_path, column, value):
    out = make_overlay(tmp_path)
    path = out / bar.REVIEWED_BRAND_FILE
    rows = par.read_review_csv(path)
    next(r for r in rows if r["decision"] == "ACCEPT")[column] = value
    manifest = json.loads((out / cli.MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["files"][bar.REVIEWED_BRAND_FILE]["sha256"] = par.write_review_csv(path, rows, bar.BRAND_REVIEW_EXTRA_COLUMNS)
    par.write_manifest(out / cli.MANIFEST_FILE, manifest)         # manifest 雜湊一致，仍必須因為列內容無效而停用
    ov = ba.load_reviewed_brand_overlay(out)
    assert not ov.active and ov.status.issues


def test_overlay_cache_reloads_when_files_change(tmp_path):
    out = make_overlay(tmp_path)
    ba.clear_overlay_cache()
    first = ba.get_overlay(out)
    assert first.active and ba.get_overlay(out) is first            # 命中快取（同一個不可變物件）
    (out / bar.REVIEWED_BRAND_FILE).write_text("tampered", encoding="utf-8")
    assert not ba.get_overlay(out).active                          # 檔案指紋改變 → 重新驗證 → fail closed
    ba.clear_overlay_cache()


# ---------------------------------------------------------------- 品牌-only：候選，不自動確認


def test_accepted_brand_only_returns_candidates_and_never_auto_confirms(overlay):
    r = run("ZORVAX", overlay=overlay)
    assert r.outcome == CLARIFY and r.confirmed_id is None
    assert r.match_type == str(ProductMatchType.BRAND_CANDIDATE_ONLY)
    assert set(r.candidate_ids) <= A_MEMBERS | pids(100, 101) and len(r.candidate_ids) == 5      # resolver 顯示上限
    # 同一個品牌的任何大小寫 / 標點變體都一樣
    for text in ("zorvax", " Zorvax ", "ZORVAX."):
        assert run(text, overlay=overlay).outcome == CLARIFY


def test_brand_only_with_one_matching_product_still_requires_confirmation(tmp_path):
    recs = [r for r in RECORDS if r.applicant != APPLICANT_A] + group("Zorvax", "佐瓦", APPLICANT_A, 1, ITEMS_A)[:5]
    only_one = [r for r in recs if r.applicant != APPLICANT_B] + group("Quilmor", "奎莫", APPLICANT_B, 20, ITEMS_B)
    ov = ba.load_reviewed_brand_overlay(make_overlay(tmp_path, records=only_one))
    catalog = [CatalogEntry(r.product_id, r.name_zh, r.permit_number, "imported_tablet_capsule", r.applicant, name_en=r.name_en)
               for r in only_one]
    # 品牌 + 只有一項產品支持的文字 → 單一候選，仍然只是「請確認是不是這一項」
    r = run("QUILMOR 益生菌", overlay=ov, repo=CatalogRepo(catalog))
    assert r.outcome == CLARIFY and r.confirmed_id is None and r.candidate_ids == ("pid-022",)
    assert r.match_type == str(ProductMatchType.BRAND_CANDIDATE_ONLY)


def test_accepted_brand_with_many_products_asks_for_clarification_without_a_winner(overlay):
    out = lookup_product(REPO, ["ZORVAX"], brand_overlay=overlay)
    brand = [m for m in out.candidates if m.matched_by == REVIEWED_BRAND_CANDIDATE_MATCHED_BY]
    assert {m.product.product_id for m in brand} == A_MEMBERS | pids(100, 101) and all(m.match_score < 0.5 for m in brand)
    r = run("ZORVAX", overlay=overlay)
    assert r.outcome == CLARIFY and r.confirmed_id is None and len(r.candidate_ids) > 1


# ---------------------------------------------------------------- 品牌 + 產品文字


def test_brand_plus_unique_product_text_offers_the_right_product_without_confirming(overlay):
    for query, expected in (("ZORVAX FOLIC", "pid-001"), ("zorvax 魚油", "pid-003"), ("QUILMOR 益生菌", "pid-022"),
                            ("Quilmor glucoxabs", None)):
        r = run(query, {expected} if expected else (), overlay=overlay)
        if expected is None:
            assert r.outcome == NOT_FOUND            # 沒有成員支持剩餘文字 → 不捏造候選，維持既有行為
            continue
        assert r.outcome == CLARIFY and r.confirmed_id is None, query
        assert r.candidate_ids == (expected,), query
        assert r.match_type == str(ProductMatchType.BRAND_CANDIDATE_ONLY)


def test_remainder_matching_ignores_spacing_and_punctuation_only(overlay):
    assert run("QUILMOR glucotabs", {"pid-023"}, overlay=overlay).candidate_ids == ("pid-023",)   # "GLUCO TABS"
    assert run("ZORVAX folic & ", {"pid-001"}, overlay=overlay).candidate_ids == ("pid-001",)
    assert run("ZORVAX &", overlay=overlay).outcome == NOT_FOUND                                # 只剩標點：不輔助


def test_brand_plus_text_matching_several_members_is_a_clarification(overlay):
    r = run("ZORVAX TABS", overlay=overlay)            # 英文名後段 TABS 同時出現在多個成員
    assert r.outcome == CLARIFY and r.confirmed_id is None
    assert len(r.candidate_ids) >= 2 and set(r.candidate_ids) <= A_MEMBERS


def test_brand_never_raises_identity_confidence_for_existing_exact_paths(overlay):
    """只要既有路徑已有結果（精確 / 相似名稱…），帶不帶 overlay 的 outcome、候選、match_type 必須完全相同。"""
    queries = ["佐瓦葉酸錠", "佐瓦鈣鎂錠", "佐瓦", "TEST_ONLY OFFICIAL ENGLISH FORMULA", "SOLOONLYWORD", "佐瓦葉酸",
               "奎莫深海魚油", "未列甲錠", "不存在的名稱XYZ"]
    for q in queries:
        base, with_brand = run(q), run(q, overlay=overlay)
        assert (with_brand.outcome, with_brand.confirmed_id, with_brand.match_type, with_brand.candidate_ids) == \
               (base.outcome, base.confirmed_id, base.match_type, base.candidate_ids), q


def test_brand_plus_full_chinese_official_name_is_offered_but_not_confirmed_by_the_brand(overlay):
    """品牌 + 完整中文官方品名：既有規則（FC-B7）不確認「品牌＋品名」混合寫法；品牌也不能讓它變成確認，只會列出候選。"""
    r = run("ZORVAX 佐瓦葉酸錠", {"pid-001"}, overlay=overlay)
    assert r.outcome == CLARIFY and r.confirmed_id is None and r.candidate_ids == ("pid-001",)


def test_brand_relation_alone_is_never_an_auto_confirm(overlay):
    queries = [f"{b.token} {w}" for b in overlay.brands for w in ("", "TABS", "CAPS", "錠", "膠囊", "FOLIC", "益生菌")]
    for q in queries:
        assert run(q, overlay=overlay).outcome != AUTO_CONFIRM, q


# ---------------------------------------------------------------- REJECT / NEEDS_REVIEW / 未審核：與 D5 之前完全相同


@pytest.mark.parametrize("query", ["NYXBRAND", "nyxbrand 組合", "NYXBRAND SET1", "GENWORD", "Genword UNIT1 TABS",
                                   "UNLISTED", "UNLISTED ALPHA TABS", "未知牌 TABS"])
def test_rejected_needs_review_and_unreviewed_brands_behave_exactly_like_before(overlay, query):
    base, with_brand = run(query), run(query, overlay=overlay)
    assert (with_brand.outcome, with_brand.confirmed_id, with_brand.match_type, with_brand.candidate_ids) == \
           (base.outcome, base.confirmed_id, base.match_type, base.candidate_ids)
    assert with_brand.match_type != str(ProductMatchType.BRAND_CANDIDATE_ONLY)
    assert overlay.find_mention(query) is None


def test_two_reviewed_brands_in_one_query_do_not_assist(overlay):
    assert overlay.find_mention("ZORVAX QUILMOR") is None
    base, with_brand = run("ZORVAX QUILMOR"), run("ZORVAX QUILMOR", overlay=overlay)
    assert (base.outcome, base.candidate_ids, base.match_type) == (with_brand.outcome, with_brand.candidate_ids, with_brand.match_type)


def test_brand_token_must_match_on_a_word_boundary(overlay):
    for text in ("ZORVAXY", "XZORVAX", "ZORVAX2", "NOTZORVAX"):
        assert overlay.find_mention(text) is None, text
    m = overlay.find_mention("zorvax-folic")
    assert m and m.brand.key == "zorvax" and m.remainder == "folic"
    assert overlay.find_mention("ZORVAX").brand_only


# ---------------------------------------------------------------- 無結果與不可用 → 既有行為


def test_no_valid_brand_candidate_falls_back_to_the_existing_result(overlay):
    for q in ("ZORVAX 不存在的剩餘文字", "QUILMOR 完全無關"):
        base, with_brand = run(q), run(q, overlay=overlay)
        assert with_brand.outcome == base.outcome == NOT_FOUND and with_brand.candidate_ids == base.candidate_ids == ()


def test_incomplete_candidate_set_or_unsupported_repo_never_assists(overlay):
    class Incomplete(CatalogRepo):
        def search_products_by_applicant(self, name, limit):
            return super().search_products_by_applicant(name, limit)[0], False

    class NoSupport:
        def __init__(self, repo):
            self._repo = repo

        def __getattr__(self, name):
            if name == "search_products_by_applicant":
                raise AttributeError(name)
            return getattr(self._repo, name)

    assert run("ZORVAX", overlay=overlay, repo=Incomplete(CATALOG)).outcome == NOT_FOUND
    assert run("ZORVAX", overlay=overlay, repo=NoSupport(REPO)).outcome == NOT_FOUND
    assert all(m.matched_by != REVIEWED_BRAND_CANDIDATE_MATCHED_BY
               for m in lookup_product(REPO, ["ZORVAX"], brand_overlay=None).candidates)


def test_applicant_mismatch_or_unofficial_rows_are_not_brand_members(overlay):
    brand = overlay.find_mention("ZORVAX").brand
    row = CATALOG[0].to_row()
    assert ba.is_brand_member(brand, row)
    assert not ba.is_brand_member(brand, {**row, "company_name": "別家公司"})
    assert not ba.is_brand_member(brand, {**row, "is_official": False})
    assert not ba.is_brand_member(brand, {**row, "product_name_zh": "別的名稱", "product_name_en": "OTHER THING"})
    assert ba.is_brand_member(brand, {**row, "product_name_zh": "別的名稱", "product_name_en": "ZORVAX OTHER"})


def test_resolver_works_normally_without_a_brand_overlay():
    """沒有 overlay（預設關閉 / None / 停用）時，所有既有路徑照常運作。"""
    assert run("佐瓦葉酸錠", pids(1)).outcome == AUTO_CONFIRM
    assert run("TEST_ONLY OFFICIAL ENGLISH FORMULA", pids(100)).outcome == AUTO_CONFIRM
    assert run("ZORVAX").match_type != str(ProductMatchType.BRAND_CANDIDATE_ONLY)
    disabled = ba.load_reviewed_brand_overlay(None)
    assert run("ZORVAX", overlay=disabled).candidate_ids == run("ZORVAX").candidate_ids
    # 預設設定（BRAND_ASSIST_OVERLAY_DIR 未設定）= 關閉
    from app.config.settings import Settings
    assert Settings(_env_file=None).brand_assist_overlay_dir is None


# ---------------------------------------------------------------- 既有 D3 / 中文 / 字號行為不變


def test_official_english_d3_behavior_is_unchanged(overlay):
    for query, expected, outcome in (("TEST_ONLY OFFICIAL ENGLISH FORMULA", pids(100), AUTO_CONFIRM),
                                     ("SOLOONLYWORD", pids(101), CLARIFY)):
        base, with_brand = run(query, expected), run(query, expected, overlay=overlay)
        assert base.outcome == with_brand.outcome == outcome
        assert (base.confirmed_id, base.match_type, base.candidate_ids) == (with_brand.confirmed_id, with_brand.match_type,
                                                                           with_brand.candidate_ids)
    # 單詞官方英文名唯一 exact：仍只是單一候選，品牌不能把它升級
    assert run("SOLOONLYWORD", pids(101), overlay=overlay).match_type == str(ProductMatchType.OFFICIAL_ENGLISH_NAME_NEEDS_CONFIRMATION)


def test_chinese_and_permit_behavior_is_unchanged(overlay):
    for entry in CATALOG:
        assert run(entry.name, {entry.product_id}, overlay=overlay).outcome == AUTO_CONFIRM
    permit = CATALOG[0].permit_number
    a, b = run(permit, {"pid-001"}, permit=True), run(permit, {"pid-001"}, permit=True, overlay=overlay)
    assert (a.outcome, a.confirmed_id) == (b.outcome, b.confirmed_id) == (AUTO_CONFIRM, "pid-001")


# ---------------------------------------------------------------- 沒有所有權、沒有網路 / 資料庫往返、deterministic


def test_brand_assist_never_expresses_ownership(overlay):
    source = (ROOT / "app/brand_assist.py").read_text(encoding="utf-8")
    assert "OWNS_BRAND" not in source.replace("沒有 OWNS_BRAND", "") and "neo4j" not in source.lower()
    r = lookup_product(REPO, ["ZORVAX"], brand_overlay=overlay)
    assert all(m.matched_by == REVIEWED_BRAND_CANDIDATE_MATCHED_BY for m in r.candidates)
    from app.risk.assessment_rules import BRAND_CANDIDATE_NOTE
    assert "不代表品牌所有權" in BRAND_CANDIDATE_NOTE and "確認是哪一項產品" in BRAND_CANDIDATE_NOTE


def test_brand_lookup_costs_at_most_one_applicant_query_and_is_deterministic(overlay):
    calls = []

    class Counting(CatalogRepo):
        def search_products_by_applicant(self, name, limit):
            calls.append(name)
            return super().search_products_by_applicant(name, limit)

    repo = Counting(CATALOG)
    a = [m.product.product_id for m in lookup_product(repo, ["ZORVAX"], brand_overlay=overlay).candidates]
    assert calls == [APPLICANT_A]
    b = [m.product.product_id for m in lookup_product(repo, ["ZORVAX"], brand_overlay=overlay).candidates]
    assert a == b
    calls.clear()
    lookup_product(repo, ["完全沒有品牌詞的查詢"], brand_overlay=overlay)
    assert calls == []                                              # 沒有品牌詞 → 不查資料庫


def test_brand_names_from_agent_a_are_combined_with_product_names(overlay):
    out = lookup_product(REPO, ["魚油"], brand_names=["ZORVAX"], brand_overlay=overlay)
    brand = [m for m in out.candidates if m.reviewed_brand_candidate]
    assert [m.product.product_id for m in brand] == ["pid-003"]


# ---------------------------------------------------------------- D5.2：最後截斷時保留品牌候選

FISH_ITEMS = [("魚油軟膠囊", "FISH CAPS A"), ("魚油錠", "FISH TABS B"), ("魚油粉", "FISH POWDER C"),
              ("鈣鎂錠", "CALMAG"), ("鋅膠囊", "ZINC"), ("鐵質錠", "IRON")]
FISH_RECORDS = group("Zorvax", "佐瓦", APPLICANT_A, 1, FISH_ITEMS)
FILLERS = [CatalogEntry(product_id=f"fill-{i:03d}", name=f"魚油ZORVAX補充品{i:02d}", permit_number=None,
                        category="imported_tablet_capsule", company_name="TEST_ONLY填充公司", name_en=None) for i in range(30)]
FISH_CATALOG = [CatalogEntry(product_id=r.product_id, name=r.name_zh, permit_number=r.permit_number,
                             category="imported_tablet_capsule", company_name=r.applicant, name_en=r.name_en)
                for r in FISH_RECORDS] + FILLERS


class CrowdedRepo(CatalogRepo):
    """一般搜尋先回傳 30 筆分數較高的填充列：品牌候選（分數 < 0.5）會被 20 筆上限擠掉，除非保留規則生效。"""

    def search_products(self, terms, limit):
        return [e.to_row() for e in FILLERS[:limit]]


@pytest.fixture
def fish_overlay(tmp_path):
    ov = ba.load_reviewed_brand_overlay(make_overlay(tmp_path, records=FISH_RECORDS, decs={"zorvax": decisions()["zorvax"]}))
    assert ov.active, ov.status.issues
    return ov


def _ids(out):
    return [m.product.product_id for m in out.candidates]


def test_brand_candidates_beyond_top_20_are_retained_without_displacing_normal_results(fish_overlay):
    repo = CrowdedRepo(FISH_CATALOG)
    base = lookup_product(repo, ["ZORVAX 魚油"], brand_overlay=None)
    out = lookup_product(repo, ["ZORVAX 魚油"], brand_overlay=fish_overlay)
    ids = _ids(out)
    assert {"pid-001", "pid-002", "pid-003"} <= set(ids)                 # 多個有效品牌候選都保留
    assert len(ids) == len(set(ids)) == 23                             # 不重複；有界（<= limit + MAX_BRAND_CANDIDATES）
    normal = [m for m in out.candidates if m.matched_by != REVIEWED_BRAND_CANDIDATE_MATCHED_BY]
    assert [m.model_dump() for m in normal] == [m.model_dump() for m in base.candidates]   # 一般結果與 D5 前逐項相同
    brand = [m for m in out.candidates if m.matched_by == REVIEWED_BRAND_CANDIDATE_MATCHED_BY]
    assert {m.product.product_id for m in brand} == {"pid-001", "pid-002", "pid-003"}
    assert all(m.reviewed_brand_candidate and m.match_score < 0.5 for m in brand)                  # 仍是候選
    scores = [m.match_score for m in out.candidates]
    assert scores == sorted(scores, reverse=True)


def test_without_retention_the_brand_candidates_would_be_lost(fish_overlay):
    base = lookup_product(CrowdedRepo(FISH_CATALOG), ["ZORVAX 魚油"], brand_overlay=None)
    assert not {"pid-001", "pid-002", "pid-003"} & set(_ids(base))


def test_single_brand_candidate_beyond_top_20_is_retained(fish_overlay):
    out = lookup_product(CrowdedRepo(FISH_CATALOG), ["ZORVAX 魚油軟膠囊"], brand_overlay=fish_overlay)
    assert "pid-001" in _ids(out) and len(out.candidates) <= 20 + ba.MAX_BRAND_CANDIDATES


def test_truncation_is_bounded_deduplicated_and_unchanged_without_brand_candidates():
    from app.schemas.evidence import ProductCandidateMatch
    from app.tools.product_tools import _product_from_row, _truncate_keeping_brand_candidates as trunc

    def m(i, score, brand=False, by="product_name"):
        row = {**FILLERS[0].to_row(), "product_id": f"x-{i:03d}", "product_name_zh": f"名{i}"}
        return ProductCandidateMatch(product=_product_from_row(row), match_score=score, matched_by=by, query="q",
                                     reviewed_brand_candidate=brand)

    plain = [m(i, 1 - i / 100) for i in range(40)]
    assert trunc(plain, 20) == plain[:20]                                # 沒有品牌候選：與原行為完全相同
    mixed = [m(i, 1 - i / 100, brand=i >= 25) for i in range(40)]       # 15 筆品牌候選都在第 25 名之後
    out = trunc(mixed, 20)
    assert [c.product.product_id for c in out if c.matched_by == "product_name"] == [f"x-{i:03d}" for i in range(20)]
    assert len(out) == 35 and len({c.product.product_id for c in out}) == 35
    assert all(c.match_score <= 0.49 and c.matched_by == REVIEWED_BRAND_CANDIDATE_MATCHED_BY for c in out[20:])
    many = [m(i, 0.3, brand=True, by=REVIEWED_BRAND_CANDIDATE_MATCHED_BY) for i in range(40)]
    assert len(trunc(many, 20)) == ba.MAX_BRAND_CANDIDATES               # 品牌候選受 MAX_BRAND_CANDIDATES 限制
    assert trunc([], 20) == []


def test_retained_brand_candidates_never_become_confirmed(fish_overlay):
    repo = CrowdedRepo(FISH_CATALOG)
    for q in ("ZORVAX 魚油", "ZORVAX 魚油軟膠囊", "ZORVAX FISH CAPS"):
        assert run(q, overlay=fish_overlay, repo=repo).outcome != AUTO_CONFIRM, q


def test_existing_valid_name_similarity_result_is_not_merged_with_brand_candidates(overlay):
    """已知限制（有意）：既有邏輯已產生有效的名稱相似候選時，品牌候選不併入，結果與沒有 overlay 時完全相同。"""
    base = run("佐瓦葉酸")
    assert base.match_type == str(ProductMatchType.NAME_SIMILARITY) and base.candidate_ids
    with_brand = run("佐瓦葉酸", overlay=overlay)
    assert (with_brand.outcome, with_brand.match_type, with_brand.candidate_ids) == (base.outcome, base.match_type, base.candidate_ids)
