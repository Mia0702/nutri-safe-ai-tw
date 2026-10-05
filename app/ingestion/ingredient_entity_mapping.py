"""Product Ingredient Mapping Bridge：Product 成分 → Interaction Corpus 的 ingredient entity。

範圍（第一版）：只映射到 Ingredient Interaction Evidence Corpus 已定義的 ingredient entity
（`N_*` NUTRIENT_GROUP、`F_*` 特定 form / salt / vitamer / mixture）；不 canonicalize 所有原料、
不映射 drug substance（`D_*`）、不建立 Interaction、不計算劑量或過量。

Identity 原則（不依 LLM 常識、不用 embedding 相似度）：
- 保留 corpus 的 identity level：魚油 ≠ Omega-3 ≠ EPA ≠ DHA；Folate ≠ Folic acid；Calcium ≠ Calcium carbonate。
  form 與營養素群的關係只用 corpus 的 `parent_nutrient_entity_id`，表示「貢獻到該營養素群」（CONTRIBUTES_TO_NUTRIENT），
  不是 SAME_AS、也不是等量換算。
- 正式 mapping 只來自 corpus `match_policy = EXACT_AT_DECLARED_IDENTITY_LEVEL` 的英文正式名稱 / 有來源的同義詞或縮寫
  （正規化後整段完全相同），或人工審核過的 alias（`ReviewedAlias`，例如經審核的中文別名）。
- corpus 的中文譯名是 `DISPLAY_ONLY_NOT_AUTOMERGE`：完全相同也只成為候選（NEEDS_REVIEW），不建立正式 CONTAINS。
- 部分包含（例如原料名稱中出現某營養素名稱）只成為候選；不做去鹽名、去字尾、語意相近的合併。
- 同一段文字指向互不相關的多個 entity → AMBIGUOUS（只保存候選）。

成分來源：
- health_food：既有 canonical Ingredient（官方名稱、官方紀錄中的英文名 / 別名）。
- 國產維生素 / 輸入錠狀膠囊：`raw_import_records.raw_data.ingredient_staging` 的研究 token draft。
  staging 的 READY / NEEDS_REVIEW 只代表「解析就緒度」，不是 clinical verification，也不提高 mapping 等級。

Reviewed overlay（`overlay/`，官方來源 desk review 的 alias / form / form→nutrient；載入時驗 SHA256SUMS）：
- 優先序（OFFICIAL_PRODUCT_INGREDIENT_TOKEN 語境）：整段 token 有 overlay alias → 只用 overlay 的判定；
  沒有 overlay alias → 原 corpus 規則（exact → 官方括號對照 → 候選）不變。
- 只有 VERIFIED_OFFICIAL / SUPPORTED_AUTHORITATIVE 可成為正式 mapping；AMBIGUOUS / NEEDS_REVIEW 只成為候選
  （即使原 corpus 對同一段文字有 exact）。
- overlay alias 只做整段 exact（NFKC、casefold、空白合併）；不參與括號拆解、部分包含、去鹽名、翻譯合併。
- PRODUCT_FORM_CONTEXT_OVER_BASE_NUTRIENT_ALIAS：產品原料語境中 overlay 指定較具體的 F_*（例如 Riboflavin → F_RIBOFLAVIN），
  F_* 只 CONTRIBUTES_TO_NUTRIENT 到營養素群，不是 SAME_AS。

本模組只做 deterministic 計算，不讀寫資料庫；raw 成分文字原樣保留（只複製，不修改）。
"""

from __future__ import annotations

import csv
import hashlib
import html
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.normalization import normalize_name, stable_id

CORPUS_DIR = Path(__file__).resolve().parents[2] / "data" / "ingredient_interaction" / "2026-10-04"
ENTITIES_FILE = "interaction_entities.csv"
ALIASES_FILE = "interaction_entity_aliases.csv"
RECORDS_FILE = "interaction_records.csv"
VALIDATION_FILE = "interaction_validation.json"
CORPUS_FILES = (ENTITIES_FILE, ALIASES_FILE, RECORDS_FILE)

NUTRIENT_GROUP_LEVEL = "NUTRIENT_GROUP"
INGREDIENT_ENTITY_TYPE = "INGREDIENT"
EXACT_POLICY = "EXACT_AT_DECLARED_IDENTITY_LEVEL"
CANONICAL_RELATION = "CANONICAL_NAME"
READY = "READY"
DUPLICATE_INTAKE = "DUPLICATE_INTAKE"

# Neo4j label（corpus identity level → graph label）
NUTRIENT_GROUP_LABEL = "NutrientGroup"
INGREDIENT_FORM_LABEL = "IngredientForm"

# Reviewed overlay
OVERLAY_DIR = CORPUS_DIR / "overlay"
OVERLAY_ALIASES_FILE = "reviewed_ingredient_aliases.csv"
OVERLAY_FORMS_FILE = "reviewed_ingredient_forms.csv"
OVERLAY_LINKS_FILE = "reviewed_form_to_nutrient.csv"
OVERLAY_SOURCES_FILE = "reviewed_alias_sources.csv"
OVERLAY_VALIDATION_FILE = "ingredient_alias_validation.json"
OVERLAY_FILES = (OVERLAY_ALIASES_FILE, OVERLAY_FORMS_FILE, OVERLAY_LINKS_FILE, OVERLAY_SOURCES_FILE,
                 OVERLAY_VALIDATION_FILE)
OVERLAY_FORMAL_STATUSES = frozenset({"VERIFIED_OFFICIAL", "SUPPORTED_AUTHORITATIVE"})
OVERLAY_CANDIDATE_STATUSES = frozenset({"AMBIGUOUS", "NEEDS_REVIEW"})
OVERLAY_PRODUCT_CONTEXT = "OFFICIAL_PRODUCT_INGREDIENT_TOKEN"
CONTRIBUTES_TO_NUTRIENT = "CONTRIBUTES_TO_NUTRIENT"
BASE_ORIGIN = "BASE_CORPUS"
OVERLAY_ORIGIN = "REVIEWED_OVERLAY"
# overlay alias 狀態本身的信心（mapping 規則信心，不是臨床確定度）
OVERLAY_STATUS_CONFIDENCE = {"VERIFIED_OFFICIAL": 0.95, "SUPPORTED_AUTHORITATIVE": 0.9,
                             "NEEDS_REVIEW": 0.5, "AMBIGUOUS": 0.2}


class MappingStatus(StrEnum):
    VERIFIED_EXACT = "VERIFIED_EXACT"        # 與 corpus 英文正式名稱完全相同
    SUPPORTED_ALIAS = "SUPPORTED_ALIAS"      # 與 corpus 有來源的同義詞 / 縮寫、人工審核 alias，或官方紀錄自身的括號對照完全相同
    FORM_TO_GROUP = "FORM_TO_GROUP"          # 已正式映射的 form 依 corpus parent 貢獻到營養素群（不是 SAME_AS）
    AMBIGUOUS = "AMBIGUOUS"                  # 同一段文字指向互不相關的多個 entity
    NEEDS_REVIEW = "NEEDS_REVIEW"            # 只有中文譯名 / 部分包含等候選證據
    NO_MATCH = "NO_MATCH"


