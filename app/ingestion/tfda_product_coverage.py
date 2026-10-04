"""TFDA 產品登記覆蓋擴充（Taiwan Product Coverage）importer。

兩個官方開放資料集（data.fda.gov.tw）：
  InfoId 24（data.gov.tw 9046）國產維生素類錠狀膠囊狀食品查驗登記  → domestic_vitamin_tablet_capsule
  InfoId 23（data.gov.tw 9047）輸入錠狀膠囊狀食品查驗登記（核備）  → imported_tablet_capsule

法律身分（與既有 565 筆 health_food 分開）：
  新類別只表示「查有官方查驗登記／核備資料」。不建立 HEALTH_FOOD、小綠人、approved health claim、
  產品安全結論；不產生 product_claims / approved_claims / warnings。

沿用既有 schema（0001），不新增資料表：
  source_registry      每個資料集一列（source_key：tw_fda_domestic_vitamin_tablet_capsule / tw_fda_imported_tablet_capsule）
  raw_import_records   官方原始列原樣保存（raw_hash = 官方原始列 sha256）＋登記事實＋成分 staging
  companies            申請商（只新增；與既有公司同正規化名稱時沿用，不改動既有列）
  permits              查驗登記證號 / 核備函字號（permit_number = permit_code = 原始字串，保留前導零）
  products             product_category = 新類別；status = UNKNOWN（官方未提供狀態），status_raw = NULL

Canonical key：TFDA:{source_info_id}:{原始官方字號}（不轉 integer、不用品名）。
  products.id = uuid5('product', 'TW:' + key)；permits.id = uuid5('permit', 'TW:' + key)

官方狀態：official_status 一律 NULL（permits.status_raw = NULL；products / permits.status = UNKNOWN 代表「未提供」）。
  derived_temporal_status（依有效日期比較）會隨時間改變，不寫入 DB；只在 dry-run 報告中依 as-of 日期計算。

成分：只保存 ingredients_raw 與研究產出的 source token draft、review_status / review_reasons（在 raw_data），
  不建立 ingredients / product_ingredients / Ingredient node / INTERACTS_WITH，不拆複方、不推測化學 ID 或劑量。
  READY / NEEDS_REVIEW 是成分解析品質，不影響產品登記匯入。

跨類別候選（08）與同名不同字號（07）只作 audit 報告：不 SAME_AS、不 merge、不以品名合併。
"""

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

from app.ingestion.company_name import normalize_company_name
from app.normalization import normalize_name, stable_id
from app.product_coverage import derived_temporal_status as runtime_temporal_status
from app.schemas.enums import ProductCategory  # runtime 與 importer 共用同一份類別 enum

COVERAGE_NAME = "tfda_product_coverage"
COUNTRY = "TW"
WRITE_BATCH = 200
PRODUCT_STATUS_NOT_PROVIDED = "UNKNOWN"  # products / permits.status NOT NULL；UNKNOWN = 官方資料集未提供狀態
MIGRATION_0004_MARKER = "migration_0004_tfda_product_coverage"
RAW_SCHEMA = "tfda_product_coverage_raw_v1"
NEO4J_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "migrations" / "neo4j" / "0003_product_category.cypher"
OFFICIAL_HOST_SUFFIXES = ("fda.gov.tw", "data.gov.tw", "mohw.gov.tw")


class ReviewStatus(StrEnum):
    """成分解析品質（研究產出）；不是產品登記可信度。"""

    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class QuarantineReason(StrEnum):
    MISSING_REGISTRATION_NUMBER = "MISSING_REGISTRATION_NUMBER"
    INVALID_REGISTRATION_NUMBER = "INVALID_REGISTRATION_NUMBER"
    MISSING_PRODUCT_NAME = "MISSING_PRODUCT_NAME"
    UNKNOWN_CATEGORY = "UNKNOWN_CATEGORY"
    UNKNOWN_REVIEW_STATUS = "UNKNOWN_REVIEW_STATUS"
    KEY_COLLISION = "KEY_COLLISION"
    REGISTRATION_NUMBER_COLLISION = "REGISTRATION_NUMBER_COLLISION"
    DB_ID_COLLISION = "DB_ID_COLLISION"
    INVALID_DATE = "INVALID_DATE"
    NORMALIZED_MISMATCH = "NORMALIZED_MISMATCH"


COLLISION_REASONS = {QuarantineReason.KEY_COLLISION, QuarantineReason.REGISTRATION_NUMBER_COLLISION,
                     QuarantineReason.DB_ID_COLLISION}
MISSING_REQUIRED_REASONS = {QuarantineReason.MISSING_REGISTRATION_NUMBER, QuarantineReason.MISSING_PRODUCT_NAME}


@dataclass(frozen=True)
class DatasetSpec:
    info_id: str
    dataset_id: str
    dataset_category: str          # 研究產出 normalized CSV 的 product_category 值
    category: ProductCategory
    source_key: str
    source_name: str
    description: str
    raw_file: str
    normalized_file: str
    number_field: str              # 官方字號欄位（原始中文欄名）
    permit_type: str               # 資料集定義的字號類型名稱（不是健康食品許可）
    signing_field: str | None      # 簽審文件編號（只有輸入資料集有）


