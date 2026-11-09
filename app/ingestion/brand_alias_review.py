"""品牌候選人工審核準備（D4；offline，只讀本機官方快照與 D2 brand_candidates.csv，不連線、不寫資料庫、不改 runtime）。

目的：對 D2 通過機械規則的品牌候選做「可重現的稽核欄位」（中文品牌證據分類、衝突統計），
再把人工決定（ACCEPT / REJECT / NEEDS_REVIEW）合併成共用審核 CSV 列。

安全界線：
* ACCEPT 只代表「已審核的品牌別名關係」：不是品牌所有權（沒有 OWNS_BRAND）、不是產品身份、不得由品牌查詢自動確認產品。
* match_policy 永遠是 CANDIDATE_ONLY（連 ACCEPT 也是）；沒有 runtime loader、沒有資料庫匯入。
* 人工決定來自 decisions CSV（資料，不是程式內的特例）；本模組只做稽核統計、套用決定、擋下不合格的 ACCEPT。
* 不翻譯、不相似度、不使用 AI 翻譯或購物 / 品牌網站當證據；證據只有官方紀錄內的中文品名 / 英文品名 / 申請商。
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from app.ingestion.product_alias_review import (
    _TOKEN_RE,
    BRAND_MIN_RECORDS,
    DESCRIPTOR_FLAGS,
    ZH_CLEAN_BRAND_PAIR,
    ZH_EVIDENCE_CLASSES,
    ZH_INCONSISTENT_PAIR,
    ZH_NO_CLEAR_BRAND,
    ZH_NOISY_PREFIX,
    OfficialRecord,
    ValidationReport,
    brand_accept_gate_failures,
    english_leading_token,
    invalid_english_reason,
    normalize_alias_text,
    validate_rows,
)

BRAND_REVIEW_VERSION = "1.0"
REVIEWED_BRAND_FILE = "reviewed_brand_aliases.csv"
DECISIONS_FILE = "brand_review_decisions.csv"
DECISION_COLUMNS = ("alias_text_normalized", "decision", "descriptor_flag", "decision_reason")

BRAND_REVIEW_EXTRA_COLUMNS = (
    "classification", "zh_brand_candidate", "zh_pair_basis", "zh_evidence_class", "zh_prefix_support",
    "zh_prefix_form_count", "zh_prefix_other_applicants", "applicant_count", "other_applicant_records",
    "token_applicant_count", "token_record_count", "token_equals_product_count", "shared_brand_token_count",
    "descriptor_flag", "passes_rule", "rule_failures", "example_permits", "example_names", "source_dataset_id",
    "review_batch",
)

LATIN_SELF_PAIR = "LATIN_SELF_PAIR"      # 官方中文品名本身就以同一英文品牌詞開頭
CJK_PREFIX = "CJK_PREFIX"                # 官方中文品名共同前綴
NO_PAIR = "NO_PAIR"

CLEAN_MIN_SHARE = 0.9          # 中文證據需涵蓋 >= 90% 的支撐紀錄
FORM_MIN_RECORDS = 2           # 一個「中文寫法」至少要有幾筆
FORM_MIN_SHARE = 0.1           # 且至少占支撐紀錄多少比例，才算一種獨立寫法
SPLIT_MIN_SHARE = 0.2          # 沒有共同前綴時：兩種寫法各 >= 20% → 前綴不一致，否則視為完全沒有中文品牌


def _nfkc(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def token_key(text: str | None) -> str:
    return (text or "").casefold().rstrip(".-'’")


def latin_tokens(text: str | None) -> set[str]:
    return {token_key(t) for t in _TOKEN_RE.findall(unicodedata.normalize("NFKC", text or ""))}


def latin_lead_of_chinese_name(name_zh: str) -> str:
    m = _TOKEN_RE.match(_nfkc(name_zh))
    return token_key(m.group(0)) if m else ""


def form_key(name_zh: str) -> str:
    return _nfkc(name_zh)[:2].casefold()


# ---------------------------------------------------------------- 中文品牌證據分類（純函式）


def prefix_boundary_noise(prefix: str) -> bool:
    """前綴首尾是標點 / 符號，或中文後面接單獨的拉丁字母（例如註冊商標符號殘留）。"""
    if not prefix:
        return False
    if not prefix[0].isalnum() or not prefix[-1].isalnum():
        return True
    return len(prefix) >= 2 and prefix[-1].isascii() and prefix[-1].isalpha() and not prefix[-2].isascii()


def classify_zh_evidence(*, support_records: int, latin_support: int, prefix: str, prefix_support: int,
                         shorter_prefix_support: int, prefix_other_applicants: int, shared_brand_tokens: int,
                         form_counts: list[int]) -> tuple[str, str]:
    """回傳 (zh_evidence_class, zh_pair_basis)。只看統計與結構，不看任何詞表。"""
    n = max(support_records, 1)
    if latin_support / n >= CLEAN_MIN_SHARE:
        return ZH_CLEAN_BRAND_PAIR, LATIN_SELF_PAIR
    real_forms = [c for c in form_counts if c >= FORM_MIN_RECORDS and c / n >= FORM_MIN_SHARE]
    if not prefix:
        split = [c for c in form_counts if c >= FORM_MIN_RECORDS and c / n >= SPLIT_MIN_SHARE]
        return (ZH_INCONSISTENT_PAIR if len(split) >= 2 else ZH_NO_CLEAR_BRAND), NO_PAIR
    if (prefix_boundary_noise(prefix) or shorter_prefix_support > prefix_support or prefix_other_applicants >= 2):
        return ZH_NOISY_PREFIX, CJK_PREFIX
    if prefix_support / n < CLEAN_MIN_SHARE or len(real_forms) > 1 or shared_brand_tokens >= 1:
        return ZH_INCONSISTENT_PAIR, CJK_PREFIX
    return ZH_CLEAN_BRAND_PAIR, CJK_PREFIX


def conflict_codes(*, token_applicants: int, other_applicant_records: int, prefix_other_applicants: int,
                   shared_brand_tokens: int, form_count: int, token_equals_product: int) -> list[str]:
    codes = []
    if token_applicants > 1:
        codes.append("TOKEN_USED_BY_MULTIPLE_APPLICANTS")
    if other_applicant_records:
        codes.append("TOKEN_USED_BY_OTHER_APPLICANTS")
    if prefix_other_applicants >= 1:
        codes.append("ZH_PREFIX_USED_BY_OTHER_APPLICANT")
    if shared_brand_tokens >= 1:
        codes.append("ZH_PREFIX_SHARED_WITH_OTHER_BRAND_TOKEN")
    if form_count > 1:
        codes.append("MULTIPLE_ZH_BRAND_FORMS")
    if token_equals_product:
        codes.append("TOKEN_EQUALS_OFFICIAL_PRODUCT_NAME")
    return codes


# ---------------------------------------------------------------- 稽核


class BrandAuditIndex:
    """一次掃描建立的官方紀錄索引（deterministic，與輸入順序無關）。"""

    def __init__(self, records: Iterable[OfficialRecord]):
        self.records = sorted(records, key=lambda r: (r.source_id, r.product_id, r.permit_number))
        self.lead: dict[str, list[OfficialRecord]] = defaultdict(list)
        self.token_applicants: dict[str, set[str]] = defaultdict(set)
        self.token_records: Counter = Counter()
        self.exact_name_count: Counter = Counter()
        for rec in self.records:
            usable_en = not invalid_english_reason(rec.name_en)
            lead = token_key(english_leading_token(rec.name_en)) if usable_en else ""
            if lead:
                self.lead[lead].append(rec)
            toks = (latin_tokens(rec.name_en) if usable_en else set()) | latin_tokens(rec.name_zh)
            for tok in toks:
                self.token_applicants[tok].add(rec.applicant)
                self.token_records[tok] += 1
            for name in {normalize_alias_text(rec.name_zh), normalize_alias_text(rec.name_en) if usable_en else ""}:
                if name:
                    self.exact_name_count[name] += 1


def _startswith_count(records: Iterable[OfficialRecord], prefix: str) -> int:
    return sum(1 for r in records if _nfkc(r.name_zh).startswith(prefix))


def audit_brand_candidate(row: dict[str, str], index: BrandAuditIndex,
                          candidate_tokens: Iterable[str]) -> dict[str, str]:
    """回傳要併入審核列的稽核欄位（全為字串）。"""
    token = row["alias_text_normalized"]
    lead = index.lead.get(token, [])
    applicants = {r.applicant for r in lead}
    n = len(lead)
    prefix = _nfkc(row.get("zh_brand_candidate"))
    latin_support = sum(1 for r in lead if latin_lead_of_chinese_name(r.name_zh) == token)
    prefix_support = _startswith_count(lead, prefix) if prefix else 0
    shorter = max((_startswith_count(lead, prefix[:k]) for k in range(2, len(prefix))), default=0)
    other_applicants = {r.applicant for r in index.records
                        if prefix and r.applicant not in applicants and _nfkc(r.name_zh).startswith(prefix)}
    shared_tokens = sum(
        1 for other in candidate_tokens
        if other != token and prefix
        and _startswith_count(index.lead.get(other, []), prefix) >= BRAND_MIN_RECORDS)
    forms = Counter(form_key(r.name_zh) for r in lead)
    form_counts = sorted(forms.values(), reverse=True)
    form_count = sum(1 for c in form_counts if c >= FORM_MIN_RECORDS and c / max(n, 1) >= FORM_MIN_SHARE)
    token_applicants = len(index.token_applicants.get(token, set()))
    equals = index.exact_name_count.get(token, 0)
    zh_class, basis = classify_zh_evidence(
        support_records=n, latin_support=latin_support, prefix=prefix, prefix_support=prefix_support,
        shorter_prefix_support=shorter, prefix_other_applicants=len(other_applicants),
        shared_brand_tokens=shared_tokens, form_counts=form_counts)
    codes = conflict_codes(
        token_applicants=token_applicants, other_applicant_records=int(row.get("other_applicant_records") or 0),
        prefix_other_applicants=len(other_applicants) if basis == CJK_PREFIX else 0,
        shared_brand_tokens=shared_tokens if basis == CJK_PREFIX else 0, form_count=form_count,
        token_equals_product=equals)
    return {
        "zh_pair_basis": basis, "zh_evidence_class": zh_class,
        "zh_prefix_support": str(latin_support if basis == LATIN_SELF_PAIR else prefix_support),
        "zh_prefix_form_count": str(form_count), "zh_prefix_other_applicants": str(len(other_applicants)),
        "applicant_count": str(len(applicants)), "token_applicant_count": str(token_applicants),
        "token_record_count": str(index.token_records.get(token, 0)), "token_equals_product_count": str(equals),
        "shared_brand_token_count": str(shared_tokens), "conflict_check": ";".join(codes) or "NONE",
    }


# ---------------------------------------------------------------- 決定檔與審核列


class BrandReviewError(RuntimeError):
    pass


def read_decisions(path: Path) -> dict[str, dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in DECISION_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise BrandReviewError(f"{path} 缺少欄位 {missing}")
        decisions: dict[str, dict[str, str]] = {}
        for row in reader:
            key = normalize_alias_text(row["alias_text_normalized"])
            if key in decisions:
                raise BrandReviewError(f"決定檔重複的品牌詞：{key}")
            decisions[key] = {k: (row.get(k) or "").strip() for k in DECISION_COLUMNS}
    return decisions


def build_reviewed_brand_rows(candidate_rows: list[dict[str, str]], records: Iterable[OfficialRecord],
                              decisions: dict[str, dict[str, str]], *, overlay_version: str, reviewer: str,
                              reviewed_at: str) -> list[dict[str, str]]:
    """只審核 passes_rule == true 的候選；每一列都必須有人工決定。ACCEPT 過不了閘門就直接報錯，不默默降級。"""
    index = BrandAuditIndex(records)
    passing = sorted((r for r in candidate_rows if r.get("passes_rule") == "true"),
                     key=lambda r: (-int(r["support_count"]), r["alias_text_normalized"]))
    tokens = [r["alias_text_normalized"] for r in passing]
    unknown = set(decisions) - set(tokens)
    absent = [t for t in tokens if t not in decisions]
    if unknown or absent:
        raise BrandReviewError(f"決定檔與候選不一致：多出 {sorted(unknown)}；缺少 {absent}")
    rows: list[dict[str, str]] = []
    for cand in passing:
        token = cand["alias_text_normalized"]
        decision = decisions[token]
        if decision["descriptor_flag"] not in DESCRIPTOR_FLAGS:
            raise BrandReviewError(f"{token}: descriptor_flag={decision['descriptor_flag']!r} 不合法")
        row = dict(cand)
        row.update(audit_brand_candidate(cand, index, tokens))
        row.update({
            "decision": decision["decision"], "decision_reason": decision["decision_reason"],
            "descriptor_flag": decision["descriptor_flag"] or "NONE", "match_policy": "CANDIDATE_ONLY",
            "reviewer": reviewer, "reviewed_at": reviewed_at, "overlay_version": overlay_version,
        })
        if decision["decision"] == "ACCEPT":
            failed = brand_accept_gate_failures(row)
            if failed:
                raise BrandReviewError(f"{token}: ACCEPT 未通過閘門：{[code for code, _ in failed]}")
        if decision["decision"] == "NEEDS_REVIEW":
            row["reviewer"] = row["reviewed_at"] = ""   # 未做決定的列不署名
        rows.append(row)
    return rows


# ---------------------------------------------------------------- validator（只回報，不修改）


def validate_reviewed_brand_rows(rows: list[dict[str, str]], candidate_rows: list[dict[str, str]]) -> ValidationReport:
    from app.ingestion.product_alias_review import Issue

    rep = validate_rows(rows, existing_review_ids=[r.get("review_id", "") for r in candidate_rows])
    candidates = {r["review_id"]: r for r in candidate_rows}
    seen: Counter = Counter()

    def err(code: str, row: dict[str, str], msg: str) -> None:
        rep.issues.append(Issue("ERROR", code, row.get("review_id", "?"), msg))

    for r in rows:
        seen[r.get("alias_text_normalized", "")] += 1
        cand = candidates.get(r.get("review_id", ""))
        if r.get("alias_kind") != "BRAND":
            err("NOT_A_BRAND_ROW", r, "reviewed_brand_aliases.csv 只能放 BRAND 列")
        if cand is None or cand.get("alias_text_normalized") != r.get("alias_text_normalized"):
            err("REVIEW_ROW_NOT_IN_CANDIDATES", r, "review_id 不在 D2 品牌候選中，或品牌詞不一致")
        if not (r.get("decision_reason") or "").strip():
            err("MISSING_DECISION_REASON", r, "每一列審核結果都需要 decision_reason")
        if r.get("zh_evidence_class") not in ZH_EVIDENCE_CLASSES:
            err("INVALID_ZH_EVIDENCE_CLASS", r, f"zh_evidence_class={r.get('zh_evidence_class')!r}")
        if r.get("descriptor_flag") not in DESCRIPTOR_FLAGS:
            err("INVALID_DESCRIPTOR_FLAG", r, f"descriptor_flag={r.get('descriptor_flag')!r}")
        if r.get("decision") == "REJECT" and not (r.get("decision_reason") or "").strip():
            err("REJECT_WITHOUT_REASON", r, "REJECT 需要 decision_reason")
    for token, count in seen.items():
        if count > 1:
            rep.issues.append(Issue("ERROR", "DUPLICATE_BRAND_ALIAS", token, f"品牌詞 {token!r} 重複 {count} 列"))
    return rep


# ---------------------------------------------------------------- 摘要


def reason_code(decision_reason: str) -> str:
    return re.split(r"[:：]", decision_reason or "", maxsplit=1)[0].strip() or "UNSPECIFIED"


def summarize_reviewed_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    by_decision = Counter(r["decision"] for r in rows)
    accepted = sorted((r for r in rows if r["decision"] == "ACCEPT"), key=lambda r: (-int(r["support_count"]), r["alias_text_normalized"]))
    return {
        "total_reviewed": len(rows),
        "decisions": {d: by_decision.get(d, 0) for d in ("ACCEPT", "REJECT", "NEEDS_REVIEW")},
        "zh_evidence_classes": {c: sum(1 for r in rows if r["zh_evidence_class"] == c) for c in ZH_EVIDENCE_CLASSES},
        "top_accepted": [{"brand": r["alias_text_raw"], "support_count": int(r["support_count"])} for r in accepted[:10]],
        "reject_reasons": dict(Counter(reason_code(r["decision_reason"]) for r in rows if r["decision"] == "REJECT").most_common()),
        "needs_review_reasons": dict(Counter(reason_code(r["decision_reason"]) for r in rows
                                             if r["decision"] == "NEEDS_REVIEW").most_common()),
    }