FORMAL_STATUSES = frozenset({MappingStatus.VERIFIED_EXACT, MappingStatus.SUPPORTED_ALIAS, MappingStatus.FORM_TO_GROUP})
CANDIDATE_STATUSES = frozenset({MappingStatus.AMBIGUOUS, MappingStatus.NEEDS_REVIEW})


class MatchMethod(StrEnum):
    CORPUS_CANONICAL_NAME_EXACT = "CORPUS_CANONICAL_NAME_EXACT"
    CORPUS_SOURCED_SYNONYM_EXACT = "CORPUS_SOURCED_SYNONYM_EXACT"
    REVIEWED_ALIAS_EXACT = "REVIEWED_ALIAS_EXACT"
    REVIEWED_OVERLAY_ALIAS_EXACT = "REVIEWED_OVERLAY_ALIAS_EXACT"   # overlay VERIFIED_OFFICIAL / SUPPORTED_AUTHORITATIVE
    OFFICIAL_PARENTHETICAL = "OFFICIAL_PARENTHETICAL"      # 官方原文「X (Y)」其中一側 exact、另一側沒有矛盾
    CORPUS_PARENT_NUTRIENT = "CORPUS_PARENT_NUTRIENT"      # FORM_TO_GROUP（base corpus form）
    REVIEWED_FORM_TO_NUTRIENT = "REVIEWED_FORM_TO_NUTRIENT"  # FORM_TO_GROUP（overlay 新增 form 的審核關係）
    REVIEWED_OVERLAY_CANDIDATE = "REVIEWED_OVERLAY_CANDIDATE"  # 候選：overlay 審核為 AMBIGUOUS / NEEDS_REVIEW
    DISPLAY_TRANSLATION_EXACT = "DISPLAY_TRANSLATION_EXACT"  # 候選
    CORPUS_ALIAS_NOT_READY = "CORPUS_ALIAS_NOT_READY"        # 候選：corpus alias / entity 尚未 READY
    ALIAS_CONTAINED_IN_TEXT = "ALIAS_CONTAINED_IN_TEXT"      # 候選
    CONFLICTING_ENTITIES = "CONFLICTING_ENTITIES"            # AMBIGUOUS
    NONE = "NONE"


class ReviewStatus(StrEnum):
    AUTO_ACCEPTED_RULE = "AUTO_ACCEPTED_RULE"  # 由上述 deterministic 規則接受（不是醫療專家審核）
    MANUAL_REVIEWED = "MANUAL_REVIEWED"
    SOURCE_REVIEWED = "SOURCE_REVIEWED"        # overlay：官方 / 權威來源 desk review（不是持照專家最終簽核）
    PENDING_REVIEW = "PENDING_REVIEW"
    NOT_APPLICABLE = "NOT_APPLICABLE"          # NO_MATCH


class IngredientSource(StrEnum):
    HEALTH_FOOD_CANONICAL_INGREDIENT = "HEALTH_FOOD_CANONICAL_INGREDIENT"
    TFDA_REGISTRATION_INGREDIENTS_RAW = "TFDA_REGISTRATION_INGREDIENTS_RAW"


class AmountBasis(StrEnum):
    UNKNOWN = "UNKNOWN"  # 元素量 / 鹽重量 / 活性量未確認 → 不得換算或合計


# mapping 規則本身的信心（不是臨床確定度）
METHOD_CONFIDENCE: dict[MatchMethod, float] = {
    MatchMethod.CORPUS_CANONICAL_NAME_EXACT: 1.0,
    MatchMethod.CORPUS_SOURCED_SYNONYM_EXACT: 0.95,
    MatchMethod.REVIEWED_ALIAS_EXACT: 0.95,
    MatchMethod.REVIEWED_OVERLAY_ALIAS_EXACT: 0.95,
    MatchMethod.OFFICIAL_PARENTHETICAL: 0.9,
    MatchMethod.REVIEWED_OVERLAY_CANDIDATE: 0.5,
    MatchMethod.DISPLAY_TRANSLATION_EXACT: 0.5,
    MatchMethod.CORPUS_ALIAS_NOT_READY: 0.5,
    MatchMethod.ALIAS_CONTAINED_IN_TEXT: 0.3,
    MatchMethod.CONFLICTING_ENTITIES: 0.2,
    MatchMethod.NONE: 0.0,
}
_METHOD_RANK = {m: i for i, m in enumerate(METHOD_CONFIDENCE)}

_LIST_CHARS_RE = re.compile(r"[、,，;；+&/]|\band\b", re.IGNORECASE)
_TRAILING_BRACKET_RE = re.compile(r"^(?P<head>.*?)\s*[(（\[【](?P<inner>[^()（）\[\]【】]*)[)）\]】]\s*$")
_ALNUM = re.compile(r"[0-9a-z]")
_CJK_RE = re.compile(r"[㐀-鿿]")
_SIMPLE_AMOUNT_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>毫克|公克|微克|mg|g|μg|µg|mcg|iu)\s*$", re.IGNORECASE)
_UNIT_CANON = {"毫克": "mg", "公克": "g", "微克": "μg", "mg": "mg", "g": "g", "μg": "μg", "µg": "μg", "mcg": "μg", "iu": "IU"}


# ----------------------------------------------------------------------------- corpus


@dataclass(frozen=True)
class CorpusEntity:
    entity_id: str
    entity_type: str
    canonical_name_en: str
    display_name_zh: str | None
    identity_level: str
    parent_nutrient_entity_id: str | None
    source_id: str
    source_url: str | None
    review_status: str
    origin: str = BASE_ORIGIN                    # BASE_CORPUS / REVIEWED_OVERLAY（overlay 新增的 form）
    overlay_identity_level: str | None = None    # overlay 審核的細分 identity level（例如 SALT_FAMILY_UNSPECIFIED_HYDRATION）

    @property
    def is_nutrient_group(self) -> bool:
        return self.identity_level == NUTRIENT_GROUP_LEVEL

    @property
    def graph_label(self) -> str:
        return NUTRIENT_GROUP_LABEL if self.is_nutrient_group else INGREDIENT_FORM_LABEL


@dataclass(frozen=True)
class CorpusAlias:
    alias_id: str
    entity_id: str
    alias_text: str
    language: str
    alias_relation: str
    match_policy: str
    source_id: str
    review_status: str

    @property
    def normalized(self) -> str:
        return match_key(self.alias_text)


@dataclass(frozen=True)
class ReviewedAlias:
    """人工審核過、可正式比對的別名（例如經審核的中文成分名）。第一版沒有任何預設資料。"""

    alias_text: str
    entity_id: str
    reviewer: str
    basis: str

    @property
    def alias_id(self) -> str:
        return f"REVIEWED:{self.entity_id}:{match_key(self.alias_text)}"