DATASETS: dict[str, DatasetSpec] = {
    "24": DatasetSpec(
        info_id="24", dataset_id="9046", dataset_category="DOMESTIC_VITAMIN_TABLET_CAPSULE",
        category=ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE,
        source_key="tw_fda_domestic_vitamin_tablet_capsule",
        source_name="衛生福利部食品藥物管理署 國產維生素類錠狀膠囊狀食品查驗登記資料集",
        description=("TFDA 開放資料 InfoId 24（data.gov.tw 9046）：國產維生素類錠狀膠囊狀食品查驗登記證號、品名、申請商、"
                     "發證／有效日期、成分原文。食品查驗登記，不是健康食品許可，不含核准保健功效；資料集未提供官方狀態。"),
        raw_file="01_tfda_domestic_vitamin_raw.csv", normalized_file="03_tfda_domestic_vitamin_normalized.csv",
        number_field="查驗登記證號", permit_type="查驗登記證", signing_field=None,
    ),
    "23": DatasetSpec(
        info_id="23", dataset_id="9047", dataset_category="IMPORTED_TABLET_CAPSULE",
        category=ProductCategory.IMPORTED_TABLET_CAPSULE,
        source_key="tw_fda_imported_tablet_capsule",
        source_name="衛生福利部食品藥物管理署 輸入錠狀膠囊狀食品查驗登記資料集",
        description=("TFDA 開放資料 InfoId 23（data.gov.tw 9047）：輸入錠狀膠囊狀食品核備函字號、品名、申請商、"
                     "發證／有效日期、成分原文、簽審文件編號。食品查驗登記（核備），不是健康食品許可，不含核准保健功效；"
                     "資料集未提供官方狀態。"),
        raw_file="02_tfda_imported_tablet_capsule_raw.csv", normalized_file="04_tfda_imported_tablet_capsule_normalized.csv",
        number_field="核備函字號", permit_type="核備函", signing_field="簽審文件編號",
    ),
}

FILES = {
    "sources": "05_tfda_product_dataset_sources.csv",
    "duplicates": "07_tfda_product_duplicates.csv",
    "candidates": "08_tfda_cross_category_match_candidates.csv",
    "validation": "10_tfda_product_data_validation.json",
    "checksums": "SHA256SUMS.txt",
}
NAME_FIELD, NAME_EN_FIELD = "中文品名", "英文品名"
ISSUE_FIELD, EXPIRY_FIELD = "發證日期", "有效日期"
APPLICANT_FIELDS = ("申請商名稱", "申請商電話", "申請商地址")
INGREDIENT_FIELD = "成分"
UNAVAILABLE_FIELDS = ("official_status", "manufacturer_name", "manufacturer_address", "country_of_origin", "dosage_form")
_NUMBER_RE = re.compile(r"^\d+$")
_ROC_DATE_RE = re.compile(r"^(\d{2,3})(\d{2})(\d{2})$")


# ---------------------------------------------------------------------------- 一般規則


def canonical_key(info_id: str, registration_number: str) -> str:
    """TFDA:{source_info_id}:{原始官方字號}；字號原樣保留（前導零不去除）。"""
    return f"TFDA:{info_id}:{registration_number}"


def product_id_for(key: str) -> str:
    return stable_id("product", f"{COUNTRY}:{key}")


def permit_id_for(key: str) -> str:
    return stable_id("permit", f"{COUNTRY}:{key}")


def source_registry_id(source_key: str) -> str:
    return stable_id("source_registry", source_key)


def raw_record_id_for(source_key: str, key: str, raw_hash: str) -> str:
    return stable_id("raw_import_record", f"{source_key}:{key}:{raw_hash}")


def parse_roc_date(raw: str | None) -> str | None:
    """6 或 7 碼民國日期（YYMMDD / YYYMMDD）→ YYYY-MM-DD；空值 → None；格式或日曆錯誤 → ValueError。"""
    text = (raw or "").strip()
    if not text:
        return None
    match = _ROC_DATE_RE.match(text)
    if not match:
        raise ValueError(f"不是 6/7 碼民國日期：{text!r}")
    year, month, day = (int(g) for g in match.groups())
    return date(year + 1911, month, day).isoformat()


def derived_temporal_status(expiry_date: str | None, as_of: date) -> str | None:
    """依有效日期的日期比較（當天仍算 CURRENT_BY_DATE）。不是官方法律狀態，不寫入 DB；與 runtime 共用同一規則。"""
    status = runtime_temporal_status(expiry_date, as_of)
    return status.value if status else None


def raw_row_sha256(raw: dict[str, str]) -> str:
    """與研究產出相同的列雜湊：UTF-8、ensure_ascii=False、separators=(',', ':')、表頭順序、不排序。"""
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _null(value: Any) -> Any:
    """研究產出的 normalized CSV 以字面 NULL 表示缺值。"""
    return None if value in (None, "NULL") else value


def _blank_to_none(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _is_official_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in OFFICIAL_HOST_SUFFIXES)


def _iso_utc(value: Any) -> str | None:
    if value in (None, ""):
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------- 讀檔


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


@dataclass
class DatasetFiles:
    spec: DatasetSpec
    raw_header: list[str]
    raw_rows: list[dict[str, str]]
    normalized_rows: list[dict[str, str]]
    source: dict[str, str] | None      # 05 中該資料集 CSV 來源列
    raw_sha256: str


@dataclass
class CoverageSnapshot:
    directory: Path
    datasets: dict[str, DatasetFiles]
    validation: dict[str, Any]
    checksums: dict[str, str]
    file_hashes: dict[str, str]
    candidates: list[dict[str, str]]
    duplicates: list[dict[str, str]]

    @property
    def snapshot_version(self) -> str:
        return str(self.validation.get("research_date") or "")

    @property
    def batch_key(self) -> str:
        digest = json.dumps(sorted(self.file_hashes.items()), separators=(",", ":"))
        return hashlib.sha256(digest.encode("utf-8")).hexdigest()


