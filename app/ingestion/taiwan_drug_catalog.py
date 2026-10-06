"""Taiwan Official Drug Ingredient Catalog（additive data layer；不是 Interaction evidence）。

「台灣未註銷藥品許可證中，依涵蓋藥證數排序的高頻主成分」——不是「最常被開立」「最多人使用」或「前 N 大藥品」。

來源（只用台灣 TFDA 官方開放資料）：
* 9123 未註銷藥品許可證資料集 → canonical 的「目前有效藥證」範圍，也是 `主成分略述` 的來源
* 9121 藥品詳細處方成分資料集 → 以成分代碼（ingredient_code）作為 canonical identity
* 9122 全部藥品許可證資料集 → 只用來交叉檢查（註銷狀態）；9123 內但 9122 標示已註銷 / 已廢止的藥證一律排除

主成分判定（依實際下載欄位稽核，見 `ACTIVE_INGREDIENT_RULE`）：9121 沒有「主成分 / 賦形劑」的明確欄位
（處方標示是自由文字，只有零星字樣提到賦形劑），因此不從成分名稱或處方標示推測；
改用 9123 官方的「主成分略述」作為主成分來源，並且只在「同一張藥證的詳細成分列中有完全相同名稱」時才取其成分代碼。

identity：成分代碼優先；沒有代碼才用 normalized_exact_name。鹽類 / 形式不合併（METFORMIN 與 METFORMIN HCL 是不同 identity）；
不使用任何 AI 翻譯。與既有「已審核 Interaction DrugSubstance」只做「正規化後完全相同」的對應；鹽類 / 形式變體只列出候選、不自動對應。
"""

import csv
import io
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

SOURCE_ID = "tfda_drug_ingredient_catalog"
DATASETS = {
    "permits_current": {"gov_dataset_id": 9123, "title": "未註銷藥品許可證資料集", "export_url": "https://data.fda.gov.tw/data/opendata/export/37/csv"},
    "detail": {"gov_dataset_id": 9121, "title": "藥品詳細處方成分資料集", "export_url": "https://data.fda.gov.tw/data/opendata/export/43/csv"},
    "permits_all": {"gov_dataset_id": 9122, "title": "全部藥品許可證資料集", "export_url": "https://data.fda.gov.tw/data/opendata/export/36/csv"},
}
COUNT_DEFINITION = "台灣未註銷藥品許可證中，依涵蓋藥證數（COUNT DISTINCT 許可證字號）排序的高頻主成分"
ACTIVE_INGREDIENT_RULE = (
    "9121 沒有明確的主成分 / 賦形劑欄位（欄位：許可證字號、處方標示、成分名稱、成分代碼、含量描述、含量、含量單位；處方標示是自由文字）→ "
    "不推測；以 9123 的「主成分略述」為主成分來源，只在同一張藥證的詳細成分列有完全相同名稱時取其成分代碼"
)
STATUS_NOT_MAPPED = "NOT_MAPPED"
STATUS_REVIEWED_EXACT = "REVIEWED_EXACT_MAP"
MAPPING_BASIS_EXACT = "EXACT_NAME_EQUALS_REVIEWED_CANONICAL"
TOP_N_DEFAULT = 1000
SELECTION_DESCRIPTION = "約 1000 個高頻主成分，並保留第 1000 名門檻的所有並列項目"
_ITEM_SPLIT = re.compile(r";;")


