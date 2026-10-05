"""「檢視包裝宣稱」：包裝上辨識到的內容（PACKAGE OBSERVATION / SCREENING，不是官方查證）。

只使用圖片分析時已保存在 request 的結構化觀察（observed_package_claims / 成分 / 警語 / 其他可見文字 / 來源圖片編號），
deterministic 整理；不重跑 Vision / Controller，不查產品資料庫、RAG、官方網站，也不與核准功效逐項比對。

呈現語意（只是篩選提示，不改變任何產品風險）：
* 一般成分 / 營養補充文字 → 中性
* 一般、尚未查證的功效宣稱 → 橘色「建議多留意」
* 明確的疾病治療 / 診斷 / 取代醫療等說法 → 沿用既有 deterministic 條件（`is_asserted_disease_claim`）標示高風險說法；
  只標示這一句宣稱，不把整個產品升級為 HIGH。
"""

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from app.risk.claim_policy import is_asserted_disease_claim
from app.schemas.enums import ClaimSemanticCategory, Intent
from app.schemas.user_request import ExtractedClaim, NormalizedUserRequest

PACKAGE_OBSERVATION_NOTICE = "以下為照片中辨識到的文字，尚未與官方資料逐項比對。"
PACKAGE_OBSERVATION_TITLE = "包裝上辨識到的內容"
ORANGE_HINT = "建議多留意"
RED_HINT = "疾病療效說法，屬高風險說法"


class ObservationTone(StrEnum):
    NEUTRAL = "NEUTRAL"
    ORANGE = "ORANGE"
    RED = "RED"


class ObservedClaimItem(BaseModel):
    text: str
    tone: ObservationTone
    source_images: list[int] = Field(default_factory=list)


class ObservedTextItem(BaseModel):
    text: str
    source_images: list[int] = Field(default_factory=list)


class PackageObservation(BaseModel):
    # 照片上看到的識別文字（產品名稱 / 品牌 / 公司 / 許可證字號）：只是「照片辨識到的文字」，沒有經過官方查證不代表它們是官方資料
    product_names: list[ObservedTextItem] = Field(default_factory=list)
    brands: list[ObservedTextItem] = Field(default_factory=list)
    companies: list[ObservedTextItem] = Field(default_factory=list)
    permit_numbers: list[ObservedTextItem] = Field(default_factory=list)
    claims: list[ObservedClaimItem] = Field(default_factory=list)
    ingredients: list[ObservedTextItem] = Field(default_factory=list)
    warnings: list[ObservedTextItem] = Field(default_factory=list)
    other_text: list[ObservedTextItem] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.product_names or self.brands or self.companies or self.permit_numbers or self.claims
                    or self.ingredients or self.warnings or self.other_text)


_PROVENANCE_RE = re.compile(r"\s*[（(]\s*圖\s*\d+(?:\s*[、,，]\s*\d+)*\s*[）)]")


class ObservationFocus(StrEnum):
    """前一張圖片的追問只顯示相關的觀察區塊（由既有 intent 決定，不看任何字句）。"""

    ALL = "ALL"
    INGREDIENTS = "INGREDIENTS"
    CLAIMS = "CLAIMS"
    WARNINGS = "WARNINGS"


_FOCUS_BY_INTENT = {
    Intent.INGREDIENT_INQUIRY: ObservationFocus.INGREDIENTS,
    Intent.CLAIM_VERIFICATION: ObservationFocus.CLAIMS,
    Intent.EFFICACY_INQUIRY: ObservationFocus.CLAIMS,
    Intent.SAFETY_INQUIRY: ObservationFocus.WARNINGS,
}


def focus_for_intent(intent: Intent) -> ObservationFocus:
    return _FOCUS_BY_INTENT.get(intent, ObservationFocus.ALL)