def load_snapshot(directory: str | Path) -> CoverageSnapshot:
    directory = Path(directory)
    checksums: dict[str, str] = {}
    sums_path = directory / FILES["checksums"]
    if sums_path.is_file():
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, name = line.split(maxsplit=1)
                checksums[name.strip().lstrip("*")] = digest
    _, sources = _read_csv(directory / FILES["sources"])
    datasets: dict[str, DatasetFiles] = {}
    file_hashes: dict[str, str] = {}
    for info_id, spec in DATASETS.items():
        raw_path, norm_path = directory / spec.raw_file, directory / spec.normalized_file
        header, raw_rows = _read_csv(raw_path)
        _, normalized = _read_csv(norm_path)
        source = next((s for s in sources if s.get("source_info_id") == info_id
                       and s.get("source_role") == "CORE_FULL_PRODUCT_EXPORT"), None)
        file_hashes[spec.raw_file] = file_sha256(raw_path)
        file_hashes[spec.normalized_file] = file_sha256(norm_path)
        datasets[info_id] = DatasetFiles(spec, header, raw_rows, normalized, source, file_hashes[spec.raw_file])
    for name in (FILES["sources"], FILES["validation"], FILES["candidates"], FILES["duplicates"]):
        file_hashes[name] = file_sha256(directory / name)
    _, candidates = _read_csv(directory / FILES["candidates"])
    _, duplicates = _read_csv(directory / FILES["duplicates"])
    validation = json.loads((directory / FILES["validation"]).read_text(encoding="utf-8"))
    return CoverageSnapshot(directory, datasets, validation, checksums, file_hashes, candidates, duplicates)


# ---------------------------------------------------------------------------- 結構驗證（任何錯誤都阻擋 apply）


def validate_snapshot(snapshot: CoverageSnapshot) -> list[str]:
    errors: list[str] = []
    for name, digest in snapshot.file_hashes.items():
        expected = snapshot.checksums.get(name)
        if expected is None:
            errors.append(f"SHA256SUMS 未列出 {name}")
        elif expected != digest:
            errors.append(f"{name} sha256 與 SHA256SUMS 不符")
    vds = snapshot.validation.get("datasets") or {}
    for info_id, files in snapshot.datasets.items():
        spec, expected = files.spec, vds.get(info_id) or {}
        if not expected:
            errors.append(f"validation.json 缺 InfoId {info_id}")
            continue
        if files.raw_header != expected.get("raw_field_names"):
            errors.append(f"InfoId {info_id} 原始表頭與 validation.json 不符：{files.raw_header}")
        if spec.number_field not in files.raw_header or NAME_FIELD not in files.raw_header:
            errors.append(f"InfoId {info_id} 缺少必要欄位（{spec.number_field} / {NAME_FIELD}）")
        if len(files.raw_rows) != expected.get("raw_rows"):
            errors.append(f"InfoId {info_id} 原始列數 {len(files.raw_rows)} ≠ validation.json {expected.get('raw_rows')}")
        if len(files.normalized_rows) != len(files.raw_rows):
            errors.append(f"InfoId {info_id} normalized 列數 {len(files.normalized_rows)} ≠ 原始 {len(files.raw_rows)}")
        if files.raw_sha256 != expected.get("raw_sha256"):
            errors.append(f"InfoId {info_id} 原始檔 sha256 與 validation.json 不符")
        src = files.source
        if src is None:
            errors.append(f"05 來源清單缺 InfoId {info_id} 的 CORE_FULL_PRODUCT_EXPORT")
        else:
            if src.get("sha256") != files.raw_sha256:
                errors.append(f"InfoId {info_id} 原始檔 sha256 與 05 來源清單不符")
            if not _is_official_host(src.get("source_url") or ""):
                errors.append(f"InfoId {info_id} 來源網址不是台灣官方網域：{src.get('source_url')}")
            if src.get("retrieval_status") != "SUCCESS":
                errors.append(f"InfoId {info_id} 來源擷取狀態不是 SUCCESS")
            try:
                _iso_utc(src.get("retrieved_at"))
            except ValueError:
                errors.append(f"InfoId {info_id} retrieved_at 無法解析：{src.get('retrieved_at')}")
    return errors


# ---------------------------------------------------------------------------- 逐列正規化


@dataclass
class CoverageRecord:
    info_id: str
    raw_row_number: int
    key: str | None
    registration_number: str | None
    product_name: str | None
    category: ProductCategory | None
    review_status: str | None
    quarantine: list[str] = field(default_factory=list)
    raw: dict[str, str] = field(default_factory=dict)
    raw_hash: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)
    company: dict[str, Any] | None = None
    permit: dict[str, Any] | None = None
    permit_ext: dict[str, Any] = field(default_factory=dict)   # 0004 欄位（只在 0004 marker 存在時寫入）
    product: dict[str, Any] | None = None
    raw_record: dict[str, Any] | None = None
    company_name_status: str = "unchanged"

    @property
    def importable(self) -> bool:
        return not self.quarantine


