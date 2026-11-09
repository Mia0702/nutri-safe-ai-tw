"""已審核品牌（D4 ACCEPT）輔助產品「候選」取回（D5）。

* 只負責：載入並驗證檔案型 overlay、在查詢文字中辨識「已審核品牌詞」、判定某筆官方產品是否屬於該品牌的候選集合。
  不查資料庫（資料庫查詢由 repo 的 `search_products_by_applicant` 負責）、不產生任何使用者回答。
* **品牌只縮小候選範圍，永遠不確認產品**：品牌關係是「英文詞與申請商之間已審核的品牌別名」（CANDIDATE_ONLY），
  不是品牌所有權（沒有 OWNS_BRAND）、不是產品身份。
* fail closed：manifest / 雜湊 / 審核欄位任何一項驗證失敗，整份 overlay 停用（brand 輔助關閉，既有產品查證照常運作），
  並保留 issues 供回報；不會默默略過無效的 ACCEPT 列。
* 只有 decision = ACCEPT 且審核者是真人（非 AI / 自動化帳號）的列會生效；REJECT / NEEDS_REVIEW / 未審核的詞完全不參與。
* 不含任何特定品牌、產品名稱；資格完全來自 overlay 檔的審核狀態。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.english_name import conservative_key
from app.normalization import normalize_name
from app.ingestion.brand_alias_review import REVIEWED_BRAND_FILE, token_key, validate_reviewed_brand_rows
from app.ingestion.product_alias_review import english_leading_token, read_review_csv, sha256_file, validate_manifest

logger = logging.getLogger("nutri_safe_ai.brand_assist")

MANIFEST_FILE = "overlay_manifest.json"
ACCEPT = "ACCEPT"
CANDIDATE_ONLY = "CANDIDATE_ONLY"
BRAND_RELATION = "BRAND_CANDIDATE_OF"
APPLICANT_TARGET = "APPLICANT_COMPANY"
# 品牌輔助取回的候選上限（只用在候選清單；resolver 另有顯示上限）
MAX_BRAND_CANDIDATES = 20
# 單一申請商的產品列超過此數量時看不完整個候選集合 → 不輔助（repo 以 complete=False 回報）
MAX_APPLICANT_ROWS = 600
MIN_REMAINDER_LEN = 2

# 審核者欄位若屬於 AI / 自動化身分就不是「人工審核」：以 token 比對（不是子字串），避免誤傷一般姓名
_AUTOMATED_REVIEWER_TOKENS = frozenset({
    "claude", "gpt", "chatgpt", "openai", "llm", "gemini", "copilot", "ai", "bot", "auto", "automated",
    "mechanical", "model", "assistant", "system", "script", "test", "synthetic", "unreviewed", "none", "unknown",
})
_TOKEN_SPLIT = re.compile(r"[^A-Za-z]+")
_PROVENANCE = ("source_name", "source_type", "source_url", "source_record_id", "retrieved_at", "target_official_name")
_ALNUM = re.compile(r"[a-z0-9]")


def is_human_reviewer(reviewer: str | None) -> bool:
    text = (reviewer or "").strip()
    if not text:
        return False
    return not any(t.lower() in _AUTOMATED_REVIEWER_TOKENS for t in _TOKEN_SPLIT.split(text) if t)


@dataclass(frozen=True)
class ReviewedBrand:
    """一筆通過 runtime 驗證的 ACCEPT 品牌別名（候選用；不是所有權）。"""

    review_id: str
    token: str            # 原始英文品牌詞
    key: str              # conservative key（查詢比對用）
    applicant: str        # 官方申請商名稱（候選集合的篩選條件；不是所有權斷言）
    zh_brand: str         # 官方中文品名共同前綴（或同一英文詞）
    support_count: int
    reviewer: str
    reviewed_at: str
    overlay_version: str
    source_record_id: str


@dataclass(frozen=True)
class BrandMention:
    """查詢中辨識到的已審核品牌＋剩餘的產品文字（空字串 = 品牌-only）。"""

    brand: ReviewedBrand
    remainder: str

    @property
    def brand_only(self) -> bool:
        return not self.remainder


@dataclass(frozen=True)
class OverlayStatus:
    active: bool
    issues: tuple[str, ...] = ()
    overlay_version: str | None = None
    accepted_brands: int = 0
    ignored_rows: int = 0     # 非 ACCEPT 列（REJECT / NEEDS_REVIEW）：不參與
    load_ms: float = 0.0
    directory: str | None = None


class ReviewedBrandOverlay:
    """已驗證的 ACCEPT 品牌集合；不可變，可跨執行緒共用。"""

    def __init__(self, brands: tuple[ReviewedBrand, ...], status: OverlayStatus):
        self.brands = brands
        self.status = status
        self._by_key = {b.key: b for b in brands}

    @property
    def active(self) -> bool:
        return self.status.active and bool(self.brands)

    def find_mention(self, query: str | None) -> BrandMention | None:
        """查詢文字中恰好出現一個已審核品牌詞（詞邊界；不做子字串 / 模糊比對）。出現多個不同品牌 → None（不猜）。"""
        if not self.active:
            return None
        text = conservative_key(query)
        if not text:
            return None
        found: list[tuple[ReviewedBrand, int]] = []
        for brand in self.brands:
            pos = _find_token(text, brand.key)
            if pos >= 0:
                found.append((brand, pos))
        # 較長的詞可能包含較短的詞（例如 "ab" ⊂ "abc"）：只保留沒有被其他命中完整涵蓋的命中
        found = [(b, p) for b, p in found
                 if not any(o.key != b.key and b.key in o.key and q <= p and p + len(b.key) <= q + len(o.key)
                            for o, q in found)]
        if len({b.key for b, _ in found}) != 1:
            return None
        brand, pos = found[0]
        remainder = (text[:pos] + " " + text[pos + len(brand.key):])
        remainder = re.sub(r"[\s\-_/,.，。、:：;；()（）\[\]【】「」『』\"'’]+", " ", remainder).strip()
        return BrandMention(brand=brand, remainder=remainder)


def _find_token(text: str, key: str) -> int:
    """key 在 text 中的詞邊界位置（前後都不是英數字；CJK 緊鄰允許）；找不到 -1。"""
    start = 0
    while True:
        pos = text.find(key, start)
        if pos < 0:
            return -1
        before = text[pos - 1] if pos > 0 else ""
        after = text[pos + len(key)] if pos + len(key) < len(text) else ""
        if not (before and _ALNUM.match(before)) and not (after and _ALNUM.match(after)):
            return pos
        start = pos + 1


# ---------------------------------------------------------------- 候選集合判定（純函式）


def is_brand_member(brand: ReviewedBrand, row: dict[str, Any]) -> bool:
    """官方產品列是否屬於該品牌的候選集合：同一申請商，且官方中文品名以審核過的中文品牌前綴開頭，
    或官方英文品名首詞就是該品牌詞（審核證據的同一組欄位）。"""
    if (row.get("company_name") or "") != brand.applicant or not row.get("is_official"):
        return False
    zh = conservative_key(row.get("product_name_zh"))
    if brand.zh_brand and zh.startswith(conservative_key(brand.zh_brand)):
        return True
    lead = english_leading_token(row.get("product_name_en") or "")
    return bool(lead) and token_key(lead) == token_key(brand.token)


def remainder_parts(remainder: str) -> list[str]:
    return [p for p in remainder.split() if p]


def supports_remainder(remainder: str, row: dict[str, Any]) -> bool:
    """品牌之外的產品文字：每一段都必須出現在這筆官方產品的中文或英文品名中（只在品牌候選集合內比對；
    兩側都以既有的 normalize_name 正規化，所以空白 / 標點差異不影響）。這只是候選縮小，不是身份比對。"""
    raw_parts = remainder_parts(remainder)
    if not raw_parts:
        return True
    parts = [p for p in (normalize_name(x) for x in raw_parts) if p]   # 純標點的片段（例如 "&"）不當作條件
    if not parts:
        return False
    zh, en = normalize_name(row.get("product_name_zh") or ""), normalize_name(row.get("product_name_en") or "")
    return all(p in zh or p in en for p in parts)


# ---------------------------------------------------------------- overlay 載入與驗證


def _parse_date(value: str) -> bool:
    try:
        date.fromisoformat((value or "").strip()[:10])
        return len((value or "").strip()) >= 10
    except ValueError:
        return False


def _row_issues(row: dict[str, str], manifest_version: str) -> list[str]:
    rid = row.get("review_id", "?")
    out: list[str] = []
    if (row.get("match_policy") or "") != CANDIDATE_ONLY:
        out.append(f"{rid}: match_policy 必須是 {CANDIDATE_ONLY}")
    if not is_human_reviewer(row.get("reviewer")):
        out.append(f"{rid}: reviewer 不是人工審核者（{row.get('reviewer') or '空白'}）")
    if not _parse_date(row.get("reviewed_at", "")):
        out.append(f"{rid}: reviewed_at 不是有效日期")
    if (row.get("alias_kind") or "") != "BRAND" or (row.get("relation") or "") != BRAND_RELATION:
        out.append(f"{rid}: 不是品牌別名關係（alias_kind / relation）")
    if (row.get("target_kind") or "") != APPLICANT_TARGET:
        out.append(f"{rid}: target_kind 必須是 {APPLICANT_TARGET}")
    if (row.get("proposed_by") or "") == "AI_SUGGESTED":
        out.append(f"{rid}: AI 單獨提出的品牌不得生效")
    if (row.get("descriptor_flag") or "NONE") != "NONE" or (row.get("conflict_check") or "NONE") != "NONE":
        out.append(f"{rid}: 有泛用詞 / 衝突標記")
    if str(row.get("overlay_version") or "") != manifest_version:
        out.append(f"{rid}: overlay_version 與 manifest 不一致")
    for field_name in _PROVENANCE:
        if not (row.get(field_name) or "").strip():
            out.append(f"{rid}: 缺少來源欄位 {field_name}")
    if not (row.get("alias_text_normalized") or "").strip():
        out.append(f"{rid}: 缺少品牌詞")
    return out


def _disabled(issues: list[str], started: float, directory: Path | None) -> ReviewedBrandOverlay:
    status = OverlayStatus(active=False, issues=tuple(issues), load_ms=round((time.perf_counter() - started) * 1000, 3),
                           directory=str(directory) if directory else None)
    return ReviewedBrandOverlay((), status)


def load_reviewed_brand_overlay(directory: str | Path | None) -> ReviewedBrandOverlay:
    """載入並驗證 overlay；任何驗證失敗 → 停用（brands 空、issues 列出原因）。從不丟出例外。"""
    started = time.perf_counter()
    if not directory:
        return _disabled(["未設定品牌 overlay 目錄"], started, None)
    directory = Path(directory)
    try:
        manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
        manifest_rep = validate_manifest(manifest, directory)
        issues = [f"manifest {i.code}: {i.message}" for i in manifest_rep.errors]
        candidate_rows: list[dict[str, str]] = []
        for name, meta in (manifest.get("inputs") or {}).items():
            path = Path(meta.get("path", ""))
            if not path.exists() or sha256_file(path) != meta.get("sha256"):
                issues.append(f"輸入 {name} 不存在或 sha256 與 manifest 不一致")
            elif name == "brand_candidates.csv":
                candidate_rows = read_review_csv(path)
        if not candidate_rows:
            issues.append("缺少可驗證的 brand_candidates.csv 輸入")
        rows = read_review_csv(directory / REVIEWED_BRAND_FILE)
        if candidate_rows:
            rep = validate_reviewed_brand_rows(rows, candidate_rows)
            issues += [f"{i.code} {i.row_id}: {i.message}" for i in rep.errors]
        version = str(manifest.get("overlay_version", ""))
        accepted = [r for r in rows if (r.get("decision") or "") == ACCEPT]
        for row in accepted:
            issues += _row_issues(row, version)
        keys = [conservative_key(r.get("alias_text_normalized")) for r in accepted]
        issues += [f"品牌詞重複：{k}" for k in sorted({k for k in keys if keys.count(k) > 1})]
        if issues:
            return _disabled(issues, started, directory)
        brands = tuple(ReviewedBrand(
            review_id=r["review_id"], token=r["alias_text_raw"], key=conservative_key(r["alias_text_normalized"]),
            applicant=r["target_official_name"], zh_brand=(r.get("zh_brand_candidate") or "").strip(),
            support_count=int(r.get("support_count") or 0), reviewer=r["reviewer"], reviewed_at=r["reviewed_at"],
            overlay_version=version, source_record_id=r["source_record_id"]) for r in accepted)
        status = OverlayStatus(active=True, overlay_version=version, accepted_brands=len(brands),
                               ignored_rows=len(rows) - len(accepted),
                               load_ms=round((time.perf_counter() - started) * 1000, 3), directory=str(directory))
        return ReviewedBrandOverlay(brands, status)
    except Exception as exc:  # noqa: BLE001 - fail closed：品牌輔助關閉，既有查證不受影響
        return _disabled([f"載入失敗：{type(exc).__name__}"], started, directory)


# ---------------------------------------------------------------- 快取（以檔案指紋判斷是否失效）

_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple, ReviewedBrandOverlay]] = {}


def _fingerprint(directory: Path) -> tuple:
    out = []
    for name in (MANIFEST_FILE, REVIEWED_BRAND_FILE):
        try:
            st = (directory / name).stat()
            out.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((name, None, None))
    return tuple(out)


def get_overlay(directory: str | Path | None) -> ReviewedBrandOverlay:
    """快取的 overlay：檔案（manifest / reviewed csv）的 mtime 或大小改變就重新驗證；驗證失敗的結果也會快取（避免每次查詢重驗），
    檔案修好後（指紋改變）自動重新載入。回傳的物件不可變，可跨執行緒共用。"""
    if not directory:
        return load_reviewed_brand_overlay(None)
    path = Path(directory)
    key, fp = str(path.resolve()), _fingerprint(path)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == fp:
            return hit[1]
    overlay = load_reviewed_brand_overlay(path)
    if not overlay.status.active:
        logger.warning("brand_overlay_disabled", extra={"stage": "brand_assist", "issue_count": len(overlay.status.issues)})
    with _cache_lock:
        _cache[key] = (fp, overlay)
    return overlay


def clear_overlay_cache() -> None:
    with _cache_lock:
        _cache.clear()


def configured_overlay() -> ReviewedBrandOverlay:
    """依設定（BRAND_ASSIST_OVERLAY_DIR；未設定 = 關閉）取得 overlay。"""
    from app.config.settings import get_settings

    return get_overlay(get_settings().brand_assist_overlay_dir)


@dataclass
class BrandAssistReport:
    """lookup 的觀測資料（不含使用者原文）；供評估 / 效能量測使用。"""

    mentioned: bool = False
    brand_only: bool = False
    member_count: int = 0
    candidate_count: int = 0
    lookup_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
