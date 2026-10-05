"""Multi-image Fusion（deterministic）：把 Vision 對每張圖的觀察融合成同一份 `FusedImageBundle`。

一般化規則（不看任何特定產品 / 字號 / 名稱）：

* 同欄位一致 → 合併，保留所有來源圖片；某張缺值、另一張有清楚值 → 採可讀值（unreadable 不採用）。
* 名稱「部分一致但不完整」（正規化後互相包含）→ 不算衝突，各 observation 都保留，由後續 resolver 判斷。
* 許可證字號：兩個以上「清楚且不同」的字號 → `MULTI_IMAGE_IDENTITY_CONFLICT`（停止產品查證，不任選其一）；
  低信心的不同字號只是 `OCR_UNCERTAIN`（不採用低信心值）。
* 明確不同的完整產品名（互相不包含、也不是錯字層級的差異），且兩者的來源圖片之間沒有共同的
  許可證字號 / 公司 / 品牌佐證 → `SAME_PRODUCT_UNCERTAIN`（請使用者確認是否拍到不同產品，不自行融合）。
* 錯字層級的差異 → `OCR_UNCERTAIN`（非關鍵，可依既有 identity 證據優先序繼續）。
* 結果與圖片順序無關：值的排序只看信心 / 長度 / 文字，來源圖片編號排序後輸出。

這裡只整理「觀察」，不確認產品；產品是否確認仍由 Agent B 的 deterministic resolver 決定。
"""

from dataclasses import dataclass
from difflib import SequenceMatcher

from app.normalization import extract_permit_code, normalize_name
from app.schemas.image_bundle import (
    FusedImageBundle,
    FusedValue,
    ImageBundleObservation,
    ImageBundleStatus,
    ImageConflictCode,
    ImageFieldConflict,
    ImageObservation,
)

CONFIDENT = 0.6  # 低於此值的讀值視為不確定（不能構成「清楚不同」的衝突）
TYPO_SIMILARITY = 0.75  # 正規化後相似度達此值、長度差 ≤ 2 → 視為辨識錯字層級的差異
TYPO_MAX_LENGTH_GAP = 2


@dataclass
class _Mention:
    text: str
    image: int
    confidence: float
    complete: bool | None = None


def _clamp(value: float) -> float:
    return min(max(value or 0.0, 0.0), 1.0)


def _mentions(observations: list[ImageObservation], attr: str) -> list[_Mention]:
    out = []
    for obs in observations:
        for item in getattr(obs, attr):
            text = (item.value or "").strip()
            if item.readable and text:
                out.append(_Mention(text, obs.image_index, _clamp(item.confidence), getattr(item, "is_complete", None)))
    return out


def _group(mentions: list[_Mention], key) -> list[FusedValue]:
    """同一個 key（正規化後相同）的讀值合併成一筆；順序與圖片順序無關。"""
    buckets: dict[str, list[_Mention]] = {}
    for m in mentions:
        k = key(m.text)
        if k:
            buckets.setdefault(k, []).append(m)
    fused = []
    for k, items in buckets.items():
        best = max(items, key=lambda m: (m.confidence, len(m.text), m.text))
        completes = [m.complete for m in items if m.complete is not None]
        fused.append(FusedValue(
            value=best.text, observed_in=sorted({m.image for m in items}), readable=True,
            confidence=max(m.confidence for m in items),
            is_complete=any(completes) if completes else None))
    return sorted(fused, key=lambda v: (-v.confidence, -len(normalize_name(v.value)), normalize_name(v.value)))


def _permit_key(text: str) -> str:
    return extract_permit_code(text) or normalize_name(text)


def _contains(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a in b or b in a)


def _typo_level(a: str, b: str) -> bool:
    return (abs(len(a) - len(b)) <= TYPO_MAX_LENGTH_GAP
            and SequenceMatcher(None, a, b).ratio() >= TYPO_SIMILARITY)


def _compatible(a: FusedValue, b: FusedValue) -> bool:
    ka, kb = normalize_name(a.value), normalize_name(b.value)
    return _contains(ka, kb) or _typo_level(ka, kb)


