"""Reviewed Product / Brand Alias 審核管線（offline；只讀本機官方快照，不連線、不寫資料庫、不改 runtime）。

目的：把「同一筆台灣官方產品紀錄內的中文品名 ↔ 英文品名」整理成可人工審核的候選，
並提供之後 PRODUCT / BRAND / DRUG_TRADE / INGREDIENT_ALIAS / INGREDIENT_FORM 共用的審核 CSV 格式與 validator。

安全界線（D2）：
* 不翻譯、不相似度、不 stemming；碰撞偵測只用 NFKC / casefold / 空白折疊。
* UNIQUE_OFFICIAL_ENGLISH 只代表「之後可進入 reviewed exact retrieval」，不代表 runtime-active。
* COLLIDING_OFFICIAL_ENGLISH 一律 CANDIDATE_ONLY；品牌一律 CANDIDATE_ONLY；INVALID_OR_GENERIC 一律 NEVER_RUNTIME。
* decision 只由人填（ACCEPT / REJECT / NEEDS_REVIEW）；本模組產生的每一列都是 NEEDS_REVIEW。
  runtime 證據等級日後由證據欄位推導，不由人手填。
* validator 只回報問題，不修改任何紀錄。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

BUILDER_VERSION = "1.0"
VALIDATOR_VERSION = "1.0"
REVIEW_BATCH_SIZE = 200

# ---------------------------------------------------------------- 共用審核 CSV 格式

REVIEW_COLUMNS = (
    "review_id", "alias_kind", "alias_text_raw", "alias_text_normalized", "language",
    "target_kind", "target_id", "target_official_name", "relation",
    "evidence_tier", "source_name", "source_type", "source_url", "source_record_id", "source_locator",
    "source_sha256", "retrieved_at", "evidence_quote", "support_count",
    "collision_count", "conflict_check", "match_policy",
    "proposed_by", "decision", "decision_reason", "reviewer", "reviewed_at",
    "overlay_version", "supersedes_review_id",
)
# 候選檔多出來的欄位（審核輔助 / 追溯；不屬於共用最小欄位）
PRODUCT_EXTRA_COLUMNS = (
    "classification", "product_category", "applicant_name", "permit_number", "source_dataset_id",
    "raw_row_sha256", "runtime_key_collision_count", "review_batch",
)
BRAND_EXTRA_COLUMNS = (
    "classification", "zh_brand_candidate", "zh_prefix_support", "applicant_count", "other_applicant_records",
    "passes_rule", "rule_failures", "example_permits", "example_names", "source_dataset_id", "review_batch",
)

ALIAS_KINDS = ("PRODUCT", "BRAND", "DRUG_TRADE", "INGREDIENT_ALIAS", "INGREDIENT_FORM")
DECISIONS = ("ACCEPT", "REJECT", "NEEDS_REVIEW")
PROPOSED_BY = ("MECHANICAL_OFFICIAL", "AI_SUGGESTED", "HUMAN")
MATCH_POLICIES = ("EXACT_REVIEW_REQUIRED", "CANDIDATE_ONLY", "NEVER_RUNTIME")
# 官方證據層級：只有這些可以支持 ACCEPT
OFFICIAL_EVIDENCE_TIERS = ("OFFICIAL_RECORD_PAIR",)
EVIDENCE_TIERS = OFFICIAL_EVIDENCE_TIERS + ("OFFICIAL_RECORD_INFERENCE", "SECONDARY", "NONE")
OFFICIAL_SOURCE_TYPES = ("OFFICIAL_OPEN_DATA", "OFFICIAL_WEB")
OFFICIAL_DOMAINS = ("fda.gov.tw", "mohw.gov.tw", "data.gov.tw", "data.nat.gov.tw")

# 英文品名分類
UNIQUE = "UNIQUE_OFFICIAL_ENGLISH"
COLLIDING = "COLLIDING_OFFICIAL_ENGLISH"
INVALID = "INVALID_OR_GENERIC"
BRAND_CLASS = "BRAND_CANDIDATE"
CLASS_POLICY = {UNIQUE: "EXACT_REVIEW_REQUIRED", COLLIDING: "CANDIDATE_ONLY", INVALID: "NEVER_RUNTIME",
                BRAND_CLASS: "CANDIDATE_ONLY"}

# 品牌候選規則（D1 保守規則）
BRAND_MIN_RECORDS = 5
BRAND_MAX_APPLICANTS = 1
BRAND_MIN_TOKEN_LEN = 3
ZH_PREFIX_MIN_LEN = 2
ZH_PREFIX_MAX_LEN = 12
ZH_PREFIX_MIN_SHARE = 0.6
GENERIC_TOKEN_MIN_APPLICANTS = 10  # 資料推導：同一英文詞被 >= 這麼多申請商使用 → 泛用詞（eval 用）

_NULL_MARKERS = frozenset({"", "null", "none", "n/a", "na", "-"})
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9&'’.\-]*")
_LETTER_RE = re.compile(r"[A-Za-z]")


# ---------------------------------------------------------------- 正規化（只用於碰撞偵測）


def normalize_alias_text(text: str | None) -> str:
    """NFKC → casefold → trim → 連續空白折成一個空格。不移除標點、不去詞、不 stemming、不翻譯。"""
    value = unicodedata.normalize("NFKC", text or "").casefold().strip()
    return re.sub(r"\s+", " ", value)


def is_null_marker(text: str | None) -> bool:
    return normalize_alias_text(text) in _NULL_MARKERS


def invalid_english_reason(text: str | None) -> str | None:
    """通用結構規則（不針對任何產品）：空值 / 無字母 / 字母數 < 4。"""
    if is_null_marker(text):
        return "EMPTY_OR_NULL_MARKER"
    letters = _LETTER_RE.findall(unicodedata.normalize("NFKC", text or ""))
    if not letters:
        return "NO_LATIN_LETTERS"
    if len(letters) < 4:
        return "FEWER_THAN_4_LETTERS"
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_review_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()[:16]}"


# ---------------------------------------------------------------- 官方快照讀取


@dataclass(frozen=True)
class SourceDataset:
    source_id: str
    dataset_title: str
    source_dataset_id: str
    source_info_id: str
    source_url: str
    retrieved_at: str
    raw_file: str
    raw_sha256: str
    normalized_file: str
    normalized_sha256: str
    rows: int


@dataclass(frozen=True)
class OfficialRecord:
    product_id: str
    source_row_key: str
    permit_number: str
    name_zh: str
    name_en: str
    category: str
    applicant: str
    source_id: str
    source_dataset_id: str
    source_url: str
    retrieved_at: str
    raw_row_sha256: str
    raw_row_number: str
    dataset_sha256: str
    source_name: str


class SnapshotError(RuntimeError):
    pass


NORMALIZED_FILES = {"SRC_DOMESTIC_CSV": "03_tfda_domestic_vitamin_normalized.csv",
                    "SRC_IMPORTED_CSV": "04_tfda_imported_tablet_capsule_normalized.csv"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(1 << 30)
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _clean(value: str | None) -> str:
    return "" if is_null_marker(value) else (value or "").strip()


def load_snapshot(snapshot_dir: Path) -> tuple[list[SourceDataset], list[OfficialRecord]]:
    """讀取 TFDA 產品覆蓋快照（唯讀）。檔案 sha256 必須與 SHA256SUMS.txt 一致，否則停止。"""
    from app.ingestion.tfda_product_coverage import canonical_key, product_id_for

    root = Path(snapshot_dir)
    sums: dict[str, str] = {}
    sums_path = root / "SHA256SUMS.txt"
    if not sums_path.exists():
        raise SnapshotError(f"缺少 {sums_path}：無法驗證來源雜湊")
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0].lower()

    sources = {r["source_id"]: r for r in _read_csv(root / "05_tfda_product_dataset_sources.csv")}
    datasets: list[SourceDataset] = []
    records: list[OfficialRecord] = []
    for source_id, norm_name in NORMALIZED_FILES.items():
        meta = sources.get(source_id)
        if meta is None:
            raise SnapshotError(f"05_tfda_product_dataset_sources.csv 缺少 {source_id}")
        raw_name = meta["download_filename"]
        for name in (raw_name, norm_name):
            actual = sha256_file(root / name)
            if sums.get(name) != actual:
                raise SnapshotError(f"{name} 的 sha256 與 SHA256SUMS.txt 不一致（{actual}）")
        if meta["sha256"].lower() != sums[raw_name]:
            raise SnapshotError(f"{raw_name} 的 sha256 與來源登記表不一致")
        rows = _read_csv(root / norm_name)
        datasets.append(SourceDataset(
            source_id=source_id, dataset_title=meta["dataset_title"], source_dataset_id=meta["source_dataset_id"],
            source_info_id=meta["source_info_id"], source_url=meta["source_url"], retrieved_at=meta["retrieved_at"],
            raw_file=raw_name, raw_sha256=sums[raw_name], normalized_file=norm_name,
            normalized_sha256=sums[norm_name], rows=len(rows)))
        for r in rows:
            permit = _clean(r.get("permit_or_registration_no"))
            info_id = _clean(r.get("source_info_id")) or meta["source_info_id"]
            key = canonical_key(info_id, permit) if permit else ""
            records.append(OfficialRecord(
                product_id=product_id_for(key) if key else "", source_row_key=_clean(r.get("source_row_key")),
                permit_number=permit, name_zh=_clean(r.get("official_name_zh")),
                name_en=(r.get("official_name_en") or "").strip(), category=_clean(r.get("product_category")),
                applicant=_clean(r.get("applicant_name")), source_id=source_id,
                source_dataset_id=_clean(r.get("source_dataset_id")) or meta["source_dataset_id"],
                source_url=_clean(r.get("source_url")) or meta["source_url"],
                retrieved_at=_clean(r.get("retrieved_at")), raw_row_sha256=_clean(r.get("raw_row_sha256")),
                raw_row_number=_clean(r.get("raw_row_number")), dataset_sha256=sums[raw_name],
                source_name=f"TFDA {meta['dataset_title']}"))
    records.sort(key=lambda r: (r.source_id, r.product_id, r.permit_number))
    return datasets, records


# ---------------------------------------------------------------- 英文品名候選


def missing_source_fields(rec: OfficialRecord) -> list[str]:
    missing = []
    for label, value in (("product_id", rec.product_id), ("source_row_key", rec.source_row_key),
                         ("source_url", rec.source_url), ("retrieved_at", rec.retrieved_at),
                         ("dataset_sha256", rec.dataset_sha256), ("name_zh", rec.name_zh),
                         ("raw_row_number", rec.raw_row_number)):
        if not value:
            missing.append(label)
    return missing


def classify_english_names(records: Iterable[OfficialRecord]) -> dict[str, Any]:
    """依保守正規化把每筆官方英文品名分成 UNIQUE / COLLIDING / INVALID。"""
    groups: dict[str, set[str]] = defaultdict(set)
    runtime_groups: dict[str, set[str]] = defaultdict(set)
    from app.normalization import normalize_name

    usable: list[tuple[OfficialRecord, str]] = []
    invalid: list[tuple[OfficialRecord, str]] = []
    for rec in records:
        reason = invalid_english_reason(rec.name_en)
        if reason:
            invalid.append((rec, reason))
            continue
        norm = normalize_alias_text(rec.name_en)
        groups[norm].add(rec.product_id)
        runtime_groups[normalize_name(rec.name_en)].add(rec.product_id)
        usable.append((rec, norm))
    return {"groups": groups, "runtime_groups": runtime_groups, "usable": usable, "invalid": invalid}


def _evidence_quote(rec: OfficialRecord) -> str:
    return f"中文品名={rec.name_zh} | 英文品名={rec.name_en}"


def _base_row(rec: OfficialRecord, overlay_version: str) -> dict[str, str]:
    return {
        "language": "en", "target_kind": "PRODUCT", "target_id": rec.product_id,
        "target_official_name": rec.name_zh, "relation": "OFFICIAL_NAME_EN",
        "evidence_tier": "OFFICIAL_RECORD_PAIR", "source_name": rec.source_name, "source_type": "OFFICIAL_OPEN_DATA",
        "source_url": rec.source_url, "source_record_id": rec.source_row_key,
        "source_locator": f"raw_row {rec.raw_row_number}; 欄位：中文品名 / 英文品名; 字號 {rec.permit_number}",
        "source_sha256": rec.dataset_sha256, "retrieved_at": rec.retrieved_at, "evidence_quote": _evidence_quote(rec),
        "support_count": "1", "proposed_by": "MECHANICAL_OFFICIAL", "decision": "NEEDS_REVIEW",
        "reviewer": "", "reviewed_at": "", "overlay_version": overlay_version, "supersedes_review_id": "",
    }


def build_product_candidates(records: list[OfficialRecord], overlay_version: str) -> list[dict[str, str]]:
    info = classify_english_names(records)
    groups, runtime_groups = info["groups"], info["runtime_groups"]
    from app.normalization import normalize_name

    zh_norm: dict[str, set[str]] = defaultdict(set)
    for rec in records:
        zh_norm[normalize_alias_text(rec.name_zh)].add(rec.product_id)

    rows: list[dict[str, str]] = []
    for rec, norm in info["usable"]:
        count = len(groups[norm])
        rt_count = len(runtime_groups[normalize_name(rec.name_en)])
        classification = UNIQUE if count == 1 else COLLIDING
        conflicts = []
        if count > 1:
            conflicts.append(f"COLLIDES_WITH_{count - 1}_OTHER_PRODUCTS")
        elif rt_count > 1:
            conflicts.append("RUNTIME_KEY_COLLISION")  # 保守正規化唯一，但去標點後與他品重複
        other_zh = zh_norm.get(norm, set()) - {rec.product_id}
        if other_zh:
            conflicts.append("EQUALS_OTHER_PRODUCT_ZH_NAME")
        row = _base_row(rec, overlay_version)
        row.update({
            "alias_kind": "PRODUCT", "alias_text_raw": rec.name_en, "alias_text_normalized": norm,
            "collision_count": str(count), "conflict_check": ";".join(conflicts) or "NONE",
            "match_policy": CLASS_POLICY[classification], "decision_reason": "",
            "classification": classification, "product_category": rec.category, "applicant_name": rec.applicant,
            "permit_number": rec.permit_number, "source_dataset_id": rec.source_dataset_id,
            "raw_row_sha256": rec.raw_row_sha256, "runtime_key_collision_count": str(rt_count),
        })
        row["review_id"] = make_review_id("PAR", "PRODUCT", norm, rec.product_id, "OFFICIAL_NAME_EN")
        missing = missing_source_fields(rec)
        if missing:
            row["decision_reason"] = "MISSING_SOURCE_METADATA:" + ",".join(missing)
        rows.append(row)
    for rec, reason in info["invalid"]:
        if is_null_marker(rec.name_en):
            continue  # 官方沒有英文品名：沒有可審核的別名文字（只計數，不產生列）
        row = _base_row(rec, overlay_version)
        norm = normalize_alias_text(rec.name_en)
        row.update({
            "alias_kind": "PRODUCT", "alias_text_raw": rec.name_en, "alias_text_normalized": norm,
            "collision_count": "0", "conflict_check": f"INVALID:{reason}", "match_policy": CLASS_POLICY[INVALID],
            "decision_reason": f"INVALID_OR_GENERIC:{reason}", "classification": INVALID,
            "product_category": rec.category, "applicant_name": rec.applicant, "permit_number": rec.permit_number,
            "source_dataset_id": rec.source_dataset_id, "raw_row_sha256": rec.raw_row_sha256,
            "runtime_key_collision_count": "0",
        })
        row["review_id"] = make_review_id("PAR", "PRODUCT", norm, rec.product_id, "OFFICIAL_NAME_EN")
        rows.append(row)
    rows.sort(key=lambda r: (r["classification"], r["alias_text_normalized"], r["target_id"]))
    assign_review_batches(rows)
    return rows


def assign_review_batches(rows: list[dict[str, str]], size: int = REVIEW_BATCH_SIZE) -> None:
    """UNIQUE 先審；每 size 列一批（依已排序順序，deterministic）。其餘分類批號前綴不同。"""
    counters: Counter = Counter()
    for row in rows:
        cls = row.get("classification", "")
        index = counters[cls]
        counters[cls] += 1
        row["review_batch"] = f"{cls.split('_')[0]}-{index // size + 1:03d}"


# ---------------------------------------------------------------- 品牌候選（CANDIDATE_ONLY）


def english_leading_token(name_en: str) -> str | None:
    m = _TOKEN_RE.match(unicodedata.normalize("NFKC", name_en or "").strip())
    return m.group(0).rstrip(".-'’") if m else None


def zh_majority_prefix(names: list[str], min_share: float = ZH_PREFIX_MIN_SHARE,
                       min_len: int = ZH_PREFIX_MIN_LEN, max_len: int = ZH_PREFIX_MAX_LEN) -> tuple[str, int]:
    """多數（>= min_share 且至少 BRAND_MIN_RECORDS 筆）官方中文品名共同的最長前綴；沒有則 ('', 0)。"""
    clean = [unicodedata.normalize("NFKC", n).strip() for n in names if n]
    need = max(BRAND_MIN_RECORDS, int(min_share * len(clean) + 0.9999))
    best, best_support = "", 0
    for length in range(min_len, max_len + 1):
        counts = Counter(n[:length] for n in clean if len(n) >= length)
        if not counts:
            break
        prefix, support = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if support < need:
            break
        best, best_support = prefix, support
    return best.strip(), best_support


def build_brand_candidates(records: list[OfficialRecord], overlay_version: str) -> list[dict[str, str]]:
    """英文品名首詞 ↔ 同一批官方紀錄的中文品名前綴。全部 CANDIDATE_ONLY，不產生所有權關係。"""
    by_token: dict[str, list[OfficialRecord]] = defaultdict(list)
    # 該詞出現在任何英文品名（不限首詞）的 申請商 → 紀錄數
    token_records: dict[str, Counter] = defaultdict(Counter)
    for rec in records:
        if invalid_english_reason(rec.name_en):
            continue
        lead = english_leading_token(rec.name_en)
        if lead:
            by_token[lead.casefold()].append(rec)
        for tok in {t.casefold().rstrip(".-'’") for t in _TOKEN_RE.findall(unicodedata.normalize("NFKC", rec.name_en))}:
            token_records[tok][rec.applicant] += 1

    rows: list[dict[str, str]] = []
    for token, recs in by_token.items():
        if len(recs) < BRAND_MIN_RECORDS:
            continue  # 低於 5 筆的長尾不輸出（只計入 raw_first_token_count）
        applicants = sorted({r.applicant for r in recs})
        other_records = sum(n for a, n in token_records[token].items() if a not in set(applicants))
        failures = []
        if len(applicants) > BRAND_MAX_APPLICANTS:
            failures.append(f"APPLICANTS>{BRAND_MAX_APPLICANTS}")
        if len(token) < BRAND_MIN_TOKEN_LEN:
            failures.append("TOKEN_TOO_SHORT")
        if other_records:
            failures.append("TOKEN_USED_BY_OTHER_APPLICANTS")
        if any(not r.applicant for r in recs):
            failures.append("MISSING_APPLICANT")
        prefix, support = zh_majority_prefix([r.name_zh for r in recs])
        first = sorted(recs, key=lambda r: (r.product_id, r.permit_number))[0]
        passes = not failures
        row = {
            "alias_kind": "BRAND", "alias_text_raw": english_leading_token(first.name_en) or token,
            "alias_text_normalized": normalize_alias_text(token), "language": "en",
            "target_kind": "APPLICANT_COMPANY", "target_id": applicants[0] if len(applicants) == 1 else "",
            "target_official_name": " / ".join(applicants[:3]), "relation": "BRAND_CANDIDATE_OF",
            "evidence_tier": "OFFICIAL_RECORD_INFERENCE", "source_name": first.source_name,
            "source_type": "OFFICIAL_OPEN_DATA", "source_url": first.source_url,
            "source_record_id": first.source_row_key,
            "source_locator": f"{len(recs)} 筆官方紀錄英文品名首詞；欄位：中文品名 / 英文品名 / 申請商名稱",
            "source_sha256": first.dataset_sha256, "retrieved_at": first.retrieved_at,
            "evidence_quote": _evidence_quote(first), "support_count": str(len(recs)),
            "collision_count": str(len(applicants)), "conflict_check": "NONE" if passes else ";".join(failures),
            "match_policy": "CANDIDATE_ONLY", "proposed_by": "MECHANICAL_OFFICIAL", "decision": "NEEDS_REVIEW",
            "decision_reason": "" if passes else "RULE_FAILED:" + ";".join(failures), "reviewer": "", "reviewed_at": "",
            "overlay_version": overlay_version, "supersedes_review_id": "", "classification": BRAND_CLASS,
            "zh_brand_candidate": prefix, "zh_prefix_support": str(support), "applicant_count": str(len(applicants)),
            "other_applicant_records": str(other_records), "passes_rule": "true" if passes else "false",
            "rule_failures": ";".join(failures),
            "example_permits": ";".join(sorted({r.permit_number for r in recs})[:5]),
            "example_names": " | ".join(sorted({r.name_zh for r in recs})[:3]),
            "source_dataset_id": first.source_dataset_id,
        }
        row["review_id"] = make_review_id("PAR", "BRAND", row["alias_text_normalized"], row["target_id"] or "MULTI")
        rows.append(row)
    rows.sort(key=lambda r: (r["passes_rule"] != "true", -int(r["support_count"]), r["alias_text_normalized"]))
    assign_review_batches(rows, size=50)
    return rows


def raw_first_token_count(records: Iterable[OfficialRecord]) -> int:
    return len({english_leading_token(r.name_en).casefold() for r in records
                if not invalid_english_reason(r.name_en) and english_leading_token(r.name_en)})


# ---------------------------------------------------------------- CSV I/O


def write_review_csv(path: Path, rows: list[dict[str, str]], extra: tuple[str, ...] = ()) -> str:
    columns = REVIEW_COLUMNS + tuple(c for c in extra if c not in REVIEW_COLUMNS)
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    return sha256_file(path)


def read_review_csv(path: Path) -> list[dict[str, str]]:
    return _read_csv(path)


# ---------------------------------------------------------------- 計數與 manifest


def classification_counts(product_rows: list[dict[str, str]], records: list[OfficialRecord],
                          brand_rows: list[dict[str, str]]) -> dict[str, Any]:
    by_class = Counter(r["classification"] for r in product_rows)
    norm_by_class: dict[str, set[str]] = defaultdict(set)
    for r in product_rows:
        norm_by_class[r["classification"]].add(r["alias_text_normalized"])
    colliding_groups = {r["alias_text_normalized"] for r in product_rows if r["classification"] == COLLIDING}
    usable_rows = [r for r in product_rows if r["classification"] in (UNIQUE, COLLIDING)]
    missing_en = sum(1 for rec in records if is_null_marker(rec.name_en))
    passing = [r for r in brand_rows if r["passes_rule"] == "true"]
    return {
        "official_records": len(records),
        "records_without_english_name": missing_en,
        "official_english_names_total": len(usable_rows),
        "unique_normalized_english_names": len({r["alias_text_normalized"] for r in usable_rows}),
        "unique_products_covered": len({r["target_id"] for r in usable_rows}),
        "UNIQUE_OFFICIAL_ENGLISH": {"rows": by_class[UNIQUE], "normalized_names": len(norm_by_class[UNIQUE])},
        "COLLIDING_OFFICIAL_ENGLISH": {"rows": by_class[COLLIDING], "normalized_names": len(colliding_groups),
                                       "products": len({r["target_id"] for r in product_rows
                                                        if r["classification"] == COLLIDING})},
        "INVALID_OR_GENERIC": {"rows": by_class[INVALID]},
        "unique_rows_with_runtime_key_collision": sum(
            1 for r in product_rows if r["classification"] == UNIQUE and int(r["runtime_key_collision_count"]) > 1),
        "brand_candidates_total": len(brand_rows),
        "brand_candidates_passing_rule": len(passing),
        "brand_passing_with_zh_prefix": sum(1 for r in passing if r["zh_brand_candidate"]),
    }


def count_decisions(rows: Iterable[dict[str, str]]) -> dict[str, int]:
    c = Counter(r.get("decision", "") for r in rows)
    return {d: c.get(d, 0) for d in DECISIONS}


MANIFEST_REQUIRED = ("overlay_version", "created_at", "builder_version", "validator_version", "source_datasets",
                     "files", "candidate_counts", "classification_counts", "policies")


def build_manifest(*, overlay_version: str, created_at: str, datasets: list[SourceDataset],
                   files: dict[str, dict[str, Any]], counts: dict[str, Any], decisions: dict[str, int],
                   sampling_note: str = "") -> dict[str, Any]:
    return {
        "overlay_version": overlay_version, "created_at": created_at,
        "builder_version": BUILDER_VERSION, "validator_version": VALIDATOR_VERSION,
        "scope": "Taiwan Phase 1 only（TW）；不含任何國際產品目錄",
        "source_datasets": [{
            "source_id": d.source_id, "dataset_title": d.dataset_title, "source_dataset_id": d.source_dataset_id,
            "source_info_id": d.source_info_id, "source_url": d.source_url, "retrieved_at": d.retrieved_at,
            "raw_file": d.raw_file, "raw_sha256": d.raw_sha256, "normalized_file": d.normalized_file,
            "normalized_sha256": d.normalized_sha256, "rows": d.rows} for d in datasets],
        "files": files,
        "candidate_counts": {"product_rows": counts["UNIQUE_OFFICIAL_ENGLISH"]["rows"]
                             + counts["COLLIDING_OFFICIAL_ENGLISH"]["rows"] + counts["INVALID_OR_GENERIC"]["rows"],
                             "brand_rows": counts["brand_candidates_total"], "review_decisions": decisions},
        "classification_counts": counts,
        "policies": {
            "normalization": "NFKC + casefold + trim + collapse whitespace（只用於碰撞偵測）",
            "UNIQUE_OFFICIAL_ENGLISH": "EXACT_REVIEW_REQUIRED（尚非 runtime-active）",
            "COLLIDING_OFFICIAL_ENGLISH": "CANDIDATE_ONLY", "BRAND": "CANDIDATE_ONLY",
            "INVALID_OR_GENERIC": "NEVER_RUNTIME", "runtime_active_alias_file": "none（reviewed_aliases.csv 無已核准列）",
            "brand_rule": f">= {BRAND_MIN_RECORDS} 筆官方紀錄、單一申請商、英文詞未被其他申請商的英文品名使用"},
        "notes": sampling_note,
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> str:
    Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return sha256_file(path)


# ---------------------------------------------------------------- validator（不修改任何紀錄）


@dataclass(frozen=True)
class Issue:
    severity: str  # ERROR | WARNING
    code: str
    row_id: str
    message: str


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    rows_checked: int = 0

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self, severity: str = "ERROR") -> set[str]:
        return {i.code for i in self.issues if i.severity == severity}

    def counts(self) -> dict[str, int]:
        return dict(Counter(f"{i.severity}:{i.code}" for i in self.issues))


_PROVENANCE_FIELDS = ("source_name", "source_type", "source_url", "source_record_id", "source_locator",
                      "source_sha256", "retrieved_at", "evidence_quote")


def _is_official_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS)


def validate_rows(rows: list[dict[str, str]], *, official_names: dict[str, set[str]] | None = None,
                  existing_review_ids: Iterable[str] = ()) -> ValidationReport:
    """檢查審核列。official_names：正規化後的官方產品名稱 → 對應 target_id 集合（用來擋「別名 = 他品官方名稱」）。"""
    rep = ValidationReport(rows_checked=len(rows))

    def err(code: str, row: dict[str, str], msg: str) -> None:
        rep.issues.append(Issue("ERROR", code, row.get("review_id", "?"), msg))

    def warn(code: str, row: dict[str, str], msg: str) -> None:
        rep.issues.append(Issue("WARNING", code, row.get("review_id", "?"), msg))

    ids = Counter(r.get("review_id", "") for r in rows)
    known_ids = set(ids) | set(existing_review_ids)
    targets_by_alias: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in rows:
        if r.get("match_policy") == "EXACT_REVIEW_REQUIRED":
            targets_by_alias[(r.get("alias_kind", ""), r.get("alias_text_normalized", ""))].add(r.get("target_id", ""))

    for r in rows:
        rid = r.get("review_id", "")
        if not rid:
            err("MISSING_REVIEW_ID", r, "review_id 空白")
        elif ids[rid] > 1:
            err("DUPLICATE_REVIEW_ID", r, f"review_id 重複 {ids[rid]} 次")
        kind, decision, policy = r.get("alias_kind", ""), r.get("decision", ""), r.get("match_policy", "")
        if kind not in ALIAS_KINDS:
            err("INVALID_ALIAS_KIND", r, f"alias_kind={kind!r}")
        if decision not in DECISIONS:
            err("INVALID_DECISION", r, f"decision={decision!r}")
        if policy not in MATCH_POLICIES:
            err("INVALID_MATCH_POLICY", r, f"match_policy={policy!r}")
        if r.get("proposed_by") not in PROPOSED_BY:
            err("INVALID_PROPOSED_BY", r, f"proposed_by={r.get('proposed_by')!r}")
        if r.get("evidence_tier") not in EVIDENCE_TIERS:
            err("INVALID_EVIDENCE_TIER", r, f"evidence_tier={r.get('evidence_tier')!r}")
        if r.get("alias_text_normalized") != normalize_alias_text(r.get("alias_text_raw")):
            err("NORMALIZED_MISMATCH", r, "alias_text_normalized 與 alias_text_raw 的正規化結果不一致")
        if not (r.get("alias_text_raw") or "").strip():
            err("EMPTY_ALIAS", r, "alias_text_raw 空白")

        missing = [f for f in _PROVENANCE_FIELDS if not (r.get(f) or "").strip()]
        if missing:
            pending = decision == "NEEDS_REVIEW" and (r.get("decision_reason") or "").startswith("MISSING_SOURCE_METADATA")
            (warn if pending else err)("MISSING_PROVENANCE", r, "缺少來源欄位：" + ",".join(missing))

        official = (r.get("evidence_tier") in OFFICIAL_EVIDENCE_TIERS
                    and r.get("source_type") in OFFICIAL_SOURCE_TYPES and _is_official_url(r.get("source_url", ""))
                    and (r.get("evidence_quote") or "").strip() and not missing)
        try:
            support = int(r.get("support_count") or 0)
        except ValueError:
            support = 0
            err("INVALID_SUPPORT_COUNT", r, f"support_count={r.get('support_count')!r}")
        if decision == "ACCEPT":
            if not official or support < 1:
                err("ACCEPT_WITHOUT_OFFICIAL_EVIDENCE", r, "ACCEPT 需要台灣官方來源、官方證據層級、引文與 support_count >= 1")
            if r.get("proposed_by") == "AI_SUGGESTED" and not official:
                err("AI_SUGGESTED_ACCEPT_WITHOUT_OFFICIAL_EVIDENCE", r, "AI 建議不得在沒有官方證據時 ACCEPT")
            if policy == "NEVER_RUNTIME":
                err("ACCEPT_NEVER_RUNTIME", r, "match_policy=NEVER_RUNTIME 不得 ACCEPT")
            if not (r.get("reviewer") or "").strip() or not (r.get("reviewed_at") or "").strip():
                err("ACCEPT_WITHOUT_REVIEWER", r, "ACCEPT 需要 reviewer 與 reviewed_at")

        try:
            collisions = int(r.get("collision_count") or 0)
        except ValueError:
            collisions = 0
            err("INVALID_COLLISION_COUNT", r, f"collision_count={r.get('collision_count')!r}")
        if kind in ("PRODUCT",):
            if policy == "EXACT_REVIEW_REQUIRED" and collisions != 1:
                err("UNIQUE_MARKED_WITH_COLLISION", r, f"標為 unique 但 collision_count={collisions}")
            if policy == "EXACT_REVIEW_REQUIRED" and len(targets_by_alias[(kind, r.get("alias_text_normalized", ""))]) > 1:
                err("UNIQUE_ALIAS_CONFLICTING_TARGETS", r, "同一正規化別名對應多個目標卻標為 unique")
            if collisions > 1 and policy not in ("CANDIDATE_ONLY", "NEVER_RUNTIME"):
                err("COLLIDING_NOT_CANDIDATE_ONLY", r, "碰撞別名必須是 CANDIDATE_ONLY")
            if r.get("classification") == INVALID and policy != "NEVER_RUNTIME":
                err("GENERIC_NOT_NEVER_RUNTIME", r, "INVALID_OR_GENERIC 必須是 NEVER_RUNTIME")
            if official_names is not None:
                owners = official_names.get(r.get("alias_text_normalized", ""), set()) - {r.get("target_id", "")}
                if owners and policy == "EXACT_REVIEW_REQUIRED":
                    err("ALIAS_EQUALS_OTHER_PRODUCT_NAME", r, "別名與另一產品的官方品名完全相同")
            if "RUNTIME_KEY_COLLISION" in (r.get("conflict_check") or ""):
                if decision == "ACCEPT":
                    err("ACCEPT_RUNTIME_KEY_COLLISION", r, "去標點後與其他產品的英文品名相同，不得 ACCEPT 為唯一別名")
                else:
                    warn("RUNTIME_KEY_COLLISION", r, "去標點後與其他產品的英文品名相同；審核時需確認")
        if kind == "BRAND":
            if policy != "CANDIDATE_ONLY":
                err("BRAND_NOT_CANDIDATE_ONLY", r, f"品牌 match_policy 必須是 CANDIDATE_ONLY（目前 {policy!r}）")
            if (r.get("relation") or "").upper() in ("OWNS_BRAND", "OFFICIAL_BRAND", "BRAND_OF"):
                err("BRAND_OWNERSHIP_RELATION", r, "品牌只能是候選關係（BRAND_CANDIDATE_OF），不得標示所有權 / 官方品牌")

        sup = r.get("supersedes_review_id") or ""
        if sup and sup not in known_ids:
            err("UNKNOWN_SUPERSEDES", r, f"supersedes_review_id={sup!r} 不存在")
    return rep


def validate_manifest(manifest: dict[str, Any], directory: Path | None = None,
                      other_manifests: Iterable[dict[str, Any]] = ()) -> ValidationReport:
    rep = ValidationReport()

    def err(code: str, msg: str) -> None:
        rep.issues.append(Issue("ERROR", code, "manifest", msg))

    if not isinstance(manifest, dict):
        err("MALFORMED_MANIFEST", "manifest 不是 JSON object")
        return rep
    for key in MANIFEST_REQUIRED:
        if key not in manifest:
            err("MALFORMED_MANIFEST", f"缺少欄位 {key}")
    if rep.errors:
        return rep
    if not re.fullmatch(r"\d+\.\d+", str(manifest["overlay_version"])):
        err("MALFORMED_MANIFEST", "overlay_version 格式應為 主.次")
    sources = manifest["source_datasets"]
    if not isinstance(sources, list) or not sources:
        err("MALFORMED_MANIFEST", "source_datasets 空白")
        return rep
    for s in sources:
        if not re.fullmatch(r"[0-9a-f]{64}", str(s.get("raw_sha256", ""))):
            err("MALFORMED_MANIFEST", f"{s.get('source_id')} 的 raw_sha256 格式錯誤")
    if directory is not None:
        for name, meta in manifest["files"].items():
            path = Path(directory) / name
            if not path.exists():
                err("MANIFEST_FILE_MISSING", f"{name} 不存在")
            elif sha256_file(path) != meta.get("sha256"):
                err("MANIFEST_HASH_MISMATCH", f"{name} 的 sha256 與 manifest 不一致")
    mine = {s["source_id"]: s["raw_sha256"] for s in sources}
    for other in other_manifests:
        if other is manifest or str(other.get("overlay_version")) != str(manifest["overlay_version"]):
            continue
        for s in other.get("source_datasets", []):
            if s["source_id"] in mine and mine[s["source_id"]] != s["raw_sha256"]:
                err("SOURCE_HASH_CHANGED_WITHOUT_VERSION",
                    f"{s['source_id']} 的來源雜湊改變，但 overlay_version {manifest['overlay_version']} 沒有變")
    return rep


def official_name_index(rows: Iterable[dict[str, str]]) -> dict[str, set[str]]:
    """正規化官方產品名稱（中文）→ target_id；供「別名 = 他品官方名稱」檢查。"""
    index: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("alias_kind") == "PRODUCT" and r.get("target_official_name"):
            index[normalize_alias_text(r["target_official_name"])].add(r["target_id"])
    return index
