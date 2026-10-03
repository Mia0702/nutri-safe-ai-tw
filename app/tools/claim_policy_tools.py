"""結構化 claim-policy Evidence Tools（TFDA 宣稱法規 corpus，migration 0003 的 claim_policy_* 表）。

* search_claim_policy_rules：依 rule_id / 規則類別 / policy_effect 取官方規則。
* search_claim_policy_examples：宣稱文字與官方例示詞句「正規化後完全相同」才算對應（只比字面，不推測語意，
  不使用任何關鍵字清單）。
* lookup_health_function：依官方功效名稱（正規化後完全相同）取保健功效目錄。

每筆結果都保留 evidence_use（DECISION_ELIGIBLE / CORROBORATION_REQUIRED / CONTEXT_ONLY / EXCLUDED_SUPERSEDED）。
EXCLUDED_SUPERSEDED 預設不回傳（current query 不使用）。能不能下結論由 app/risk/claim_policy.py 決定，
Tool 只負責查詢與轉換，不產生使用者回答。
"""

from typing import Any, Iterable, Protocol

from app.normalization import normalize_name
from app.schemas.enums import PolicyEvidenceUse, SourceType
from app.schemas.evidence import (
    ClaimPolicyExampleEvidence,
    ClaimPolicyRuleEvidence,
    HealthFunctionEvidence,
    SourceRef,
)
from app.tools.product_tools import _parse_dt

RULE_PREFIX = "policy_rule:"
EXAMPLE_PREFIX = "policy_example:"
HEALTH_FUNCTION_PREFIX = "health_function:"
MAX_RULES = 20
MAX_EXAMPLE_MATCHES = 10


class ClaimPolicyRepository(Protocol):
    """claim_policy_* 表的唯讀存取。每列附上所屬來源（corpus source_id、網址、標題、取得時間）。"""

    def corpus_available(self) -> bool: ...

    def list_rules(self) -> list[dict[str, Any]]: ...

    def list_examples(self) -> list[dict[str, Any]]: ...

    def list_health_functions(self) -> list[dict[str, Any]]: ...


def _use(row: dict[str, Any]) -> PolicyEvidenceUse:
    return PolicyEvidenceUse(row["evidence_use"])


def _current(rows: Iterable[dict[str, Any]], include_superseded: bool) -> list[dict[str, Any]]:
    return [r for r in rows if include_superseded or _use(r) != PolicyEvidenceUse.EXCLUDED_SUPERSEDED]


def _source(src: dict[str, Any], record_id: str) -> SourceRef:
    title = src.get("document_title") or src.get("source_id") or "官方法規文件"
    authority = src.get("authority")
    return SourceRef(
        source_name=f"{authority} {title}" if authority else title,
        source_type=SourceType.STRUCTURED_DB,
        source_url=src.get("source_url"),
        source_record_id=record_id,
        retrieved_at=_parse_dt(src.get("retrieved_at")),
        is_official=True,
    )


def _date(value: Any) -> str | None:
    return str(value) if value else None


def rule_evidence(row: dict[str, Any]) -> ClaimPolicyRuleEvidence:
    src = row.get("source") or {}
    return ClaimPolicyRuleEvidence(
        evidence_id=RULE_PREFIX + row["rule_id"],
        rule_id=row["rule_id"],
        source_id=src.get("source_id") or "",
        rule_category=row["rule_category"],
        policy_effect=row["policy_effect"],
        claim_category=row["claim_category"],
        rule_summary_zh=row.get("rule_summary_zh"),
        official_text=row["official_text"],
        conditions=row.get("conditions"),
        legal_basis=row.get("legal_basis"),
        article_or_section=row["article_or_attachment"],
        effective_date=_date(row.get("effective_date")),
        status=row["status"],
        review_status=row["review_status"],
        evidence_use=_use(row),
        quarantine_reasons=list(row.get("quarantine_reasons") or []),
        source=_source(src, row["rule_id"]),
    )


def example_evidence(row: dict[str, Any], matched: list[str]) -> ClaimPolicyExampleEvidence:
    src = row.get("source") or {}
    return ClaimPolicyExampleEvidence(
        evidence_id=EXAMPLE_PREFIX + row["example_id"],
        example_id=row["example_id"],
        rule_id=row["rule_id"],
        source_id=src.get("source_id") or "",
        example_text=row["example_text"],
        example_type=row["example_type"],
        claim_category=row["claim_category"],
        conditions=row.get("conditions"),
        article_or_section=row.get("article_or_attachment"),
        evidence_use=_use(row),
        quarantine_reasons=list(row.get("quarantine_reasons") or []),
        matched_claim_texts=matched,
        source=_source(src, row["example_id"]),
    )


def health_function_evidence(row: dict[str, Any]) -> HealthFunctionEvidence:
    src = row.get("source") or {}
    return HealthFunctionEvidence(
        evidence_id=HEALTH_FUNCTION_PREFIX + row["function_id"],
        function_id=row["function_id"],
        official_function_name=row["official_function_name"],
        official_definition=row.get("official_definition"),
        evaluation_method_title=row.get("evaluation_method_title"),
        legal_basis=row.get("legal_basis"),
        effective_date=_date(row.get("effective_date")),
        status=row["status"],
        review_status=row["review_status"],
        evidence_use=_use(row),
        source=_source(src, row["function_id"]),
    )


def search_claim_policy_rules(
    repo: ClaimPolicyRepository,
    *,
    rule_ids: list[str] | None = None,
    rule_categories: list[str] | None = None,
    policy_effects: list[str] | None = None,
    include_superseded: bool = False,
) -> list[ClaimPolicyRuleEvidence]:
    """rule_ids 直接取；rule_categories / policy_effects 為交集條件（兩者都給才篩選類別規則）。"""
    wanted_ids = set(rule_ids or [])
    categories, effects = set(rule_categories or []), set(policy_effects or [])
    out: list[ClaimPolicyRuleEvidence] = []
    for row in _current(repo.list_rules(), include_superseded):
        by_id = row["rule_id"] in wanted_ids
        by_filter = bool(categories and effects) and row["rule_category"] in categories and row["policy_effect"] in effects
        if by_id or by_filter:
            out.append(rule_evidence(row))
    out.sort(key=lambda e: e.rule_id)
    return out[:MAX_RULES]


def search_claim_policy_examples(
    repo: ClaimPolicyRepository, claim_texts: list[str], *, include_superseded: bool = False
) -> list[ClaimPolicyExampleEvidence]:
    """宣稱文字與官方例示詞句正規化後完全相同才回傳（每筆附上對應到的宣稱文字）。"""
    by_norm: dict[str, list[str]] = {}
    for text in claim_texts:
        norm = normalize_name(text)
        if norm:
            by_norm.setdefault(norm, []).append(text)
    if not by_norm:
        return []
    out: list[ClaimPolicyExampleEvidence] = []
    for row in _current(repo.list_examples(), include_superseded):
        matched = by_norm.get(normalize_name(row["example_text"]))
        if matched:
            out.append(example_evidence(row, matched))
    out.sort(key=lambda e: e.example_id)
    return out[:MAX_EXAMPLE_MATCHES]


def lookup_health_function(
    repo: ClaimPolicyRepository, function_names: list[str], *, include_superseded: bool = False
) -> list[HealthFunctionEvidence]:
    wanted = {normalize_name(n) for n in function_names if normalize_name(n)}
    if not wanted:
        return []
    return sorted(
        (health_function_evidence(row) for row in _current(repo.list_health_functions(), include_superseded)
         if normalize_name(row["official_function_name"]) in wanted),
        key=lambda e: e.function_id,
    )