def _clusters(values: list[FusedValue]) -> list[list[FusedValue]]:
    """把「互相相容（包含或錯字層級）」的值連成同一群；群之間才是真的不同。"""
    parent = list(range(len(values)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            if _compatible(values[i], values[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[FusedValue]] = {}
    for i, v in enumerate(values):
        groups.setdefault(find(i), []).append(v)
    return sorted(groups.values(), key=lambda g: (-max(v.confidence for v in g), normalize_name(g[0].value)))


def _has_typo_variants(cluster: list[FusedValue]) -> bool:
    """群內是否有「不是單純包含」的寫法差異（= 辨識錯字層級）。"""
    keys = [normalize_name(v.value) for v in cluster]
    return any(not _contains(keys[i], keys[j]) for i in range(len(keys)) for j in range(i + 1, len(keys)))


def _images_of(cluster: list[FusedValue]) -> set[int]:
    return {i for v in cluster for i in v.observed_in}


def _is_full_name(cluster: list[FusedValue]) -> bool:
    return any(v.confidence >= CONFIDENT and v.is_complete is not False for v in cluster)


def _shared_evidence(a: set[int], b: set[int], evidence: list[list[FusedValue]]) -> bool:
    """兩群名稱各自的來源圖片之間，是否有同一個許可證字號 / 公司 / 品牌在兩邊都出現。"""
    for values in evidence:
        for v in values:
            seen = set(v.observed_in)
            if seen & a and seen & b:
                return True
    return False


def fuse_image_bundle(observation: ImageBundleObservation, image_count: int) -> FusedImageBundle:
    # 只接受有效的圖片編號；同一編號重複 → 一併處理（讀值各自保留）
    valid = [o for o in observation.images if 1 <= o.image_index <= image_count]
    kinds = {}
    for o in sorted(valid, key=lambda o: o.image_index):
        kinds.setdefault(o.image_index, o.image_kind)

    fields = {a: _mentions(valid, a) for a in (
        "product_names", "brands", "companies", "permit_numbers", "ingredients", "claims", "warnings",
        "certification_marks", "other_visible_text")}
    readable_images = {m.image for ms in fields.values() for m in ms}
    unreadable = [i for i in range(1, image_count + 1) if i not in readable_images]

    conflicts: list[ImageFieldConflict] = []

    # ---- 許可證字號（identity-critical）
    permits = _group(fields["permit_numbers"], _permit_key)
    confident = [p for p in permits if p.confidence >= CONFIDENT]
    status = ImageBundleStatus.FUSED
    if len(confident) >= 2:
        conflicts.append(ImageFieldConflict(field="permit_number", code=ImageConflictCode.MULTI_IMAGE_IDENTITY_CONFLICT,
                                            values=confident, identity_critical=True))
        status = ImageBundleStatus.IDENTITY_CONFLICT
    elif confident and len(permits) > len(confident):
        # 一個清楚的字號 + 低信心的不同讀值：不採用低信心值，只標 OCR_UNCERTAIN
        conflicts.append(ImageFieldConflict(field="permit_number", code=ImageConflictCode.OCR_UNCERTAIN, values=permits))
        permits = confident
    elif len(permits) > 1:
        conflicts.append(ImageFieldConflict(field="permit_number", code=ImageConflictCode.OCR_UNCERTAIN, values=permits))

    # ---- 品牌 / 公司（非 identity-critical 的欄位衝突）
    brands = _group(fields["brands"], normalize_name)
    companies = _group(fields["companies"], normalize_name)
    for name, values in (("brand", brands), ("company", companies)):
        clusters = _clusters(values)
        if len(clusters) > 1:
            conflicts.append(ImageFieldConflict(field=name, code=ImageConflictCode.FIELD_VALUE_CONFLICT, values=values))
        elif clusters and _has_typo_variants(clusters[0]):
            conflicts.append(ImageFieldConflict(field=name, code=ImageConflictCode.OCR_UNCERTAIN, values=values))

    # ---- 產品名稱
    names = _group(fields["product_names"], normalize_name)
    clusters = _clusters(names)
    for cluster in clusters:
        if len(cluster) > 1 and _has_typo_variants(cluster):
            conflicts.append(ImageFieldConflict(field="product_name", code=ImageConflictCode.OCR_UNCERTAIN, values=cluster))
    full = [c for c in clusters if _is_full_name(c)]
    if len(full) > 1 and status == ImageBundleStatus.FUSED:
        evidence = [permits, companies, brands]
        for i in range(len(full)):
            for j in range(i + 1, len(full)):
                a, b = _images_of(full[i]), _images_of(full[j])
                values = full[i] + full[j]
                if _shared_evidence(a, b, evidence):
                    conflicts.append(ImageFieldConflict(
                        field="product_name", code=ImageConflictCode.FIELD_VALUE_CONFLICT, values=values))
                else:
                    conflicts.append(ImageFieldConflict(
                        field="product_name", code=ImageConflictCode.SAME_PRODUCT_UNCERTAIN, values=values,
                        identity_critical=True))
                    status = ImageBundleStatus.SAME_PRODUCT_UNCERTAIN

    claims = _group(fields["claims"], normalize_name)
    ingredients = _group(fields["ingredients"], normalize_name)
    warnings = _group(fields["warnings"], normalize_name)
    marks = _group(fields["certification_marks"], normalize_name)
    other = _group(fields["other_visible_text"], normalize_name)

    if status == ImageBundleStatus.FUSED and not any((names, brands, companies, permits, ingredients, claims, warnings)):
        status = ImageBundleStatus.NO_READABLE_CONTENT

    return FusedImageBundle(
        status=status, image_count=image_count, image_kinds=kinds, unreadable_images=unreadable,
        product_names=names, brands=brands, companies=companies, permit_numbers=permits,
        ingredients=ingredients, claims=claims, warnings=warnings, certification_marks=marks,
        other_visible_text=other, conflicts=conflicts)


def candidate_product_names(fused: FusedImageBundle) -> list[str]:
    """交給 resolver 的產品名稱線索：只有「品名」欄位的讀值（成分 / 宣稱 / 其他文字不會成為產品候選）。

    同一群內被更完整的寫法完全包含、且自己不是完整名稱的片段不送（它沒有額外資訊）；
    錯字層級的不同寫法都保留，由 resolver 與字號證據判斷。
    """
    out: list[str] = []
    for cluster in _clusters(fused.product_names):
        keys = {id(v): normalize_name(v.value) for v in cluster}
        for v in cluster:
            fragment = v.is_complete is False and any(
                keys[id(v)] != keys[id(o)] and keys[id(v)] in keys[id(o)] for o in cluster)
            if not fragment and v.value not in out:
                out.append(v.value)
    return out


BUNDLE_CONFIRM_ASK = "請確認這幾張照片是不是同一個產品；如果是不同產品，請分開傳送，我再逐一查證。"


def bundle_stop_message(fused: FusedImageBundle) -> tuple[str, list[str]]:
    """身份衝突 / 可能不是同一產品時給使用者看的說明（固定模板；只引用融合觀察中的值與來源圖片）。"""
    critical = next((c for c in fused.conflicts if c.identity_critical), None)
    if critical is None:
        return "這幾張照片目前沒有辨識到可用來查證的產品資訊。", ["請補拍清楚的產品正面或許可證字號區域。"]
    shown = ("照片上出現的字號：" if critical.field == "permit_number" else "照片上出現的品名：") + "、".join(
        v.value for v in critical.values)  # 來源圖片編號只保留在結構化資料，不顯示在使用者介面
    if critical.code == ImageConflictCode.MULTI_IMAGE_IDENTITY_CONFLICT:
        return ("這幾張照片上出現了不同的許可證字號，可能拍到不同的產品，所以我先不查證。",
                [shown, BUNDLE_CONFIRM_ASK])
    return ("這幾張照片上的產品名稱明顯不同，而且找不到共同的許可證字號、公司或品牌，所以我先不把它們當成同一個產品。",
            [shown, BUNDLE_CONFIRM_ASK])


def render_fused_content(fused: FusedImageBundle) -> str:
    """給 Agent A 語意理解的文字：由融合後的結構化觀察產生（每項都標示來源圖片），不是各圖 OCR 全文串接。"""

    def refs(v: FusedValue) -> str:
        return "圖" + "、".join(str(i) for i in v.observed_in)

    def line(label: str, values: list[FusedValue]) -> str | None:
        if not values:
            return None
        return f"{label}：" + "；".join(f"{v.value}（{refs(v)}）" for v in values)

    lines = [f"以下是同一個 bundle 的 {fused.image_count} 張照片，融合後的觀察（括號為來源圖片）："]
    for label, values in (("產品名稱", fused.product_names), ("品牌", fused.brands), ("公司", fused.companies),
                          ("許可證字號", fused.permit_numbers), ("認證標章", fused.certification_marks),
                          ("包裝宣稱", fused.claims), ("警語", fused.warnings), ("其他文字", fused.other_visible_text)):
        if text := line(label, values):
            lines.append(text)
    by_source: dict[tuple[int, ...], list[str]] = {}
    for v in fused.ingredients:  # 成分依來源圖片分組，避免每個成分都重複標示
        by_source.setdefault(tuple(v.observed_in), []).append(v.value)
    for sources, items in sorted(by_source.items()):
        lines.append("成分表：" + "、".join(items) + "（圖" + "、".join(str(i) for i in sources) + "）")
    if fused.unreadable_images:
        lines.append("無法辨識的圖片：" + "、".join(f"圖{i}" for i in fused.unreadable_images))
    for c in fused.conflicts:
        if c.code == ImageConflictCode.OCR_UNCERTAIN:
            lines.append(f"辨識不確定（{c.field}）：" + " / ".join(f"{v.value}（{refs(v)}）" for v in c.values))
    return "\n".join(lines)