def focus_items(observation: "PackageObservation", focus: ObservationFocus) -> list:
    return {ObservationFocus.INGREDIENTS: observation.ingredients, ObservationFocus.CLAIMS: observation.claims,
            ObservationFocus.WARNINGS: observation.warnings}.get(focus) or (
        [] if focus != ObservationFocus.ALL else (
            observation.product_names + observation.brands + observation.companies + observation.permit_numbers
            + observation.claims + observation.ingredients + observation.warnings + observation.other_text))


def observed_ingredient_presence(asked: list[str], observation: "PackageObservation") -> list[tuple[str, bool]]:
    """使用者問的成分是否出現在照片辨識到的成分文字中（只比對正規化後的文字包含；不是官方成分資料）。"""
    from app.normalization import normalize_name

    observed = [normalize_name(i.text) for i in observation.ingredients]
    out = []
    for name in asked:
        key = normalize_name(name)
        out.append((name, bool(key) and any(key in o or (o and o in key) for o in observed)))
    return out


def display_text(text: str) -> str:
    """只給使用者看辨識到的文字：Agent A 若把給它的來源標示（例如「（圖1）」）一起抄進文字，這裡去掉。"""
    return _PROVENANCE_RE.sub("", text or "").strip()


def claim_tone(claim: ExtractedClaim) -> ObservationTone:
    if is_asserted_disease_claim(claim):
        return ObservationTone.RED
    if claim.semantic_category == ClaimSemanticCategory.NUTRIENT_SUPPLEMENT:
        return ObservationTone.NEUTRAL
    return ObservationTone.ORANGE


def _dedupe(items: list[ObservedTextItem]) -> list[ObservedTextItem]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = display_text(item.text)
        if key and key not in seen:
            seen.add(key)
            out.append(item.model_copy(update={"text": key}))
    return out


def build_package_observation(request: NormalizedUserRequest) -> PackageObservation:
    """只讀 request 上已保存的圖片觀察（不呼叫任何 Agent / Tool / 資料來源）。"""
    package_claims = list(request.observed_package_claims) + (list(request.claims) if request.claims_from_package else [])
    claims, seen = [], set()
    for c in package_claims:
        text = display_text(c.claim_text)
        if text and text not in seen:
            seen.add(text)
            claims.append(ObservedClaimItem(text=text, tone=claim_tone(c), source_images=list(c.source_images)))
    bundle = request.image_bundle
    if bundle is not None:  # multi-image：每項保留來源圖片
        def fused(values):
            return [ObservedTextItem(text=v.value, source_images=v.observed_in) for v in values]

        names, brands, companies, permits = (fused(bundle.product_names), fused(bundle.brands), fused(bundle.companies),
                                             fused(bundle.permit_numbers))
        ingredients, warnings, other = fused(bundle.ingredients), fused(bundle.warnings), fused(bundle.other_visible_text)
    else:
        def plain(values):
            return [ObservedTextItem(text=t) for t in values]

        names, brands, companies, permits = (plain(request.product_candidates), plain(request.brand_candidates),
                                             plain(request.company_candidates), plain(request.permit_number_candidates))
        ingredients, warnings, other = plain(request.ingredient_candidates), [], []
    other += [ObservedTextItem(text=t) for t in request.other_visible_text]
    names, brands, companies, permits = _dedupe(names), _dedupe(brands), _dedupe(companies), _dedupe(permits)
    ingredients, warnings = _dedupe(ingredients), _dedupe(warnings)
    # 其他文字不重複前面已列出的內容（品名 / 品牌 / 宣稱 / 成分…）
    from app.normalization import normalize_name

    shown = {normalize_name(i.text) for i in names + brands + companies + permits + ingredients + warnings}
    shown |= {normalize_name(c.text) for c in claims}
    other = [o for o in _dedupe(other) if normalize_name(o.text) not in shown]
    return PackageObservation(product_names=names, brands=brands, companies=companies, permit_numbers=permits,
                              claims=claims, ingredients=ingredients, warnings=warnings, other_text=other)
