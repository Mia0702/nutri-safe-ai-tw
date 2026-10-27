"""D3：官方英文品名（product_name_en）exact 取回與身份安全規則。

虛構 TEST_ONLY 資料；離線 CatalogRepo（與 Supabase repo 相同的縮小範圍與 key 比對）；不連 OpenAI / Supabase / Neo4j。
"""

import ast
from pathlib import Path

from app import english_name as en
from app.ingestion.product_alias_review import normalize_alias_text
from app.schemas.enums import ProductMatchType
from app.services.supabase_service import SupabaseOfficialDataRepository
from app.tools.product_tools import lookup_official_english_name
from scripts.eval.resolver_eval import AUTO_CONFIRM, CLARIFY, NOT_FOUND, CatalogEntry, CatalogRepo, EvalCase, resolve_case

ROOT = Path(__file__).resolve().parents[2]


def entry(i: int, zh: str, name_en: str | None, permit: str | None = None) -> CatalogEntry:
    return CatalogEntry(product_id=f"pid-{i:03d}", name=zh, permit_number=permit, category="imported_tablet_capsule",
                        company_name=f"TEST_ONLY公司{i}", name_en=name_en)


CATALOG = [
    entry(1, "測試甲多詞錠", "TEST_ONLY ALPHA BETA TABS"),
    entry(2, "測試乙單詞錠", "TESTSOLO"),
    entry(3, "測試丙同名錠", "TEST_ONLY TWIN PAIR CAPS"),
    entry(4, "測試丁同名膠囊", "TEST_ONLY TWIN PAIR CAPS"),
    entry(5, "測試戊標點錠", "TEST_ONLY DELTA-X PLUS"),
    entry(6, "測試己空白錠", "TEST_ONLY DELTA X PLUS"),
    entry(7, "測試庚品牌一號", "ZORBEXBRAND ONE TABS"),
    entry(8, "測試辛品牌二號", "ZORBEXBRAND TWO CAPS"),
    entry(9, "測試壬連字號錠", "TEST-ONLY-HYPHENATED"),
    entry(10, "測試癸字號錠", "TEST_ONLY PERMIT ITEM", permit="衛署藥製字第123456號"),
    entry(11, "測試子成分類錠", "TESTGENERIC EXTRACT CAPS"),
]
REPO = CatalogRepo(CATALOG)


def run(query: str, expected: set[str] | None = None, *, permit: bool = False):
    case = EvalCase(case_id=f"t:{query}", product_id="x", variant="t", query=query,
                    expected_ids=frozenset(expected or ()), is_permit_query=permit)
    return resolve_case(REPO, case, [])


def test_multi_token_unique_exact_auto_confirms_the_correct_product():
    r = run("TEST_ONLY ALPHA BETA TABS", {"pid-001"})
    assert (r.outcome, r.confirmed_id, r.correct) == (AUTO_CONFIRM, "pid-001", True)


def test_single_token_unique_exact_requires_confirmation_and_offers_the_product():
    r = run("TESTSOLO", {"pid-002"})
    assert r.outcome == CLARIFY and r.confirmed_id is None
    assert r.match_type == str(ProductMatchType.OFFICIAL_ENGLISH_NAME_NEEDS_CONFIRMATION)
    assert r.candidate_ids == ("pid-002",)  # 單一候選；不是 NOT_FOUND


def test_hyphenated_name_is_one_lexical_token_not_split_by_punctuation():
    assert en.is_single_token("TEST-ONLY-HYPHENATED") and en.is_single_token("B-Complex+Iron")
    assert not en.is_single_token("TEST ONLY") and not en.is_single_token("A　B")  # NFKC：全形空白也是分詞
    assert run("TEST-ONLY-HYPHENATED", {"pid-009"}).outcome == CLARIFY


def test_colliding_official_english_name_asks_the_user_and_never_picks_one():
    r = run("TEST_ONLY TWIN PAIR CAPS", {"pid-003", "pid-004"})
    assert r.outcome == CLARIFY and set(r.candidate_ids) == {"pid-003", "pid-004"} and r.confirmed_id is None


def test_punctuation_runtime_key_collision_asks_the_user():
    for query in ("TEST_ONLY DELTA X PLUS", "TEST_ONLY DELTA-X PLUS"):
        r = run(query, {"pid-005", "pid-006"})
        assert r.outcome == CLARIFY and set(r.candidate_ids) == {"pid-005", "pid-006"}, query


def test_case_only_difference_is_still_exact():
    r = run("test_only alpha beta tabs", {"pid-001"})
    assert (r.outcome, r.confirmed_id) == (AUTO_CONFIRM, "pid-001")


def test_whitespace_differences_are_normalized_but_not_removed():
    assert run("  TEST_ONLY   ALPHA \t BETA  TABS ", {"pid-001"}).confirmed_id == "pid-001"
    # 空白被移除（只剩 runtime key 相同）不是 exact：不取回
    assert lookup_official_english_name(REPO, "TEST_ONLYALPHABETATABS") == []


def test_partial_english_name_does_not_auto_confirm():
    for query in ("TEST_ONLY ALPHA BETA", "ALPHA BETA TABS", "TEST_ONLY ALPHA"):
        assert run(query, {"pid-001"}).outcome != AUTO_CONFIRM, query


def test_english_brand_only_does_not_auto_confirm():
    r = run("ZORBEXBRAND")
    assert r.outcome != AUTO_CONFIRM and r.confirmed_id is None


