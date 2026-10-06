"""Reviewed Taiwan Drug Chinese Alias Overlay（runtime；唯讀、deterministic）。

繁體中文藥品成分名稱 → 已審核的 canonical identity（DrugSubstance 或台灣官方藥品成分名錄項目）。

* 只有 review_status = VERIFIED_OFFICIAL，或「台灣官方來源直接說明 identity 且無衝突」的 SUPPORTED_OFFICIAL，
  且 match_policy = EXACT_NFKC_CASEFOLD_WHITESPACE 的列參與自動比對；NEEDS_REVIEW / REJECTED 一律不比對
  （AI / LLM 翻譯只能產生 NEEDS_REVIEW 候選，不會變成 runtime-active alias）（不猜、不翻譯、不用相似度、不做 substring / prefix 比對、不轉簡繁）。
* 比對鍵只消除格式差異（NFKC、大小寫、空白標點），與既有 DrugSubstance 比對相同。
* 同一個鍵對到多個不同 identity → 兩邊都不採用（歧義，視為沒有 alias）。
* 這個 overlay 只提供「identity」：不建立 interaction / evidence，也不影響 eligibility 與風險判斷。
* 檔案缺失 / 格式錯誤 → 空 index（行為與沒有 overlay 時完全相同），不影響既有流程。
"""

import csv
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.normalization import normalize_name

logger = logging.getLogger(__name__)

STATUS_VERIFIED = "VERIFIED_OFFICIAL"
STATUS_SUPPORTED = "SUPPORTED_OFFICIAL"
ACTIVE_STATUSES = (STATUS_VERIFIED, STATUS_SUPPORTED)
MATCH_EXACT = "EXACT_NFKC_CASEFOLD_WHITESPACE"
TARGET_DRUG = "DRUG_SUBSTANCE"
TARGET_CATALOG = "TAIWAN_DRUG_INGREDIENT"
DEFAULT_OVERLAY_PATH = Path(__file__).resolve().parents[2] / "data" / "taiwan_drug_alias_overlay" / "2026-10-06" / "reviewed_drug_zh_aliases.csv"


@dataclass(frozen=True)
class ZhAliasHit:
    alias_id: str
    alias_text: str
    target_kind: str
    canonical_entity_id: str | None  # DrugSubstance ID（target_kind = DRUG_SUBSTANCE）
    taiwan_drug_ingredient_code: str | None  # 名錄 ingredient_code（target_kind = TAIWAN_DRUG_INGREDIENT）
    official_name: str | None  # 名錄官方成分名稱（原樣英文）
    source_locator: str
    review_status: str = STATUS_VERIFIED

    @property
    def target_id(self) -> str:
        return (self.canonical_entity_id or self.taiwan_drug_ingredient_code or "")


def alias_key(text: str | None) -> str:
    return normalize_name(text or "")


def _valid_verified(row: dict[str, str]) -> bool:
    """VERIFIED_OFFICIAL 列的結構不變量；不符 → 不參與比對。"""
    if not (row.get("alias_text_zh_tw") or "").strip() or not alias_key(row["alias_text_zh_tw"]):
        return False
    if not all((row.get(k) or "").strip() for k in ("official_source", "source_locator", "retrieved_at")):
        return False
    try:
        if row.get("review_status") == STATUS_VERIFIED and int(row.get("support_permit_count") or 0) < 1:
            return False
    except ValueError:
        return False
    if row.get("review_status") == STATUS_SUPPORTED:  # 台灣官方來源直接說明 identity：需要來源網域 / 引文 / 有效成分 / 無衝突
        from urllib.parse import urlparse

        host = (urlparse(row.get("evidence_source_url") or "").hostname or "").lower()
        if not (host == "gov.tw" or host.endswith(".gov.tw")):
            return False
        if not all((row.get(k) or "").strip() for k in ("identity_statement", "active_ingredient_in_source")) or row.get("conflict_check") != "NONE":
            return False
        if (row.get("candidate_origin") or "") == "AI_TRANSLATION" and not (row.get("evidence_source_name") or "").strip():
            return False
    kind = row.get("target_kind")
    if kind == TARGET_DRUG:
        return bool((row.get("canonical_entity_id") or "").strip())
    if kind == TARGET_CATALOG:
        return bool((row.get("taiwan_drug_ingredient_code") or "").strip() and (row.get("taiwan_drug_ingredient_official_name") or "").strip())
    return False


class DrugZhAliasIndex:
    def __init__(self, rows: Iterable[dict[str, str]] = ()):
        by_key: dict[str, dict[tuple[str, str], ZhAliasHit]] = {}
        for row in rows:
            if row.get("review_status") not in ACTIVE_STATUSES or row.get("match_policy") != MATCH_EXACT:
                continue  # NEEDS_REVIEW / REJECTED / 非 exact policy：不參與自動比對
            if not _valid_verified(row):
                logger.warning("drug_zh_alias_row_invalid", extra={"alias_id": row.get("alias_id")})
                continue
            hit = ZhAliasHit(
                alias_id=row.get("alias_id") or "", alias_text=row["alias_text_zh_tw"].strip(), target_kind=row["target_kind"],
                canonical_entity_id=(row.get("canonical_entity_id") or "").strip() or None,
                taiwan_drug_ingredient_code=(row.get("taiwan_drug_ingredient_code") or "").strip() or None,
                official_name=(row.get("taiwan_drug_ingredient_official_name") or "").strip() or None,
                source_locator=row["source_locator"], review_status=row["review_status"])
            by_key.setdefault(alias_key(hit.alias_text), {})[(hit.target_kind, hit.target_id)] = hit
        self._hits: dict[str, ZhAliasHit] = {}
        for key, targets in by_key.items():
            if len(targets) == 1:
                self._hits[key] = next(iter(targets.values()))
            else:
                logger.warning("drug_zh_alias_ambiguous_key_ignored", extra={"targets": len(targets)})

    def __len__(self) -> int:
        return len(self._hits)

    def lookup(self, text: str | None) -> ZhAliasHit | None:
        """exact（NFKC / 大小寫 / 空白標點）比對；沒有 → None。"""
        key = alias_key(text)
        return self._hits.get(key) if key else None

    def catalog_entry_by_code(self, code: str) -> ZhAliasHit | None:
        return next((h for h in self._hits.values() if h.target_kind == TARGET_CATALOG and h.taiwan_drug_ingredient_code == code), None)

    def entries(self, target_kind: str | None = None) -> list[ZhAliasHit]:
        return [h for h in self._hits.values() if target_kind is None or h.target_kind == target_kind]


def load_index(path: Path | str) -> DrugZhAliasIndex:
    with open(path, encoding="utf-8", newline="") as fh:
        return DrugZhAliasIndex(csv.DictReader(fh))


@lru_cache(maxsize=1)
def load_default_index() -> DrugZhAliasIndex:
    """預設 overlay；檔案缺失 / 讀取失敗 → 空 index（等同沒有 overlay）。"""
    try:
        return load_index(DEFAULT_OVERLAY_PATH)
    except Exception:  # noqa: BLE001 - overlay 是附加資料層，失敗不得影響既有 interaction 流程
        logger.warning("drug_zh_alias_overlay_unavailable")
        return DrugZhAliasIndex()
