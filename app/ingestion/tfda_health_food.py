"""TFDA「健康食品資料集」（data.fda.gov.tw InfoId 19）正規化。

官方原始紀錄 → companies / permits / products / ingredients / approved_claims /
product_claims / warnings 的 canonical rows（含 canonical UUID 與來源欄位）。

所有規則都是針對欄位格式的一般性規則；原始紀錄完整保存在 raw_import_records.raw_data，
成分原文保存在 product_ingredients.raw_text，parser 不完美時仍可追溯。
"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.ingestion.company_name import normalize_company_name
from app.ingestion.ingredient_canonical import IngredientMention, canonical_key, split_inline_bilingual
from app.ingestion.ingredient_parser import IngredientParseStatus, parse_ingredient_text
from app.normalization import extract_permit_code, extract_permit_type, normalize_name, stable_id
from app.schemas.enums import ProductStatus
from app.schemas.evidence import CLAIM_KIND_UNCLASSIFIED

SOURCE_KEY = "tw_fda_health_food_dataset"
DATASET_URL = "https://data.fda.gov.tw/data/opendata/export/19/json"
SOURCE_REGISTRY_ROW = {
    "source_key": SOURCE_KEY,
    "source_name": "衛生福利部食品藥物管理署 健康食品資料集",
    "source_type": "open_data",
    "publisher": "衛生福利部食品藥物管理署",
    "base_url": DATASET_URL,
    "country_code": "TW",
    "is_official": True,
    "description": "TFDA 開放資料 InfoId 19：健康食品許可證、品名、申請商、保健功效、警語與注意事項",
}

STATUS_MAP = {
    "核可": ProductStatus.ACTIVE,
    "失效": ProductStatus.EXPIRED,
    "併証": ProductStatus.MERGED,
    "併證": ProductStatus.MERGED,
    "註銷": ProductStatus.CANCELLED,
    "廢止": ProductStatus.REVOKED,
}

# 官方紀錄有「保健功效宣稱」原文、但「保健功效」類別欄位空白時：
# 不推測類別，改掛在固定的「未分類官方宣稱」上，宣稱原文仍是 APPROVED_OFFICIAL evidence。
CLAIM_KIND_FUNCTION = "HEALTH_FOOD_FUNCTION"
UNCLASSIFIED_FUNCTION_KEY = "__unclassified_official_claim__"
UNCLASSIFIED_FUNCTION_NAME = "未分類（官方資料未列功效類別）"

_TRAILING_PAREN_RE = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")
_WARNING_SPLIT_RE = re.compile(r"\s*(?:[（(]\d{1,2}[）)]|(?:^|\n)\s*\d{1,2}[.、])\s*")


class HealthFoodBundle(BaseModel):
    """一筆官方紀錄正規化後的所有 rows。"""

    source_record_key: str
    raw_data: dict[str, Any]
    raw_hash: str
    source_url: str | None
    company: dict[str, Any]
    permit: dict[str, Any]
    product: dict[str, Any]
    # 申請商正規化追溯（不寫入 companies 表；原文另存於 raw_data）
    company_name_raw: str = ""
    company_name_status: str = "unchanged"
    company_name_removed_notes: list[str] = Field(default_factory=list)
    # 成分解析品質（不寫入獨立欄位；原文保存在 product_ingredients.raw_text 與 raw_data）
    ingredient_parse_status: str = IngredientParseStatus.NO_SOURCE_INGREDIENT.value
    ingredient_unresolved_segments: list[str] = Field(default_factory=list)
    # 成分提及（canonicalization / alias 用，不寫入 DB）
    ingredient_mentions: list[IngredientMention] = Field(default_factory=list)
    ingredients: list[dict[str, Any]] = Field(default_factory=list)
    product_ingredients: list[dict[str, Any]] = Field(default_factory=list)
    approved_claims: list[dict[str, Any]] = Field(default_factory=list)
    product_claims: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    product_warnings: list[dict[str, Any]] = Field(default_factory=list)


def _clean(text: Any) -> str:
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def flatten_record(record: Any) -> dict[str, Any]:
    """API 有兩種格式：扁平 dict，或 [{欄位: 值}, ...] list。"""
    if isinstance(record, dict):
        return record
    flat: dict[str, Any] = {}
    for item in record:
        flat.update(item)
    return flat


def clean_permit_number(raw: str) -> str:
    return _TRAILING_PAREN_RE.sub("", _clean(raw)).strip()


def parse_date(raw: Any) -> str | None:
    text = _clean(raw)
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_ingredients(raw: Any) -> list[dict[str, str | None]]:
    """相容介面：只回傳成功解析的成分；完整狀態請用 parse_ingredient_text。"""
    return [i.model_dump(exclude={"source_segment"}) for i in parse_ingredient_text(raw).ingredients]


def parse_functions(raw: Any) -> list[str]:
    names: list[str] = []
    for part in re.split(r"[,，、]", _clean(raw)):
        part = part.strip()
        if "-" in part:
            part = part.rsplit("-", 1)[1].strip()
        part = re.sub(r"功能$", "", part).strip()
        if part and part not in names:
            names.append(part)
    return names


def clean_claim_text(raw: Any) -> str:
    text = _clean(raw).strip("「」\"' ")
    return re.sub(r"[ \t]+", " ", text)


def split_numbered(raw: Any) -> list[str]:
    text = _clean(raw)
    if not text:
        return []
    items = [s.strip(" \n") for s in _WARNING_SPLIT_RE.split(text)]
    return [s for s in items if s]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_record(record: Any, retrieved_at: datetime) -> HealthFoodBundle | None:
    raw = flatten_record(record)
    permit_number = clean_permit_number(raw.get("許可證字號") or "")
    product_name = _clean(raw.get("中文品名"))
    company_name_result = normalize_company_name(_clean(raw.get("申請商")))
    company_name = company_name_result.name
    permit_code = extract_permit_code(permit_number)
    if not (permit_number and product_name and permit_code):
        return None

    retrieved = retrieved_at.isoformat()
    key = permit_number
    url = _clean(raw.get("網址")) or None
    status_raw = _clean(raw.get("證況"))
    status = STATUS_MAP.get(status_raw, ProductStatus.UNKNOWN).value
    common = {"source_record_key": key, "source_url": url, "retrieved_at": retrieved}

    company_norm = normalize_name(company_name)
    company_id = stable_id("company", f"TW:{company_norm}")
    permit_id = stable_id("permit", f"TW:{permit_number}")
    product_id = stable_id("product", f"TW:{permit_number}")

    bundle = HealthFoodBundle(
        source_record_key=key,
        raw_data=raw,
        raw_hash=_sha(json.dumps(raw, ensure_ascii=False, sort_keys=True)),
        source_url=url,
        company_name_raw=company_name_result.raw,
        company_name_status=company_name_result.status.value,
        company_name_removed_notes=company_name_result.removed_notes,
        company={"id": company_id, "country_code": "TW", "name_zh": company_name, "name_normalized": company_norm, **common},
        permit={
            "id": permit_id, "country_code": "TW", "permit_number": permit_number, "permit_code": permit_code,
            "permit_type": extract_permit_type(permit_number) or _clean(raw.get("類別")) or None,
            "approval_date": parse_date(raw.get("核可日期")), "status": status, "status_raw": status_raw or None, **common,
        },
        product={
            "id": product_id, "country_code": "TW", "product_name_zh": product_name,
            "product_name_normalized": normalize_name(product_name), "product_category": "health_food",
            "company_id": company_id, "permit_id": permit_id, "status": status, **common,
        },
    )

    parsed = parse_ingredient_text(raw.get("保健功效相關成分"))
    bundle.ingredient_parse_status = parsed.status.value
    bundle.ingredient_unresolved_segments = parsed.unresolved_segments
    for ing in parsed.ingredients:
        # 同一欄位「中文名English name」相連 → 一個 canonical，英文另作 alias
        name_zh, inline_en = split_inline_bilingual(ing.name_zh)
        norm = canonical_key(name_zh)
        ing_id = stable_id("ingredient", norm)
        english = ing.name_en or inline_en
        bundle.ingredient_mentions.append(IngredientMention(
            ingredient_id=ing_id, surface=ing.name_zh, name_zh=name_zh, english=english,
            english_basis=("official_parenthetical_english" if ing.name_en else "official_inline_bilingual") if english else None,
            source_record_key=key, source_url=url,
        ))
        if any(i["id"] == ing_id for i in bundle.ingredients):
            continue
        bundle.ingredients.append({
            "id": ing_id, "name_zh": name_zh, "name_en": english, "name_normalized": norm,
            "source_record_key": key, "retrieved_at": retrieved,
        })
        bundle.product_ingredients.append({
            "product_id": product_id, "ingredient_id": ing_id, "raw_text": parsed.raw_text,
            "amount_text": ing.amount_text, **common,
        })

    claim_text = clean_claim_text(raw.get("保健功效宣稱"))
    functions = [(fn, normalize_name(fn), CLAIM_KIND_FUNCTION) for fn in parse_functions(raw.get("保健功效"))]
    if not functions and claim_text:
        functions = [(UNCLASSIFIED_FUNCTION_NAME, UNCLASSIFIED_FUNCTION_KEY, CLAIM_KIND_UNCLASSIFIED)]
    for fn, fkey, kind in functions:
        claim_id = stable_id("approved_claim", f"TW:{fkey}")
        bundle.approved_claims.append({
            "id": claim_id, "country_code": "TW", "function_name": fn, "function_key": fkey, "claim_kind": kind,
        })
        if claim_text:
            bundle.product_claims.append({
                "id": stable_id("product_claim", f"{product_id}:{claim_id}"), "product_id": product_id,
                "approved_claim_id": claim_id, "claim_type": "APPROVED_OFFICIAL", "claim_text": claim_text, **common,
            })

    for field, wtype in (("警語", "WARNING"), ("注意事項", "PRECAUTION")):
        for text in split_numbered(raw.get(field)):
            text_hash = _sha(f"{wtype}:{normalize_name(text)}")
            wid = stable_id("warning", text_hash)
            bundle.warnings.append({"id": wid, "warning_type": wtype, "text": text, "text_hash": text_hash})
            bundle.product_warnings.append({"product_id": product_id, "warning_id": wid, **common})
    return bundle