@dataclass(frozen=True)
class OverlayAlias:
    """Reviewed overlay 的一筆 alias。entity_id 只在正式狀態才有；候選只有 candidate_entity_id（可為 None）。"""

    alias_id: str
    alias_text: str
    language: str
    alias_status: str                 # VERIFIED_OFFICIAL / SUPPORTED_AUTHORITATIVE / AMBIGUOUS / NEEDS_REVIEW
    entity_id: str | None
    candidate_entity_id: str | None
    identity_level: str | None
    source_id: str
    source_url: str | None
    source_locator: str | None
    retrieved_at: str | None
    match_policy: str
    mapping_context: str
    precedence: str
    overlay_version: str

    @property
    def is_formal(self) -> bool:
        return self.alias_status in OVERLAY_FORMAL_STATUSES

    @property
    def key(self) -> str:
        return overlay_key(self.alias_text)


@dataclass(frozen=True)
class FormNutrientLink:
    """IngredientForm -[:CONTRIBUTES_TO_NUTRIENT]-> NutrientGroup 的審核來源（不是 SAME_AS、不可換算含量）。"""

    relationship_id: str
    form_entity_id: str
    nutrient_entity_id: str
    relationship_type: str
    source_id: str
    source_url: str | None
    source_locator: str | None
    review_status: str
    retrieved_at: str | None
    amount_conversion_allowed: bool
    automatic_interaction_inheritance: bool


@dataclass
class ReviewedOverlay:
    version: str
    directory: str
    aliases: list[OverlayAlias]
    forms: list[dict[str, str]]                      # reviewed_ingredient_forms.csv 原列
    links: dict[str, FormNutrientLink]               # form_entity_id → link
    sources: dict[str, dict[str, str]]
    file_sha256: dict[str, str] = field(default_factory=dict)


@dataclass
class InteractionCorpus:
    version: str
    directory: str
    entities: dict[str, CorpusEntity]
    aliases: list[CorpusAlias]
    duplicate_templates: dict[str, str]                 # nutrient group entity_id → DUP interaction_id
    file_sha256: dict[str, str] = field(default_factory=dict)
    reviewed_aliases: list[ReviewedAlias] = field(default_factory=list)
    overlay: ReviewedOverlay | None = None

    def __post_init__(self) -> None:
        self._overlay: dict[str, list[OverlayAlias]] = defaultdict(list)
        for alias in (self.overlay.aliases if self.overlay else ()):
            if alias.mapping_context == OVERLAY_PRODUCT_CONTEXT and alias.key:
                self._overlay[alias.key].append(alias)
        self._exact: dict[str, list[tuple[str, MatchMethod, str]]] = defaultdict(list)
        self._display: dict[str, list[tuple[str, str, MatchMethod]]] = defaultdict(list)
        self._contain: list[tuple[str, str, str]] = []  # (alias 比對字串, entity_id, alias_id)
        for alias in self.aliases:
            entity = self.entities.get(alias.entity_id)
            if entity is None or not alias.normalized:
                continue
            usable = alias.review_status == READY and entity.review_status == READY
            if alias.match_policy == EXACT_POLICY and usable:
                method = (MatchMethod.CORPUS_CANONICAL_NAME_EXACT if alias.alias_relation == CANONICAL_RELATION
                          else MatchMethod.CORPUS_SOURCED_SYNONYM_EXACT)
                self._exact[alias.normalized].append((alias.entity_id, method, alias.alias_id))
            else:
                method = (MatchMethod.DISPLAY_TRANSLATION_EXACT if alias.match_policy != EXACT_POLICY
                          else MatchMethod.CORPUS_ALIAS_NOT_READY)
                self._display[alias.normalized].append((alias.entity_id, alias.alias_id, method))
            self._contain.append((_contain_form(alias.alias_text), alias.entity_id, alias.alias_id))
        for reviewed in self.reviewed_aliases:
            if reviewed.entity_id in self.entities and match_key(reviewed.alias_text):
                self._exact[match_key(reviewed.alias_text)].append(
                    (reviewed.entity_id, MatchMethod.REVIEWED_ALIAS_EXACT, reviewed.alias_id))

    # 相同 entity、或其中一個是另一個的 corpus parent → 同一條 identity lineage（不是 SAME_AS）
    def related(self, a: str, b: str) -> bool:
        return a == b or self.parent(a) == b or self.parent(b) == a

    def parent(self, entity_id: str) -> str | None:
        entity = self.entities.get(entity_id)
        return entity.parent_nutrient_entity_id if entity else None

    def nutrient_groups(self, entity_id: str) -> list[str]:
        entity = self.entities.get(entity_id)
        if entity is None:
            return []
        if entity.is_nutrient_group:
            return [entity_id]
        return [entity.parent_nutrient_entity_id] if entity.parent_nutrient_entity_id else []

    @property
    def overlay_version(self) -> str | None:
        return self.overlay.version if self.overlay else None

    def overlay_hits(self, text: str) -> list[OverlayAlias]:
        """整段 token 的 overlay alias（NFKC、casefold、空白合併；不拆括號、不部分包含）。"""
        return list(self._overlay.get(overlay_key(text), []))

    def form_link(self, form_entity_id: str) -> FormNutrientLink | None:
        return self.overlay.links.get(form_entity_id) if self.overlay else None

    def relationship_id(self, form_entity_id: str) -> str:
        """form→nutrient 關係 ID：overlay 審核紀錄（OV1_REL_*），沒有時為 base corpus parent。"""
        link = self.form_link(form_entity_id)
        return link.relationship_id if link else f"CORPUS_PARENT:{form_entity_id}"

    def exact_hits(self, key: str) -> list[tuple[str, MatchMethod, str]]:
        return list(self._exact.get(key, []))

    def display_hits(self, key: str) -> list[tuple[str, str, MatchMethod]]:
        return list(self._display.get(key, []))

    def contained_hits(self, text: str) -> list[tuple[str, str]]:
        form = _contain_form(text)
        head = _head_core(text)
        hits = []
        for needle, entity_id, alias_id in self._contain:
            if not needle:
                continue
            if len(needle) == 1 and _CJK_RE.match(needle):
                # 單一中文字（鈣、鐵、鋅…）在中文裡常是其他詞的一部分（例如膽鈣化醇）；
                # 只有位在名稱主體最後（中文鹽類命名「某酸鋅」的位置）才列為候選。
                if head.endswith(needle):
                    hits.append((entity_id, alias_id))
            elif _contains_bounded(form, needle):
                hits.append((entity_id, alias_id))
        return hits