def normalize_exact_name(text: str | None) -> str:
    """只做格式正規化（NFKC、空白折疊、大小寫）；不去鹽類、不去括號、不翻譯。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip().casefold()


def parse_csv(raw: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def main_ingredient_items(summary: str | None) -> list[str]:
    return [item.strip() for item in _ITEM_SPLIT.split(summary or "") if item.strip()]


@dataclass
class CatalogBuild:
    audit: dict[str, Any] = field(default_factory=dict)
    ingredients: list[dict[str, Any]] = field(default_factory=list)
    permit_rows: list[tuple[str, str]] = field(default_factory=list)  # (ingredient_code or key, permit_number)
    mappings: list[dict[str, Any]] = field(default_factory=list)
    variant_candidates: list[dict[str, Any]] = field(default_factory=list)  # 鹽類 / 形式變體：只列出，不對應
    ambiguous_mappings: list[dict[str, Any]] = field(default_factory=list)


def build_catalog(current_rows: Iterable[dict[str, str]], detail_rows: Iterable[dict[str, str]],
                  all_rows: Iterable[dict[str, str]], *, top_n: int = TOP_N_DEFAULT,
                  reviewed_drugs: Iterable[dict[str, Any]] = ()) -> CatalogBuild:
    current_rows, detail_rows, all_rows = list(current_rows), list(detail_rows), list(all_rows)
    status_all = {r["許可證字號"]: (r.get("註銷狀態") or "").strip() for r in all_rows}
    current = {r["許可證字號"] for r in current_rows}
    cancelled_conflict = {p for p in current if status_all.get(p)}  # 9123 內，但 9122 標示已註銷 / 已廢止
    universe = current - cancelled_conflict

    detail_by_permit: dict[str, list[dict[str, str]]] = defaultdict(list)
    name_codes: dict[str, set[str]] = defaultdict(set)
    for r in detail_rows:
        detail_by_permit[r["許可證字號"]].append(r)
        code = (r.get("成分代碼") or "").strip()
        if code:
            name_codes[normalize_exact_name(r["成分名稱"])].add(code)

    permits_of: dict[str, set[str]] = defaultdict(set)
    raw_names: dict[str, Counter] = defaultdict(Counter)
    join = Counter()
    for r in current_rows:
        permit = r["許可證字號"]
        if permit not in universe:
            continue
        seen: set[str] = set()
        for item in main_ingredient_items(r.get("主成分略述")):
            key_name = normalize_exact_name(item)
            row_codes = {(d.get("成分代碼") or "").strip() for d in detail_by_permit.get(permit, [])
                         if normalize_exact_name(d["成分名稱"]) == key_name and (d.get("成分代碼") or "").strip()}
            if len(row_codes) == 1:
                ident, how = "code:" + next(iter(row_codes)), "permit_row_exact_name"
            elif len(row_codes) > 1:
                ident, how = "name:" + key_name, "ambiguous_codes_in_permit"
            elif len(name_codes.get(key_name, ())) == 1:
                ident, how = "code:" + next(iter(name_codes[key_name])), "unique_code_for_exact_name"
            else:
                ident, how = "name:" + key_name, "name_only"
            join[how] += 1
            if ident in seen:
                continue
            seen.add(ident)
            permits_of[ident].add(permit)
            raw_names[ident][item] += 1

    def official_name(ident: str) -> str:
        counts = raw_names[ident]
        return sorted(counts, key=lambda n: (-counts[n], n))[0]

    ranked = sorted(permits_of, key=lambda i: (-len(permits_of[i]), i))
    # 約 top_n 個：nominal rank top_n 的藥證數即門檻，保留所有 >= 門檻的並列項目（不以 key 任意切掉同頻者；筆數可略高於 top_n）
    threshold = len(permits_of[ranked[min(top_n, len(ranked)) - 1]]) if ranked and top_n > 0 else 0
    top = [i for i in ranked if len(permits_of[i]) >= threshold] if ranked and top_n > 0 else []
    build = CatalogBuild()
    exact_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in reviewed_drugs:
        exact_index[normalize_exact_name(d["canonical_name_en"])].append(d)
    for rank, ident in enumerate(top, start=1):
        name = official_name(ident)
        normalized = normalize_exact_name(name)
        code = ident[5:] if ident.startswith("code:") else None
        row = {"ingredient_code": code, "official_name": name, "normalized_exact_name": normalized,
               "active_permit_count": len(permits_of[ident]), "rank": rank, "source_id": SOURCE_ID,
               "interaction_mapping_status": STATUS_NOT_MAPPED, "mapped_drug_substance_id": None, "mapping_basis": None}
        matches = exact_index.get(normalized, [])
        if len(matches) == 1:
            row.update(interaction_mapping_status=STATUS_REVIEWED_EXACT, mapped_drug_substance_id=matches[0]["drug_substance_id"],
                       mapping_basis=MAPPING_BASIS_EXACT)
            build.mappings.append({"ingredient_code": code, "official_name": name, "drug_substance_id": matches[0]["drug_substance_id"]})
        elif len(matches) > 1:
            build.ambiguous_mappings.append({"official_name": name, "candidates": [m["drug_substance_id"] for m in matches]})
        build.ingredients.append(row)
        build.permit_rows += [(ident, p) for p in sorted(permits_of[ident])]
    for d in reviewed_drugs:  # 鹽類 / 形式變體：只列出候選，需人工審核（不自動對應）
        base = normalize_exact_name(d["canonical_name_en"])
        for ident in top:
            n = normalize_exact_name(official_name(ident))
            if base in n and n != base:
                build.variant_candidates.append({"drug_substance_id": d["drug_substance_id"], "official_name": official_name(ident),
                                                 "active_permit_count": len(permits_of[ident])})
    cutoff = len(permits_of[top[-1]]) if top else 0
    build.audit = {
        "count_definition": COUNT_DEFINITION, "selection": SELECTION_DESCRIPTION, "active_ingredient_rule": ACTIVE_INGREDIENT_RULE,
        "permits_current_rows": len(current_rows), "permits_current_unique": len(current),
        "excluded_cancelled_in_9122": len(cancelled_conflict), "permits_in_universe": len(universe),
        "detail_rows": len(detail_rows), "join_methods": dict(join), "unique_main_ingredients": len(ranked),
        "nominal_top_n": top_n, "selected_count": len(top), "cutoff_permit_count": cutoff,
        "tie_count_at_cutoff": sum(1 for i in top if len(permits_of[i]) == cutoff),
        "with_code": sum(1 for r in build.ingredients if r["ingredient_code"]),
        "without_code": sum(1 for r in build.ingredients if not r["ingredient_code"]),
        "reviewed_exact_mappings": len(build.mappings), "ambiguous_mappings": len(build.ambiguous_mappings),
        "variant_candidates_not_mapped": len(build.variant_candidates), "permit_provenance_rows": len(build.permit_rows),
    }
    return build