def _ingredient_staging(norm: dict[str, str], raw: dict[str, str]) -> dict[str, Any]:
    draft_text = _null(norm.get("ingredients_normalized"))
    draft = json.loads(draft_text) if draft_text else None
    try:
        reasons = json.loads(norm.get("review_reasons") or "[]")
    except json.JSONDecodeError:
        reasons = [norm.get("review_reasons")]
    return {
        "ingredients_raw": raw.get(INGREDIENT_FIELD) if (raw.get(INGREDIENT_FIELD) or "").strip() else None,
        "parser_draft": draft,  # source token 序列（research draft），不是已識別的化學成分
        "review_status": norm.get("review_status"),
        "review_reasons": reasons,
        "chemical_identification": "NOT_PERFORMED",
        "compound_expansion": "NOT_PERFORMED",
        "dose_extraction": "NOT_PERFORMED",
        "interaction_eligible": False,
    }


def normalize_dataset(files: DatasetFiles, retrieved_at: str) -> list[CoverageRecord]:
    spec, src = files.spec, files.source or {}
    source_url = src.get("source_url")
    out: list[CoverageRecord] = []
    for index, raw in enumerate(files.raw_rows, start=1):
        norm = files.normalized_rows[index - 1] if index - 1 < len(files.normalized_rows) else {}
        number = (raw.get(spec.number_field) or "").strip()
        name = (raw.get(NAME_FIELD) or "").strip()
        category = (spec.category if norm.get("product_category") == spec.dataset_category else None)
        rec = CoverageRecord(
            info_id=spec.info_id, raw_row_number=index, key=canonical_key(spec.info_id, number) if number else None,
            registration_number=number or None, product_name=name or None, category=category,
            review_status=norm.get("review_status"), raw=raw, raw_hash=raw_row_sha256(raw),
        )
        if not number:
            rec.quarantine.append(QuarantineReason.MISSING_REGISTRATION_NUMBER)
        elif not _NUMBER_RE.match(number):
            rec.quarantine.append(QuarantineReason.INVALID_REGISTRATION_NUMBER)
        if not name:
            rec.quarantine.append(QuarantineReason.MISSING_PRODUCT_NAME)
        if category is None:
            rec.quarantine.append(QuarantineReason.UNKNOWN_CATEGORY)
        if rec.review_status not in {s.value for s in ReviewStatus}:
            rec.quarantine.append(QuarantineReason.UNKNOWN_REVIEW_STATUS)
        try:
            issue = parse_roc_date(raw.get(ISSUE_FIELD))
            expiry = parse_roc_date(raw.get(EXPIRY_FIELD))
        except ValueError:
            issue = expiry = None
            rec.quarantine.append(QuarantineReason.INVALID_DATE)
        # 研究產出與本 importer 的 deterministic 轉換必須一致（任何差異都隔離，不猜哪邊對）
        if norm and not rec.quarantine:
            mismatch = (
                norm.get("source_row_key") != rec.key
                or int(norm.get("raw_row_number") or 0) != index
                or norm.get("raw_row_sha256") != rec.raw_hash
                or json.loads(norm.get("raw_source_fields_json") or "{}") != raw
                or _null(norm.get("issue_date")) != issue
                or _null(norm.get("expiry_date")) != expiry
                or _null(norm.get("official_status")) is not None
            )
            if mismatch:
                rec.quarantine.append(QuarantineReason.NORMALIZED_MISMATCH)
        elif not norm:
            rec.quarantine.append(QuarantineReason.NORMALIZED_MISMATCH)
        if rec.quarantine:
            out.append(rec)
            continue

        signing = _blank_to_none(raw.get(spec.signing_field)) if spec.signing_field else None
        applicant = normalize_company_name((raw.get(APPLICANT_FIELDS[0]) or "").strip())
        rec.company_name_status = applicant.status.value
        rec.raw_data = {
            "schema": RAW_SCHEMA,
            "source_info_id": spec.info_id,
            "source_dataset_id": spec.dataset_id,
            "product_category": spec.category.value,
            "canonical_key": rec.key,
            "raw_row_number": index,
            "raw_row_sha256": rec.raw_hash,
            "dataset_raw_sha256": files.raw_sha256,
            "raw_source_fields": raw,  # 官方原始欄位與原始字串（含空字串），可完整重建該列
            "registration": {
                "permit_or_registration_no": number,
                "permit_type": spec.permit_type,
                "issue_date": issue,
                "expiry_date": expiry,
                "issue_date_raw": _blank_to_none(raw.get(ISSUE_FIELD)),
                "expiry_date_raw": _blank_to_none(raw.get(EXPIRY_FIELD)),
                "official_status": None,  # 官方資料集未提供；不得以有效日期推導代替
                "signing_document_no": signing,
                "applicant_name_raw": applicant.raw or None,
                "applicant_phone": _blank_to_none(raw.get(APPLICANT_FIELDS[1])),
                "applicant_address": _blank_to_none(raw.get(APPLICANT_FIELDS[2])),
            },
            "not_provided_by_source": list(UNAVAILABLE_FIELDS),
            "ingredient_staging": _ingredient_staging(norm, raw),
        }
        common = {"source_record_key": rec.key, "source_url": source_url, "retrieved_at": retrieved_at}
        company_norm = normalize_name(applicant.name)
        company_id = stable_id("company", f"{COUNTRY}:{company_norm}") if company_norm else None
        if company_id:
            rec.company = {"id": company_id, "country_code": COUNTRY, "name_zh": applicant.name,
                           "name_normalized": company_norm, **common}
        pid, mid = product_id_for(rec.key), permit_id_for(rec.key)
        rec.permit = {
            "id": mid, "country_code": COUNTRY, "permit_number": number,
            # permit_code = 完整原始字號（與 extract_permit_code 對完整數字字號的結果相同：不截斷、保留前導零）
            "permit_code": number, "permit_type": spec.permit_type, "approval_date": issue,
            "status": PRODUCT_STATUS_NOT_PROVIDED, "status_raw": None, **common,
        }
        rec.permit_ext = {"expiry_date": expiry, "signing_document_no": signing}
        rec.product = {
            "id": pid, "country_code": COUNTRY, "product_name_zh": name,
            "product_name_en": _blank_to_none(raw.get(NAME_EN_FIELD)),
            "product_name_normalized": normalize_name(name), "product_category": spec.category.value,
            "company_id": company_id, "permit_id": mid, "status": PRODUCT_STATUS_NOT_PROVIDED, **common,
        }
        out.append(rec)
    return out