def _null(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return None if value in ("", "NULL") else value


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verified_sha256(directory: Path, names: Iterable[str], label: str, *, require_sums: bool = False) -> dict[str, str]:
    sums: dict[str, str] = {}
    sums_path = directory / "SHA256SUMS.txt"
    if sums_path.exists():
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                sums[parts[1].lstrip("*")] = parts[0]
    elif require_sums:
        raise ValueError(f"{label} 缺少 SHA256SUMS.txt")
    file_sha = {}
    for name in names:
        digest = _sha256(directory / name)
        if (sums or require_sums) and sums.get(name) != digest:
            raise ValueError(f"{label} 檔案 sha256 不符：{name}")
        file_sha[name] = digest
    return file_sha


def load_reviewed_overlay(directory: str | Path = OVERLAY_DIR) -> ReviewedOverlay:
    """讀 reviewed overlay（alias / form / form→nutrient / sources）。SHA256SUMS 必須存在且相符（fail closed）。

    只做結構檢查；與 base corpus 的一致性（entity 存在、parent 相同）在 `load_interaction_corpus` 檢查。
    """
    import json

    directory = Path(directory)
    file_sha = _verified_sha256(directory, OVERLAY_FILES, "reviewed overlay", require_sums=True)
    validation = json.loads((directory / OVERLAY_VALIDATION_FILE).read_text(encoding="utf-8"))
    version = str(validation.get("overlay_version") or "unknown")

    aliases = []
    for r in _read_csv(directory / OVERLAY_ALIASES_FILE):
        status = r["mapping_status"]
        alias = OverlayAlias(
            alias_id=r["alias_id"], alias_text=r["alias_text"], language=r["language"], alias_status=status,
            entity_id=_null(r.get("entity_id")), candidate_entity_id=_null(r.get("candidate_entity_id")),
            identity_level=_null(r.get("identity_level")), source_id=r["source_id"], source_url=_null(r.get("source_url")),
            source_locator=_null(r.get("source_locator")), retrieved_at=_null(r.get("retrieved_at")),
            match_policy=r["match_policy"], mapping_context=r["mapping_context"], precedence=r["precedence"],
            overlay_version=_null(r.get("overlay_version")) or version,
        )
        if status in OVERLAY_FORMAL_STATUSES:
            if not alias.entity_id or r.get("review_status") != READY:
                raise ValueError(f"overlay 正式 alias 必須有 entity 且 READY：{alias.alias_id}")
        elif status in OVERLAY_CANDIDATE_STATUSES:
            if alias.entity_id:
                raise ValueError(f"overlay 候選 alias 不得有正式 entity：{alias.alias_id}")
        else:
            raise ValueError(f"overlay alias 狀態未知：{alias.alias_id}={status}")
        aliases.append(alias)
    formal_targets: dict[str, set[str]] = defaultdict(set)
    for alias in aliases:
        if alias.is_formal:
            formal_targets[alias.key].add(alias.entity_id)
    if any(len(t) > 1 for t in formal_targets.values()):
        raise ValueError("overlay 同一段文字指向多個正式 entity")

    links = {}
    for r in _read_csv(directory / OVERLAY_LINKS_FILE):
        if r["relationship_type"] != CONTRIBUTES_TO_NUTRIENT:
            raise ValueError(f"overlay form→nutrient 只允許 {CONTRIBUTES_TO_NUTRIENT}：{r['mapping_id']}")
        if r["form_entity_id"] in links:
            raise ValueError(f"overlay form 有多個 parent：{r['form_entity_id']}")
        links[r["form_entity_id"]] = FormNutrientLink(
            relationship_id=r["mapping_id"], form_entity_id=r["form_entity_id"],
            nutrient_entity_id=r["parent_nutrient_entity_id"], relationship_type=r["relationship_type"],
            source_id=r["source_id"], source_url=_null(r.get("source_url")), source_locator=_null(r.get("source_locator")),
            review_status=r["review_status"], retrieved_at=_null(r.get("retrieved_at")),
            amount_conversion_allowed=r.get("amount_conversion_allowed", "").lower() == "true",
            automatic_interaction_inheritance=r.get("automatic_interaction_inheritance", "").lower() == "true",
        )
        if links[r["form_entity_id"]].amount_conversion_allowed or links[r["form_entity_id"]].automatic_interaction_inheritance:
            raise ValueError(f"overlay form→nutrient 不得允許含量換算 / interaction 繼承：{r['mapping_id']}")
    forms = _read_csv(directory / OVERLAY_FORMS_FILE)
    for form in forms:
        if form["relationship_type"] != CONTRIBUTES_TO_NUTRIENT or form["identity_level"] == NUTRIENT_GROUP_LEVEL:
            raise ValueError(f"overlay form 不得是營養素群或 SAME_AS：{form['form_entity_id']}")
        link = links.get(form["form_entity_id"])
        if link is None or link.nutrient_entity_id != form["parent_nutrient_entity_id"]:
            raise ValueError(f"overlay form 缺少一致的 form→nutrient 關係：{form['form_entity_id']}")
    sources = {r["source_id"]: r for r in _read_csv(directory / OVERLAY_SOURCES_FILE)}
    missing_sources = {a.source_id for a in aliases} - set(sources)
    if missing_sources:
        raise ValueError(f"overlay alias 來源不存在：{sorted(missing_sources)}")
    return ReviewedOverlay(version=version, directory=str(directory), aliases=aliases, forms=forms, links=links,
                           sources=sources, file_sha256=file_sha)


def _apply_overlay(entities: dict[str, CorpusEntity], overlay: ReviewedOverlay) -> dict[str, CorpusEntity]:
    """overlay form 併入 entity 表：既有 F_* 只補 overlay identity level（parent 必須相同）；新 F_* 標 REVIEWED_OVERLAY。"""
    import dataclasses

    merged = dict(entities)
    for form in overlay.forms:
        fid, parent = form["form_entity_id"], form["parent_nutrient_entity_id"]
        group = merged.get(parent)
        if group is None or not group.is_nutrient_group:
            raise ValueError(f"overlay form 的 parent 不是 corpus 營養素群：{fid}→{parent}")
        if fid in merged:
            if merged[fid].parent_nutrient_entity_id != parent:
                raise ValueError(f"overlay 與 corpus 的 parent 不一致：{fid}")
            merged[fid] = dataclasses.replace(merged[fid], overlay_identity_level=form["identity_level"])
        else:
            merged[fid] = CorpusEntity(
                entity_id=fid, entity_type=INGREDIENT_ENTITY_TYPE, canonical_name_en=form["canonical_name_en"],
                display_name_zh=_null(form.get("official_name_zh")), identity_level=form["identity_level"],
                parent_nutrient_entity_id=parent, source_id=form["source_id"], source_url=_null(form.get("source_url")),
                review_status=form["review_status"], origin=OVERLAY_ORIGIN, overlay_identity_level=form["identity_level"],
            )
    for alias in overlay.aliases:
        for target in (alias.entity_id, alias.candidate_entity_id):
            if target and target not in merged:
                raise ValueError(f"overlay alias 指向不存在的 entity：{alias.alias_id}→{target}")
    return merged


def load_interaction_corpus(directory: str | Path = CORPUS_DIR,
                            reviewed_aliases: Iterable[ReviewedAlias] = (),
                            overlay: ReviewedOverlay | None = None) -> InteractionCorpus:
    """讀 corpus 的 entity / alias / DUP 模板；SHA256SUMS 不符 → ValueError（fail closed）。

    `overlay`（`load_reviewed_overlay()`）提供時：併入 overlay form，並以 overlay alias 優先比對產品原料 token。
    """
    import json

    directory = Path(directory)
    file_sha = _verified_sha256(directory, CORPUS_FILES, "interaction corpus")
    version = "unknown"
    if (directory / VALIDATION_FILE).exists():
        version = str(json.loads((directory / VALIDATION_FILE).read_text(encoding="utf-8")).get("corpus_version") or version)

    entities = {}
    for row in _read_csv(directory / ENTITIES_FILE):
        if row["entity_type"] != INGREDIENT_ENTITY_TYPE:
            continue  # 本輪不映射 drug substance
        entities[row["entity_id"]] = CorpusEntity(
            entity_id=row["entity_id"], entity_type=row["entity_type"], canonical_name_en=row["canonical_name_en"],
            display_name_zh=_null(row.get("display_name_zh")), identity_level=row["identity_level"],
            parent_nutrient_entity_id=_null(row.get("parent_nutrient_entity_id")), source_id=row["source_id"],
            source_url=_null(row.get("source_url")), review_status=row["review_status"],
        )
    aliases = [
        CorpusAlias(alias_id=r["alias_id"], entity_id=r["entity_id"], alias_text=r["alias_text"], language=r["language"],
                    alias_relation=r["alias_relation"], match_policy=r["match_policy"], source_id=r["source_id"],
                    review_status=r["review_status"])
        for r in _read_csv(directory / ALIASES_FILE) if r["entity_id"] in entities
    ]
    duplicates = {
        r["entity_a_id"]: r["interaction_id"] for r in _read_csv(directory / RECORDS_FILE)
        if r["interaction_type"] == DUPLICATE_INTAKE and r["entity_a_id"] == r["entity_b_id"] and r["entity_a_id"] in entities
    }
    for entity in entities.values():
        if entity.parent_nutrient_entity_id and entity.parent_nutrient_entity_id not in entities:
            raise ValueError(f"interaction corpus parent 不存在：{entity.entity_id}")
    if overlay is not None:
        entities = _apply_overlay(entities, overlay)
    return InteractionCorpus(version=version, directory=str(directory), entities=entities, aliases=aliases,
                             duplicate_templates=duplicates, file_sha256=file_sha,
                             reviewed_aliases=list(reviewed_aliases), overlay=overlay)


# ----------------------------------------------------------------------------- 文字比對（只做格式正規化）


def match_key(text: str | None) -> str:
    """HTML entity 還原 + `normalize_name`（NFKC、小寫、去空白標點）。只消除格式差異。"""
    return normalize_name(html.unescape(text or ""))


def overlay_key(text: str | None) -> str:
    """overlay 宣告的比對規則：NFKC、casefold、空白合併（不去標點、不去括號、不還原 HTML entity）。"""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _contain_form(text: str) -> str:
    """部分包含比對用：NFKC、小寫、標點 / 空白 → 單一空白（保留字詞邊界）。"""
    text = unicodedata.normalize("NFKC", html.unescape(text or "")).lower()
    out = []
    for ch in text:
        out.append(" " if unicodedata.category(ch)[0] in ("P", "S", "Z", "C") else ch)
    return " ".join("".join(out).split())


def _head_core(text: str) -> str:
    """名稱主體：第一個括號之前，去掉尾端數字 / 百分比 / 空白 / 標點。"""
    text = unicodedata.normalize("NFKC", html.unescape(text or ""))
    head = re.split(r"[(\[【（]", text, maxsplit=1)[0]
    return re.sub(r"[\s\d.%~\-+*/:,，、;；]+$", "", head)


def _contains_bounded(haystack: str, needle: str) -> bool:
    """needle 出現在 haystack 中，且英數邊界不被切斷（避免 Vitamin B1 命中 Vitamin B12、iron 命中 environment）。"""
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before_ok = start == 0 or not (_ALNUM.match(needle[0]) and _ALNUM.match(haystack[start - 1]))
        after_ok = end == len(haystack) or not (_ALNUM.match(needle[-1]) and _ALNUM.match(haystack[end]))
        if before_ok and after_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


# ----------------------------------------------------------------------------- 單一文字的判定


@dataclass(frozen=True)
class SurfaceMatch:
    status: MappingStatus
    entity_id: str | None = None                 # 只有正式狀態才有
    candidate_entity_ids: tuple[str, ...] = ()
    method: MatchMethod = MatchMethod.NONE
    alias_ids: tuple[str, ...] = ()
    overlay_status: str | None = None            # overlay alias 狀態（VERIFIED_OFFICIAL…）；非 overlay 為 None
    overlay_precedence: str | None = None


OVERLAY_METHODS = frozenset({MatchMethod.REVIEWED_OVERLAY_ALIAS_EXACT, MatchMethod.REVIEWED_OVERLAY_CANDIDATE})


def _overlay_match(corpus: InteractionCorpus, text: str) -> SurfaceMatch | None:
    """整段 token 有 overlay alias → overlay 判定（優先於原 corpus）；沒有 → None（交回原規則）。

    全部是正式 alias（同一 entity，載入時已驗）→ SUPPORTED_ALIAS；任一列是 AMBIGUOUS / NEEDS_REVIEW → 只有候選。
    """
    hits = corpus.overlay_hits(text)
    if not hits:
        return None
    alias_ids = tuple(sorted(h.alias_id for h in hits))
    if all(h.is_formal for h in hits):
        h = min(hits, key=lambda a: a.alias_id)
        base = _exact_only(corpus, text)
        if base.status == MappingStatus.VERIFIED_EXACT and base.entity_id == h.entity_id:
            # overlay 與 corpus 英文正式名稱指向同一 entity：保留 VERIFIED_EXACT，另記 overlay 佐證
            return SurfaceMatch(MappingStatus.VERIFIED_EXACT, entity_id=h.entity_id, method=base.method,
                                alias_ids=tuple(sorted(set(base.alias_ids) | set(alias_ids))),
                                overlay_status=h.alias_status, overlay_precedence=h.precedence)
        return SurfaceMatch(MappingStatus.SUPPORTED_ALIAS, entity_id=h.entity_id,
                            method=MatchMethod.REVIEWED_OVERLAY_ALIAS_EXACT, alias_ids=alias_ids,
                            overlay_status=h.alias_status, overlay_precedence=h.precedence)
    candidates = tuple(sorted({h.candidate_entity_id or h.entity_id for h in hits} - {None}))
    ambiguous = any(h.alias_status == "AMBIGUOUS" or h.is_formal for h in hits)
    status = MappingStatus.AMBIGUOUS if ambiguous else MappingStatus.NEEDS_REVIEW
    return SurfaceMatch(status, candidate_entity_ids=candidates, method=MatchMethod.REVIEWED_OVERLAY_CANDIDATE,
                        alias_ids=alias_ids, overlay_status=status.value,
                        overlay_precedence=min(h.precedence for h in hits))


def _most_specific(corpus: InteractionCorpus, ids: Iterable[str]) -> list[str]:
    """同一條 lineage 只留最具體的 entity（form 優先於其營養素群）；不同 lineage 全部保留。"""
    ids = sorted(set(ids))
    return [i for i in ids if not any(o != i and corpus.parent(o) == i for o in ids)]


def _single_lineage(corpus: InteractionCorpus, ids: Iterable[str]) -> bool:
    ids = list(set(ids))
    return all(corpus.related(a, b) for a in ids for b in ids)


def _candidates(corpus: InteractionCorpus, hits: list[tuple[str, str]], method: MatchMethod) -> SurfaceMatch:
    ids = _most_specific(corpus, (e for e, _ in hits))
    alias_ids = tuple(sorted({a for _, a in hits}))
    if _single_lineage(corpus, (e for e, _ in hits)):
        return SurfaceMatch(MappingStatus.NEEDS_REVIEW, candidate_entity_ids=tuple(ids), method=method, alias_ids=alias_ids)
    return SurfaceMatch(MappingStatus.AMBIGUOUS, candidate_entity_ids=tuple(ids),
                        method=MatchMethod.CONFLICTING_ENTITIES, alias_ids=alias_ids)


def _exact_only(corpus: InteractionCorpus, text: str) -> SurfaceMatch:
    hits = corpus.exact_hits(match_key(text))
    if not hits:
        return SurfaceMatch(MappingStatus.NO_MATCH)
    ids = {e for e, _, _ in hits}
    if not _single_lineage(corpus, ids):
        return SurfaceMatch(MappingStatus.AMBIGUOUS, candidate_entity_ids=tuple(sorted(ids)),
                            method=MatchMethod.CONFLICTING_ENTITIES, alias_ids=tuple(sorted(a for _, _, a in hits)))
    entity_id = _most_specific(corpus, ids)[0]
    best = min((h for h in hits if h[0] == entity_id), key=lambda h: _METHOD_RANK[h[1]])
    status = (MappingStatus.VERIFIED_EXACT if best[1] == MatchMethod.CORPUS_CANONICAL_NAME_EXACT
              else MappingStatus.SUPPORTED_ALIAS)
    return SurfaceMatch(status, entity_id=entity_id, method=best[1], alias_ids=(best[2],))


def _weak_match(corpus: InteractionCorpus, text: str) -> SurfaceMatch:
    """沒有 exact 時：中文譯名 / 未 READY alias 完全相同 → 候選；alias 部分包含 → 候選；否則 NO_MATCH。"""
    display = corpus.display_hits(match_key(text))
    if display:
        method = min((m for _, _, m in display), key=lambda m: _METHOD_RANK[m])
        return _candidates(corpus, [(e, a) for e, a, _ in display], method)
    contained = corpus.contained_hits(text)
    if contained:
        return _candidates(corpus, contained, MatchMethod.ALIAS_CONTAINED_IN_TEXT)
    return SurfaceMatch(MappingStatus.NO_MATCH)


def _parenthetical(corpus: InteractionCorpus, text: str) -> SurfaceMatch | None:
    """官方原文「X (Y)」：括號內是單一名稱時，兩側分別判定。

    一側 exact、另一側沒有任何其他 lineage 的線索 → 取 exact 側（兩側都 exact 且同 lineage → 取較具體者），
    SUPPORTED_ALIAS / OFFICIAL_PARENTHETICAL。兩側指向不相關 entity → AMBIGUOUS。括號是清單 → 不拆（None）。
    """
    m = _TRAILING_BRACKET_RE.match(text.strip())
    if not m:
        return None
    head, inner = m.group("head").strip(), m.group("inner").strip()
    if not head or not inner or _LIST_CHARS_RE.search(inner):
        return None
    sides = [_exact_only(corpus, head), _exact_only(corpus, inner)]
    if any(s.status == MappingStatus.AMBIGUOUS for s in sides):
        return SurfaceMatch(MappingStatus.AMBIGUOUS, candidate_entity_ids=tuple(sorted(
            {e for s in sides for e in s.candidate_entity_ids})), method=MatchMethod.CONFLICTING_ENTITIES)
    exact = [s for s in sides if s.entity_id]
    if not exact:
        return None
    clue_ids = {s.entity_id for s in exact}
    for side_text, side in zip((head, inner), sides):
        if not side.entity_id:
            weak = _weak_match(corpus, side_text)
            clue_ids.update(weak.candidate_entity_ids)
    if not _single_lineage(corpus, clue_ids):
        return SurfaceMatch(MappingStatus.AMBIGUOUS, candidate_entity_ids=tuple(_most_specific(corpus, clue_ids)),
                            method=MatchMethod.CONFLICTING_ENTITIES)
    exact_ids = _most_specific(corpus, (s.entity_id for s in exact))
    chosen = next(s for s in exact if s.entity_id == exact_ids[0])
    return SurfaceMatch(MappingStatus.SUPPORTED_ALIAS, entity_id=chosen.entity_id,
                        method=MatchMethod.OFFICIAL_PARENTHETICAL, alias_ids=chosen.alias_ids)


def classify_surface(corpus: InteractionCorpus, text: str) -> SurfaceMatch:
    """一段成分文字 → mapping 判定（overlay 整段 alias → exact → 官方括號對照 → 中文譯名 / 部分包含候選 → NO_MATCH）。"""
    if not match_key(text):
        return SurfaceMatch(MappingStatus.NO_MATCH)
    overlay = _overlay_match(corpus, text)
    if overlay is not None:
        return overlay
    exact = _exact_only(corpus, text)
    if exact.status != MappingStatus.NO_MATCH:
        return exact
    paren = _parenthetical(corpus, text)
    if paren is not None:
        return paren
    return _weak_match(corpus, text)


def combine_surfaces(corpus: InteractionCorpus, matches: list[SurfaceMatch]) -> SurfaceMatch:
    """同一個 canonical 成分的多個官方寫法（中文名 / 官方英文名 / 官方別名）合併判定。

    正式結果需全部落在同一 lineage；任一寫法的候選指向其他 lineage → AMBIGUOUS（不猜）。
    """
    if len(matches) == 1 and matches[0].method in OVERLAY_METHODS:
        return matches[0]  # 單一寫法的 overlay 判定原樣保留（含 overlay 狀態）
    formal = [m for m in matches if m.entity_id]
    clue_ids = {m.entity_id for m in formal} | {e for m in matches for e in m.candidate_entity_ids}
    if any(m.status == MappingStatus.AMBIGUOUS for m in matches) or (clue_ids and not _single_lineage(corpus, clue_ids)):
        return SurfaceMatch(MappingStatus.AMBIGUOUS, candidate_entity_ids=tuple(_most_specific(corpus, clue_ids)),
                            method=MatchMethod.CONFLICTING_ENTITIES,
                            alias_ids=tuple(sorted({a for m in matches for a in m.alias_ids})))
    if formal:
        target = _most_specific(corpus, (m.entity_id for m in formal))[0]
        return min((m for m in formal if m.entity_id == target), key=lambda m: _METHOD_RANK[m.method])
    weak = [m for m in matches if m.status == MappingStatus.NEEDS_REVIEW]
    if weak:
        best = min(weak, key=lambda m: _METHOD_RANK[m.method])
        return SurfaceMatch(MappingStatus.NEEDS_REVIEW, candidate_entity_ids=tuple(_most_specific(corpus, clue_ids)),
                            method=best.method, alias_ids=tuple(sorted({a for m in weak for a in m.alias_ids})),
                            overlay_status=best.overlay_status, overlay_precedence=best.overlay_precedence)
    return SurfaceMatch(MappingStatus.NO_MATCH)


# ----------------------------------------------------------------------------- product 層


@dataclass(frozen=True)
class IngredientInput:
    """一個待映射的成分單位（raw 文字原樣保存）。"""

    raw_text: str
    surfaces: tuple[str, ...]            # 比對用的官方寫法（health_food：名稱 / 英文名 / 官方別名；登記類：token）
    source_ingredient_id: str | None = None
    token_index: int | None = None
    amount_raw: str | None = None


@dataclass(frozen=True)
class ProductIngredientSource:
    product_id: str
    product_category: str
    ingredient_source: IngredientSource
    ingredients: tuple[IngredientInput, ...]
    source_parse_status: str | None = None   # 研究 staging 的 READY / NEEDS_REVIEW（解析就緒度，不是 clinical verification）
    source_record_key: str | None = None
    source_url: str | None = None
    retrieved_at: str | None = None
    ingredients_raw: str | None = None        # 登記類原始成分欄位（原樣）
    source_id: str | None = None              # source_registry.id（產品的官方資料集來源）


@dataclass(frozen=True)
class IngredientMapping:
    product_id: str
    ingredient_source: IngredientSource
    raw_text: str
    mapping_status: MappingStatus
    match_method: MatchMethod
    confidence: float
    review_status: ReviewStatus
    corpus_version: str
    entity_id: str | None = None
    entity_type: str | None = None             # graph label：NutrientGroup / IngredientForm（只有正式狀態）
    identity_level: str | None = None          # corpus identity level（NUTRIENT_GROUP / SALT / MIXTURE…）
    candidate_entity_ids: tuple[str, ...] = ()
    matched_alias_ids: tuple[str, ...] = ()
    via_entity_id: str | None = None          # FORM_TO_GROUP：來源 form
    source_ingredient_id: str | None = None
    token_index: int | None = None
    amount_raw: str | None = None
    amount_normalized: float | None = None    # 第一版永遠 None（amount basis 未確認）
    unit: str | None = None
    amount_basis: AmountBasis = AmountBasis.UNKNOWN
    source_record_key: str | None = None
    source_url: str | None = None
    retrieved_at: str | None = None
    source_id: str | None = None
    overlay_version: str | None = None
    alias_evidence_status: str | None = None  # overlay alias 狀態（VERIFIED_OFFICIAL / SUPPORTED_AUTHORITATIVE / AMBIGUOUS / NEEDS_REVIEW）
    overlay_precedence: str | None = None     # EXACT_OVERLAY / PRODUCT_FORM_CONTEXT_OVER_BASE_NUTRIENT_ALIAS
    relationship_id: str | None = None        # FORM_TO_GROUP：overlay CONTRIBUTES_TO_NUTRIENT 審核紀錄

    @property
    def is_formal(self) -> bool:
        return self.mapping_status in FORMAL_STATUSES

    @property
    def mapping_id(self) -> str:
        key = "|".join([self.product_id, self.source_ingredient_id or "", str(self.token_index), self.raw_text,
                        self.mapping_status.value, self.entity_id or ",".join(self.candidate_entity_ids),
                        self.via_entity_id or "", self.corpus_version, self.overlay_version or ""])
        return stable_id("product_ingredient_entity_mapping", key)


@dataclass
class ProductMappingResult:
    source: ProductIngredientSource
    mappings: list[IngredientMapping]

    @property
    def product_id(self) -> str:
        return self.source.product_id

    def formal(self) -> list[IngredientMapping]:
        return [m for m in self.mappings if m.is_formal]

    def contains_targets(self) -> dict[str, list[IngredientMapping]]:
        """正式 CONTAINS 的目標 entity（只有 VERIFIED_EXACT / SUPPORTED_ALIAS；FORM_TO_GROUP 經 form 推導）。"""
        out: dict[str, list[IngredientMapping]] = defaultdict(list)
        for m in self.mappings:
            if m.mapping_status in (MappingStatus.VERIFIED_EXACT, MappingStatus.SUPPORTED_ALIAS):
                out[m.entity_id].append(m)
        return dict(out)

    def nutrient_groups(self) -> dict[str, set[str]]:
        """營養素群 → 本產品經由哪些 entity 對應到（直接 group 或 form）。"""
        out: dict[str, set[str]] = defaultdict(set)
        for m in self.mappings:
            if m.mapping_status in (MappingStatus.VERIFIED_EXACT, MappingStatus.SUPPORTED_ALIAS) and m.entity_id:
                if m.identity_level == NUTRIENT_GROUP_LEVEL:
                    out[m.entity_id].add(m.entity_id)
            elif m.mapping_status == MappingStatus.FORM_TO_GROUP and m.entity_id and m.via_entity_id:
                out[m.entity_id].add(m.via_entity_id)
        return dict(out)


def parse_simple_amount(amount_raw: str | None) -> str | None:
    """只有「單一數值＋單位」才取單位（原樣語意，不換算）；範圍、每 100 mL、百分比等 → None。"""
    if not amount_raw:
        return None
    m = _SIMPLE_AMOUNT_RE.match(unicodedata.normalize("NFKC", amount_raw))
    return _UNIT_CANON[m.group("unit").lower()] if m else None


def _review_status(status: MappingStatus, method: MatchMethod) -> ReviewStatus:
    if status in FORMAL_STATUSES:
        if method == MatchMethod.REVIEWED_ALIAS_EXACT:
            return ReviewStatus.MANUAL_REVIEWED
        if method == MatchMethod.REVIEWED_OVERLAY_ALIAS_EXACT:
            return ReviewStatus.SOURCE_REVIEWED
        return ReviewStatus.AUTO_ACCEPTED_RULE
    if status in CANDIDATE_STATUSES:
        return ReviewStatus.PENDING_REVIEW
    return ReviewStatus.NOT_APPLICABLE


def map_product(corpus: InteractionCorpus, source: ProductIngredientSource) -> ProductMappingResult:
    mappings: list[IngredientMapping] = []
    for ing in source.ingredients:
        match = combine_surfaces(corpus, [classify_surface(corpus, s) for s in ing.surfaces if s])
        common = dict(
            product_id=source.product_id, ingredient_source=source.ingredient_source, raw_text=ing.raw_text,
            corpus_version=corpus.version, source_ingredient_id=ing.source_ingredient_id, token_index=ing.token_index,
            amount_raw=ing.amount_raw, unit=parse_simple_amount(ing.amount_raw),
            source_record_key=source.source_record_key, source_url=source.source_url, retrieved_at=source.retrieved_at,
            source_id=source.source_id, overlay_version=corpus.overlay_version, alias_evidence_status=match.overlay_status,
            overlay_precedence=match.overlay_precedence,
        )
        confidence = (OVERLAY_STATUS_CONFIDENCE[match.overlay_status] if match.method in OVERLAY_METHODS
                      else METHOD_CONFIDENCE[match.method])
        mappings.append(IngredientMapping(
            mapping_status=match.status, match_method=match.method, confidence=confidence,
            review_status=_review_status(match.status, match.method), entity_id=match.entity_id,
            entity_type=corpus.entities[match.entity_id].graph_label if match.entity_id else None,
            identity_level=corpus.entities[match.entity_id].identity_level if match.entity_id else None,
            candidate_entity_ids=match.candidate_entity_ids, matched_alias_ids=match.alias_ids, **common,
        ))
        parent = corpus.parent(match.entity_id) if match.entity_id else None
        if match.status in FORMAL_STATUSES and parent:
            overlay_form = corpus.entities[match.entity_id].origin == OVERLAY_ORIGIN
            mappings.append(IngredientMapping(
                mapping_status=MappingStatus.FORM_TO_GROUP,
                match_method=MatchMethod.REVIEWED_FORM_TO_NUTRIENT if overlay_form else MatchMethod.CORPUS_PARENT_NUTRIENT,
                confidence=confidence, review_status=_review_status(match.status, match.method),
                entity_id=parent, entity_type=corpus.entities[parent].graph_label,
                identity_level=corpus.entities[parent].identity_level,
                via_entity_id=match.entity_id, matched_alias_ids=match.alias_ids,
                relationship_id=corpus.relationship_id(match.entity_id), **common,
            ))
    return ProductMappingResult(source=source, mappings=mappings)


# ----------------------------------------------------------------------------- 成分來源轉換


def health_food_source(product: dict[str, Any], product_ingredients: list[dict[str, Any]],
                       ingredients: dict[str, dict[str, Any]], aliases: dict[str, list[dict[str, Any]]]) -> ProductIngredientSource:
    """既有 canonical ProductIngredient / Ingredient → 比對單位。只用 OFFICIAL_RECORD / MANUAL_REVIEWED 別名。"""
    items = []
    for pi in sorted(product_ingredients, key=lambda r: r["ingredient_id"]):
        ing = ingredients.get(pi["ingredient_id"]) or {}
        surfaces = [ing.get("name_zh"), ing.get("name_en")]
        surfaces += [a["alias"] for a in aliases.get(pi["ingredient_id"], [])
                     if a.get("evidence_status") in ("OFFICIAL_RECORD", "MANUAL_REVIEWED")]
        items.append(IngredientInput(
            raw_text=pi.get("raw_text") or ing.get("name_zh") or "",
            surfaces=tuple(dict.fromkeys(s for s in surfaces if s)),
            source_ingredient_id=pi["ingredient_id"], amount_raw=pi.get("amount_text"),
        ))
    first = product_ingredients[0] if product_ingredients else {}
    return ProductIngredientSource(
        product_id=product["id"], product_category=product.get("product_category") or "health_food",
        ingredient_source=IngredientSource.HEALTH_FOOD_CANONICAL_INGREDIENT, ingredients=tuple(items),
        source_record_key=product.get("source_record_key"), source_url=first.get("source_url") or product.get("source_url"),
        retrieved_at=first.get("retrieved_at") or product.get("retrieved_at"), source_id=product.get("source_id"),
    )


def registration_source(product: dict[str, Any], staging: dict[str, Any] | None) -> ProductIngredientSource:
    """登記類 raw_data.ingredient_staging → 比對單位（研究 token draft；每個 token 原樣、不拆 nested 內容）。"""
    staging = staging or {}
    draft = staging.get("parser_draft") or {}
    tokens = [t for t in (draft.get("tokens") or []) if isinstance(t, str) and t.strip()]
    items = tuple(IngredientInput(raw_text=t, surfaces=(t,), token_index=i) for i, t in enumerate(tokens))
    return ProductIngredientSource(
        product_id=product["id"], product_category=product["product_category"],
        ingredient_source=IngredientSource.TFDA_REGISTRATION_INGREDIENTS_RAW, ingredients=items,
        source_parse_status=staging.get("review_status"), source_record_key=product.get("source_record_key"),
        source_url=product.get("source_url"), retrieved_at=product.get("retrieved_at"),
        ingredients_raw=staging.get("ingredients_raw"), source_id=product.get("source_id"),
    )


# ----------------------------------------------------------------------------- Neo4j / Supabase 計畫（dry-run，不寫入）


def graph_plan(corpus: InteractionCorpus, results: Iterable[ProductMappingResult]) -> dict[str, Any]:
    """正式 bridge 的 graph 形狀：
    (Product)-[:CONTAINS]->(IngredientForm|NutrientGroup)；(IngredientForm)-[:CONTRIBUTES_TO_NUTRIENT]->(NutrientGroup)。
    候選（AMBIGUOUS / NEEDS_REVIEW）不建立邊；不建立 Interaction、SAME_AS 或 Product↔Product 關係。
    """
    contains = []
    for result in results:
        for entity_id, ms in sorted(result.contains_targets().items()):
            first = ms[0]
            contains.append({
                "product_id": result.product_id, "entity_id": entity_id,
                "target_label": corpus.entities[entity_id].graph_label,
                "source": first.ingredient_source.value, "raw_text": first.raw_text,
                "raw_texts": sorted({m.raw_text for m in ms}),
                "mapping_status": first.mapping_status.value, "match_method": first.match_method.value,
                "confidence": max(m.confidence for m in ms), "review_status": first.review_status.value,
                "amount_raw": first.amount_raw, "amount_normalized": None, "unit": first.unit,
                "amount_basis": first.amount_basis.value, "corpus_version": first.corpus_version,
                "overlay_version": first.overlay_version, "alias_ids": sorted({a for m in ms for a in m.matched_alias_ids}),
                "alias_evidence_status": first.alias_evidence_status,
                "entity_origin": corpus.entities[entity_id].origin,
            })
    contributes = []
    for e in sorted(corpus.entities.values(), key=lambda e: e.entity_id):
        if not e.parent_nutrient_entity_id:
            continue
        link = corpus.form_link(e.entity_id)
        contributes.append({
            "form_entity_id": e.entity_id, "nutrient_entity_id": e.parent_nutrient_entity_id,
            "relationship_type": CONTRIBUTES_TO_NUTRIENT,
            "basis": "CORPUS_PARENT_NUTRIENT" if e.origin == BASE_ORIGIN else "REVIEWED_OVERLAY",
            "relationship_id": corpus.relationship_id(e.entity_id),
            "source_id": link.source_id if link else e.source_id,
            "source_url": link.source_url if link else e.source_url,
            "source_locator": link.source_locator if link else None,
            "review_status": link.review_status if link else e.review_status,
            "amount_conversion_allowed": False, "automatic_interaction_inheritance": False,
            "corpus_version": corpus.version, "overlay_version": corpus.overlay_version,
        })
    nodes: dict[str, int] = defaultdict(int)
    for entity in corpus.entities.values():  # 全部 corpus ingredient entity（之後審核通過的候選只需加邊）
        nodes[entity.graph_label] += 1
    return {
        "nodes": dict(nodes), "contains": contains, "contributes_to_nutrient": contributes,
        "relationship_counts": {"CONTAINS": len(contains), "CONTRIBUTES_TO_NUTRIENT": len(contributes),
                                "SAME_AS": 0, "INTERACTS_WITH": 0, "PRODUCT_TO_INTERACTION": 0},
        "interaction_nodes": 0,
    }