def test_generic_english_token_does_not_auto_confirm():
    for query in ("TESTGENERIC", "EXTRACT", "CAPS", "TABS"):
        assert run(query).outcome != AUTO_CONFIRM, query
    assert run("TESTGENERIC EXTRACT CAPS", {"pid-011"}).outcome == AUTO_CONFIRM  # 完整多詞官方英文名才是 exact


def test_chinese_exact_name_behavior_is_unchanged_even_when_an_english_name_exists():
    r = run("測試甲多詞錠", {"pid-001"})
    assert (r.outcome, r.confirmed_id, r.match_type) == (AUTO_CONFIRM, "pid-001", str(ProductMatchType.CANONICAL_EXACT))
    # 單詞英文品名的產品，用中文官方品名仍直接確認
    assert run("測試乙單詞錠", {"pid-002"}).outcome == AUTO_CONFIRM


def test_permit_behavior_is_unchanged():
    r = run("衛署藥製字第123456號", {"pid-010"}, permit=True)
    assert (r.outcome, r.confirmed_id) == (AUTO_CONFIRM, "pid-010")


def test_unusable_queries_skip_english_retrieval():
    for query in ("N/A", "VC", "測試甲", "", "12345678"):
        assert lookup_official_english_name(REPO, query) == []
    assert run("NOT A REAL NAME ANYWHERE").outcome == NOT_FOUND


def test_incomplete_narrowing_or_non_official_rows_never_confirm():
    class Incomplete:
        def search_products_by_english_name(self, query):
            return [CATALOG[0].to_row()], False

    assert lookup_official_english_name(Incomplete(), "TEST_ONLY ALPHA BETA TABS") == []

    class NotOfficial:
        def search_products_by_english_name(self, query):
            return [{**CATALOG[0].to_row(), "is_official": False}], True

    assert lookup_official_english_name(NotOfficial(), "TEST_ONLY ALPHA BETA TABS") == []

    class NoSupport:  # 沒有英文取回的 repo：行為與 D3 之前相同
        pass

    assert lookup_official_english_name(NoSupport(), "TEST_ONLY ALPHA BETA TABS") == []


def test_conservative_key_matches_the_d2_review_dataset_normalization():
    for text in ("  Ab　Cd  Ⅱ ", "ABC  def", "Ｆｕｌｌ Ｗｉｄｔｈ", "", None, "Straße"):
        assert en.conservative_key(text) == normalize_alias_text(text)


def test_narrowing_never_misses_runtime_key_or_compatibility_character_partners():
    names = ["TEST_ONLY DELTA X PLUS", "TEST_ONLY DELTA-X PLUS", "TEST_ONLY GRADE Ⅱ CAPS", "TEST_ONLY GRADE II CAPS"]
    rows = [{"product_id": f"p{i}", "product_name_en": n} for i, n in enumerate(names)]

    def in_scope(pattern, name):
        text, pos = name.lower(), 0
        for part in pattern.strip("%").split("%"):
            found = text.find(part, pos)
            if found < 0:
                return bool(__import__("re").search(en.NON_ASCII_REGEX, name))
            pos = found + len(part)
        return True

    for query, want in (("TEST_ONLY DELTA X PLUS", {"p0", "p1"}), ("TEST_ONLY GRADE II CAPS", {"p2", "p3"})):
        narrow = [r for r in rows if in_scope(en.narrowing_pattern(query), r["product_name_en"])]
        assert set(en.match_english_name(query, narrow).collision_ids) == want


def test_supabase_repo_reports_incomplete_when_narrowing_exceeds_the_limit():
    class Builder:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *a):
            return self

        def or_(self, *a):
            return self

        def order(self, *a):
            return self

        def limit(self, n):
            return self

        def in_(self, *a):
            return self

    class Db:
        def __init__(self, narrow):
            self.narrow = narrow

        def table(self, name):
            return Builder(self.narrow)

        def select(self, builder):
            return builder.rows

    over = [{"product_id": str(i), "product_name_en": "x"} for i in range(en.NARROW_LIMIT + 1)]
    repo = SupabaseOfficialDataRepository(Db(over))
    assert repo.search_products_by_english_name("TEST_ONLY ALPHA BETA TABS") == ([], False)
    assert repo.search_products_by_english_name("VC") == ([], False)  # 太短：無法縮小範圍
    assert SupabaseOfficialDataRepository(Db([])).search_products_by_english_name("TEST_ONLY ALPHA BETA TABS") == ([], True)


def test_runtime_rule_contains_no_hard_coded_vocabulary():
    """規則只依結構（空白、字母數、key 相等）；runtime 英文名稱模組不得有詞表 / 產品 / 品牌 / 成分字串。"""
    tree = ast.parse((ROOT / "app" / "english_name.py").read_text(encoding="utf-8"))
    allowed_null_markers = {"", "null", "none", "n/a", "na", "-", "NFKC"}  # NFKC = unicodedata 的正規化形式參數
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    docstrings = {ast.get_docstring(n) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))}
    words = {s for s in strings - docstrings if s.isalpha() and len(s) > 2} - allowed_null_markers
    assert words == set(), words


def test_bare_name_routing_single_token_english_name_is_not_a_confirmed_product_identity():
    """FC-F4 稽核：單一詞英文名只是待確認候選，不會被當成「整句等於官方產品名稱」的身份碰撞；
    多詞唯一官方英文名與中文官方品名一致（視為官方產品名稱）。"""
    from app.services.bare_name_routing import _is_exact_official_product

    assert _is_exact_official_product(REPO, "TESTSOLO") is False
    assert _is_exact_official_product(REPO, "TEST_ONLY ALPHA BETA TABS") is True
    assert _is_exact_official_product(REPO, "測試乙單詞錠") is True  # 中文行為不變