def _mark_collisions(records: list[CoverageRecord]) -> None:
    """同一 canonical key 或同一官方字號（permits 以 (country_code, permit_number) unique）出現多列 → 全部隔離，不覆寫。"""
    by_key: dict[str, list[CoverageRecord]] = defaultdict(list)
    by_number: dict[str, list[CoverageRecord]] = defaultdict(list)
    for rec in records:
        if rec.key:
            by_key[rec.key].append(rec)
        if rec.registration_number:
            by_number[rec.registration_number].append(rec)
    for group in by_key.values():
        if len(group) > 1:
            for rec in group:
                rec.quarantine.append(QuarantineReason.KEY_COLLISION)
    for group in by_number.values():
        if len({r.info_id for r in group}) > 1:
            for rec in group:
                rec.quarantine.append(QuarantineReason.REGISTRATION_NUMBER_COLLISION)


# ---------------------------------------------------------------------------- store 介面


class CoverageStore(Protocol):
    def fetch(self, table: str, columns: list[str], eq: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None: ...


class EmptyStore:
    """dry-run 預設：視為尚未匯入；任何寫入都拒絕。"""

    def fetch(self, table, columns, eq=None):
        return []

    def upsert(self, table, rows, on_conflict):
        raise RuntimeError("dry-run store 不允許寫入")


# ---------------------------------------------------------------------------- 匯入計畫


@dataclass
class TableDiff:
    insert: list[dict[str, Any]] = field(default_factory=list)
    update: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0
    reused_existing: int = 0

    def counts(self) -> dict[str, int]:
        return {"insert": len(self.insert), "update": len(self.update), "unchanged": self.unchanged,
                "reused_existing": self.reused_existing}


@dataclass
class CoveragePlan:
    snapshot: CoverageSnapshot
    batch_id: str
    records: list[CoverageRecord]
    source_rows: list[dict[str, Any]]
    diffs: dict[str, TableDiff]
    product_outcome: dict[str, str]          # canonical key → insert / update / unchanged
    migration_0004: bool
    as_of: date
    errors: list[str]
    notes: list[str]
    db_name_overlap: list[dict[str, Any]] = field(default_factory=list)

    @property
    def importable(self) -> list[CoverageRecord]:
        return [r for r in self.records if r.importable]

    def graph_rows(self) -> dict[str, list[dict[str, Any]]]:
        recs = self.importable
        products = [{**r.product, "source_id": self.source_id(r.info_id)} for r in recs]
        permits = [{**r.permit, **(r.permit_ext if self.migration_0004 else {})} for r in recs]
        companies = list({r.company["id"]: r.company for r in recs if r.company}.values())
        sources = [{"id": s["id"], "source_name": s["source_name"], "is_official": s["is_official"],
                    "base_url": s["base_url"]} for s in self.source_rows]
        return {"sources": sources, "products": products, "permits": permits, "companies": companies}

    def source_id(self, info_id: str) -> str:
        return source_registry_id(DATASETS[info_id].source_key)

    def report(self) -> dict[str, Any]:
        recs = self.records
        by_ds = {info_id: [r for r in recs if r.info_id == info_id] for info_id in DATASETS}
        quarantined = [r for r in recs if r.quarantine]
        importable = self.importable
        outcome = Counter(self.product_outcome.values())
        reasons = Counter(q for r in quarantined for q in r.quarantine)
        cands = self.snapshot.candidates
        graph = self.graph_rows()
        return {
            "snapshot_version": self.snapshot.snapshot_version,
            "batch_key": self.snapshot.batch_key,
            "as_of": self.as_of.isoformat(),
            "datasets": {
                info_id: {
                    "category": DATASETS[info_id].category.value,
                    "source_key": DATASETS[info_id].source_key,
                    "rows": len(rows),
                    "importable": sum(r.importable for r in rows),
                    "quarantined": sum(not r.importable for r in rows),
                    "review_status": dict(Counter(r.review_status for r in rows)),
                    "derived_temporal_status_not_persisted": dict(Counter(
                        derived_temporal_status(r.permit_ext.get("expiry_date"), self.as_of)
                        for r in rows if r.importable)),
                    "missing_issue_date": sum(r.importable and r.permit["approval_date"] is None for r in rows),
                    "missing_ingredients_raw": sum(
                        r.importable and r.raw_data["ingredient_staging"]["ingredients_raw"] is None for r in rows),
                    "leading_zero_numbers": sum((r.registration_number or "").startswith("0") for r in rows),
                }
                for info_id, rows in by_ds.items()
            },
            "total_rows": len(recs),
            "products": {
                "would_insert": outcome.get("insert", 0),
                "would_update": outcome.get("update", 0),
                "would_remain_unchanged": outcome.get("unchanged", 0),
                "would_quarantine": len(quarantined),
            },
            "quarantine_reasons": dict(reasons),
            "key_collisions": sum(any(q in COLLISION_REASONS for q in r.quarantine) for r in recs),
            "missing_required_fields": sum(any(q in MISSING_REQUIRED_REASONS for q in r.quarantine) for r in recs),
            "review_status": dict(Counter(r.review_status for r in recs)),
            "review_status_importable": dict(Counter(r.review_status for r in importable)),
            "tables": {t: d.counts() for t, d in self.diffs.items()},
            "company_name_normalization": dict(Counter(r.company_name_status for r in importable)),
            "cross_category_candidates": {
                "file_pairs": len(cands),
                "by_scope_and_classification": {f"{s}/{c}": n for (s, c), n in sorted(
                    Counter((x["comparison_scope"], x["classification"]) for x in cands).items())},
                "same_name_distinct_registration_rows": len(self.snapshot.duplicates),
                "db_health_food_exact_name_overlap": len(self.db_name_overlap),
                "action": "AUDIT_ONLY; no SAME_AS, no merge",
            },
            "derived_rows_written": {"product_claims": 0, "approved_claims": 0, "warnings": 0, "ingredients": 0,
                                     "product_ingredients": 0, "brands": 0, "entity_aliases": 0},
            "graph_plan": {
                "Source": len(graph["sources"]), "Product": len(graph["products"]), "Permit": len(graph["permits"]),
                "Company(merge)": len(graph["companies"]),
                "SUPPORTED_BY(Product->Source)": len(graph["products"]),
                "REGISTERED_AS": sum(bool(p["permit_id"]) for p in graph["products"]),
                "OWNS_OR_APPLIES_FOR": sum(bool(p["company_id"]) for p in graph["products"]),
                "Ingredient": 0, "CONTAINS": 0, "HAS_APPROVED_CLAIM": 0, "INTERACTS_WITH": 0,
            },
            "migration_0004_columns": self.migration_0004,
            "errors": self.errors,
            "notes": self.notes,
        }


def build_source_rows(snapshot: CoverageSnapshot) -> list[dict[str, Any]]:
    rows = []
    for info_id, files in snapshot.datasets.items():
        spec = files.spec
        rows.append({
            "id": source_registry_id(spec.source_key), "source_key": spec.source_key, "source_name": spec.source_name,
            "source_type": "open_data", "publisher": "衛生福利部食品藥物管理署",
            "base_url": (files.source or {}).get("source_url"), "country_code": COUNTRY, "is_official": True,
            "description": spec.description,
        })
    return rows


_PRODUCT_FIELDS = ("product_name_zh", "product_name_en", "product_name_normalized", "product_category", "company_id",
                   "permit_id", "status", "source_id", "source_record_key", "source_url", "retrieved_at", "raw_record_id")
_PERMIT_FIELDS = ("permit_number", "permit_code", "permit_type", "approval_date", "status", "status_raw", "source_id",
                  "source_record_key", "source_url", "retrieved_at", "raw_record_id")
_PERMIT_EXT_FIELDS = ("expiry_date", "signing_document_no")
_REGISTRY_FIELDS = ("source_name", "source_type", "publisher", "base_url", "country_code", "is_official", "description")


def _cmp(value: Any, column: str) -> Any:
    if value is None:
        return None
    if column == "retrieved_at":
        return _iso_utc(value)
    if column in ("approval_date", "expiry_date"):
        return str(value)[:10]
    if column.endswith("_id"):
        return str(value)
    return value


def _same(new: dict[str, Any], old: dict[str, Any], columns: Iterable[str]) -> bool:
    return all(_cmp(new.get(c), c) == _cmp(old.get(c), c) for c in columns)


def plan_coverage_import(
    snapshot: CoverageSnapshot, store: CoverageStore | None = None, *, migration_0004: bool = False,
    as_of: date | None = None, allow_older_snapshot: bool = False,
) -> CoveragePlan:
    store = store or EmptyStore()
    as_of = as_of or date.today()
    errors = validate_snapshot(snapshot)
    notes: list[str] = []
    batch_id = stable_id(f"{COVERAGE_NAME}_import_batch", snapshot.batch_key)
    source_rows = build_source_rows(snapshot)
    source_ids = {r["id"] for r in source_rows}

    records: list[CoverageRecord] = []
    for info_id, files in snapshot.datasets.items():
        try:
            retrieved = _iso_utc((files.source or {}).get("retrieved_at"))
        except ValueError:
            retrieved = None
        records.extend(normalize_dataset(files, retrieved))
    _mark_collisions(records)

    # ---- source_registry
    diffs: dict[str, TableDiff] = {}
    reg = TableDiff()
    existing_reg = {r["source_key"]: r for r in store.fetch("source_registry", ["id", "source_key", *_REGISTRY_FIELDS])}
    for row in source_rows:
        old = existing_reg.get(row["source_key"])
        if old and str(old["id"]) != row["id"]:
            errors.append(f"source_registry.source_key={row['source_key']} 既有 id 與 canonical id 不同")
        elif old and _same(row, old, _REGISTRY_FIELDS):
            reg.unchanged += 1
        else:
            (reg.update if old else reg.insert).append(row)
    diffs["source_registry"] = reg

    # ---- version-aware：資料庫已有較新 snapshot 時拒絕降版
    existing_raw = store.fetch("raw_import_records", ["id", "source_id", "source_record_key", "raw_hash", "raw_data",
                                                      "retrieved_at"])
    existing_raw = [r for r in existing_raw if str(r["source_id"]) in source_ids]
    for info_id, files in snapshot.datasets.items():
        sid = source_registry_id(files.spec.source_key)
        mine = [_iso_utc(r["retrieved_at"]) for r in existing_raw if str(r["source_id"]) == sid]
        try:
            current = _iso_utc((files.source or {}).get("retrieved_at"))
        except ValueError:
            current = None
        if mine and current and max(mine) > current and not allow_older_snapshot:
            errors.append(f"InfoId {info_id} 資料庫已有較新的 snapshot（{max(mine)} > {current}），拒絕降版")

    # ---- 與資料庫既有列比對（ID 碰撞一律隔離，不覆寫其他來源的列）
    existing_products = {str(r["id"]): r for r in store.fetch("products", ["id", *_PRODUCT_FIELDS])}
    permit_cols = ["id", *_PERMIT_FIELDS, *(_PERMIT_EXT_FIELDS if migration_0004 else ())]
    existing_permits = {str(r["id"]): r for r in store.fetch("permits", permit_cols)}
    permit_by_number = {(r.get("permit_number")): str(r["id"]) for r in existing_permits.values()}
    existing_companies = {str(r["id"]) for r in store.fetch("companies", ["id"])}
    for rec in records:
        if not rec.importable:
            continue
        sid = source_registry_id(DATASETS[rec.info_id].source_key)
        old_p = existing_products.get(rec.product["id"])
        old_m = existing_permits.get(rec.permit["id"])
        other_number = permit_by_number.get(rec.registration_number)
        if ((old_p and str(old_p.get("source_id")) != sid) or (old_m and str(old_m.get("source_id")) != sid)
                or (other_number and other_number != rec.permit["id"])):
            rec.quarantine.append(QuarantineReason.DB_ID_COLLISION)

    # ---- raw_import_records（相同官方內容 = 同一列；官方內容變更 = 新增一列，舊列保留）
    raw_by_identity = {(str(r["source_id"]), r["source_record_key"], r["raw_hash"]): r for r in existing_raw}
    raw_diff = TableDiff()
    for rec in records:
        if not rec.importable:
            continue
        spec = DATASETS[rec.info_id]
        sid = source_registry_id(spec.source_key)
        old = raw_by_identity.get((sid, rec.key, rec.raw_hash))
        rid = str(old["id"]) if old else raw_record_id_for(spec.source_key, rec.key, rec.raw_hash)
        rec.raw_record = {
            "id": rid, "source_id": sid, "source_record_key": rec.key, "source_url": rec.product["source_url"],
            "retrieved_at": rec.product["retrieved_at"], "raw_data": rec.raw_data, "raw_hash": rec.raw_hash,
            "import_batch_id": batch_id,
        }
        if old is None:
            raw_diff.insert.append(rec.raw_record)
        elif old.get("raw_data") != rec.raw_data:
            raw_diff.update.append(rec.raw_record)   # 官方內容相同，只有 staging / 登記正規化結果改變
        else:
            raw_diff.unchanged += 1
        rec.product["raw_record_id"] = rec.permit["raw_record_id"] = rid
        if rec.company:
            rec.company["raw_record_id"] = rid
        rec.product["source_id"] = rec.permit["source_id"] = sid
        if rec.company:
            rec.company["source_id"] = sid
    diffs["raw_import_records"] = raw_diff

    # ---- companies：只新增；既有（含 health_food 申請商）沿用，不更新
    comp = TableDiff()
    planned: set[str] = set()
    for rec in records:
        if not rec.importable or not rec.company or rec.company["id"] in planned:
            continue
        planned.add(rec.company["id"])
        if rec.company["id"] in existing_companies:
            comp.reused_existing += 1
        else:
            comp.insert.append(rec.company)
    diffs["companies"] = comp

    # ---- permits / products
    perm, prod = TableDiff(), TableDiff()
    outcome: dict[str, str] = {}
    for rec in records:
        if not rec.importable:
            continue
        permit_row = {**rec.permit, **(rec.permit_ext if migration_0004 else {})}
        permit_cmp = (*_PERMIT_FIELDS, *(_PERMIT_EXT_FIELDS if migration_0004 else ()))
        old_m = existing_permits.get(rec.permit["id"])
        if old_m is None:
            perm.insert.append(permit_row)
        elif _same(permit_row, old_m, permit_cmp):
            perm.unchanged += 1
        else:
            perm.update.append(permit_row)
        old_p = existing_products.get(rec.product["id"])
        if old_p is None:
            prod.insert.append(rec.product)
            outcome[rec.key] = "insert"
        elif _same(rec.product, old_p, _PRODUCT_FIELDS):
            prod.unchanged += 1
            outcome[rec.key] = "unchanged"
        else:
            prod.update.append(rec.product)
            outcome[rec.key] = "update"
    diffs["permits"], diffs["products"] = perm, prod

    # ---- 與既有 health_food 產品同正規化品名：只列 audit candidate（不合併）
    overlap: list[dict[str, Any]] = []
    hf_names: dict[str, list[str]] = defaultdict(list)
    for p in existing_products.values():
        if p.get("product_category") == ProductCategory.HEALTH_FOOD.value:
            hf_names[p.get("product_name_normalized")].append(str(p["id"]))
    for rec in records:
        if rec.importable and rec.product["product_name_normalized"] in hf_names:
            overlap.append({"source_row_key": rec.key, "health_food_product_ids": hf_names[rec.product["product_name_normalized"]],
                            "classification": "CONFIRMATION_REQUIRED", "action": "CANDIDATE_ONLY; DO_NOT_MERGE"})
    if not existing_products:
        notes.append("未比對資料庫（dry-run 未連線或資料庫為空）：既有 565 health_food 重疊只依 08 研究候選")
    if not migration_0004:
        notes.append("0004 未套用：expiry_date / signing_document_no 只保存在 raw_import_records.raw_data.registration")
    return CoveragePlan(snapshot, batch_id, records, source_rows, diffs, outcome, migration_0004, as_of,
                        errors, notes, overlap)


# ---------------------------------------------------------------------------- 寫入（只有 apply 模式）


def _batched(store: CoverageStore, table: str, rows: list[dict[str, Any]], on_conflict: str) -> int:
    for i in range(0, len(rows), WRITE_BATCH):
        store.upsert(table, rows[i : i + WRITE_BATCH], on_conflict)
    return len(rows)


GRAPH_QUERIES = {
    "sources": """
        UNWIND $rows AS r MERGE (s:Source {source_id: r.id})
        SET s.source_name = r.source_name, s.is_official = r.is_official, s.base_url = r.base_url""",
    # 申請商若已存在（例如 health_food 的同一公司）只沿用，不改既有屬性
    "companies": """
        UNWIND $rows AS r MERGE (c:Company {company_id: r.id}) ON CREATE SET c.name_zh = r.name_zh""",
    "permits": """
        UNWIND $rows AS r MERGE (pm:Permit {permit_id: r.id})
        SET pm.permit_number = r.permit_number, pm.status = r.status, pm.permit_type = r.permit_type""",
    "products": """
        UNWIND $rows AS r MERGE (p:Product {product_id: r.id})
        SET p.name_zh = r.product_name_zh, p.status = r.status, p.country_code = r.country_code,
            p.product_category = r.product_category
        WITH p, r MATCH (s:Source {source_id: r.source_id})
        MERGE (p)-[rel:SUPPORTED_BY]->(s)
        SET rel.source_url = r.source_url, rel.source_record_key = r.source_record_key""",
    "links": """
        UNWIND $rows AS r MATCH (p:Product {product_id: r.id})
        OPTIONAL MATCH (c:Company {company_id: r.company_id})
        OPTIONAL MATCH (pm:Permit {permit_id: r.permit_id})
        FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | MERGE (c)-[:OWNS_OR_APPLIES_FOR]->(p))
        FOREACH (_ IN CASE WHEN pm IS NULL THEN [] ELSE [1] END | MERGE (p)-[:REGISTERED_AS]->(pm))""",
}


def prepare_coverage_graph(neo4j, health_food_source_id: str | None) -> int:
    """寫入新類別前執行 Neo4j 0003：Product.product_category index＋既有健康食品 Product backfill（只補 NULL）。

    health_food_source_id 為 None（DB 沒有健康食品資料集來源）時，backfill 不會命中任何節點。"""
    from app.services.neo4j_service import load_schema_statements

    statements = load_schema_statements(NEO4J_SCHEMA_FILE)
    for stmt in statements:
        neo4j.write(stmt, {"health_food_source_id": health_food_source_id})
    return len(statements)


def write_coverage_graph(neo4j, plan: CoveragePlan, batch_size: int = 500) -> dict[str, int]:
    rows = plan.graph_rows()
    payloads = {"sources": rows["sources"], "companies": rows["companies"], "permits": rows["permits"],
                "products": rows["products"], "links": rows["products"]}
    for stage, data in payloads.items():
        for i in range(0, len(data), batch_size):
            neo4j.write(GRAPH_QUERIES[stage], {"rows": data[i : i + batch_size]})
    return {k: len(v) for k, v in payloads.items()}


def apply_coverage_import(plan: CoveragePlan, store: CoverageStore, *, neo4j=None) -> dict[str, Any]:
    """依計畫寫入。計畫有錯誤時拒絕；隔離列一律不寫入。全部 upsert（以 canonical ID），重跑可補齊。"""
    if plan.errors:
        raise RuntimeError(f"匯入計畫有 {len(plan.errors)} 個錯誤，拒絕寫入：{plan.errors[:3]}")
    d = plan.diffs
    written = {
        "source_registry": _batched(store, "source_registry", d["source_registry"].insert + d["source_registry"].update, "source_key"),
        "raw_import_records": _batched(store, "raw_import_records", d["raw_import_records"].insert + d["raw_import_records"].update,
                                       "source_id,source_record_key,raw_hash"),
        "companies": _batched(store, "companies", d["companies"].insert, "id"),
        "permits": _batched(store, "permits", d["permits"].insert + d["permits"].update, "id"),
        "products": _batched(store, "products", d["products"].insert + d["products"].update, "id"),
    }
    report = plan.report()
    report["written"] = written
    if neo4j is not None:
        from app.ingestion.tfda_health_food import SOURCE_KEY as HEALTH_FOOD_SOURCE_KEY

        hf = store.fetch("source_registry", ["id", "source_key"], {"source_key": HEALTH_FOOD_SOURCE_KEY})
        report["graph_schema_statements"] = prepare_coverage_graph(neo4j, str(hf[0]["id"]) if hf else None)
        report["graph_written"] = write_coverage_graph(neo4j, plan)
    return report
