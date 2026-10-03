"""官方「保健功效相關成分」原文 → Ingredient 結構化解析。

設計原則：
- 只用針對「文字格式」的一般性規則（分隔符號、括號、列表編號、含量／單位、標籤用語），
  不針對任何產品名稱。
- 原文永遠保留（IngredientParseResult.raw_text）；無法可靠判斷的片段放進 unresolved_segments，
  不捏造成分。
- 含量、份量、劑型、規格、說明文字不建立成 Ingredient。

流程：原文 → 切片段（segment）→ 去除編號／份量／標籤前綴 → 切出含量 → 名稱驗證。
"""

import re
import unicodedata
from enum import Enum

from pydantic import BaseModel, Field

from app.normalization import normalize_name


class IngredientParseStatus(str, Enum):
    PARSED = "PARSED"                        # 所有片段都成功解析或可確定為非成分（標題／含量／規格）
    PARTIALLY_PARSED = "PARTIALLY_PARSED"    # 至少一個成分，但仍有無法解析的片段
    UNPARSED = "UNPARSED"                    # 有原文，但沒有可靠成分
    NO_SOURCE_INGREDIENT = "NO_SOURCE_INGREDIENT"  # 官方沒有提供成分原文


class ParsedIngredient(BaseModel):
    name_zh: str
    name_en: str | None = None
    amount_text: str | None = None
    source_segment: str


class IngredientParseResult(BaseModel):
    status: IngredientParseStatus
    raw_text: str
    ingredients: list[ParsedIngredient] = Field(default_factory=list)
    unresolved_segments: list[str] = Field(default_factory=list)


_OPEN, _CLOSE = "(（「『【[", ")）」』】]"
_CJK = r"一-鿿"
# 立體化學標記 (-)、(+)、(±) 屬於名稱本身，先保護起來避免被當成括號
_STEREO_RE = re.compile(r"[（(]([+\-±])[)）]")
_STEREO_MARK = {"+": "", "-": "", "±": ""}
_STEREO_RESTORE = {v: f"({k})" for k, v in _STEREO_MARK.items()}

_NUM = r"\d+(?:[.,]\d+)*"
_UNIT = (
    r"(?:mg|g|μg|ug|mL|ml|IU|CFU|cfu|cells|ppm|%|毫克|亳克|微克|公克|克|毫升|個|億個|億|"
    r"活性力價|單位|粒|顆|錠|包|瓶)"
)
# 含量：可有比較符號、範圍／誤差、科學記號，最後一定要有單位
_AMOUNT_RE = re.compile(
    rf"(?:(?:[≧≥>＞<≦≤]|大於|小於|不低於|不少於)\s*)?[(（]?{_NUM}\s*(?:[~～〜\-–±]\s*{_NUM}\s*)?(?:[x×*]\s*10\^?\d*\s*)?(?:{_NUM}\s*)?[)）]?\s*{_UNIT}"
    rf"(?![A-Za-z])"
)
_AMOUNT_ONLY_RE = re.compile(rf"^[\s(（]*(?:{_AMOUNT_RE.pattern})[\s\S]*$")

# 列表編號：1. / 1、 / (1) / (1). / A. （後面不能直接接數字，避免切到 1.0）
_MARKER_RE = re.compile(r"^(?:[（(]\d{1,2}[)）]\.?|\d{1,2}[.、](?!\d)|[A-Za-z][.、](?![A-Za-z\d]))\s*")
_INLINE_MARKER_RE = re.compile(r"(?:(?<=\s)|(?<=^)|(?<=[。.;]))(?=(?:[（(]\d{1,2}[)）]|\d{1,2}[.、](?!\d))\s*\S)")

_SERVING_UNIT = r"(?:份量|份|顆|粒|錠|包|瓶|罐|條|匙|軟膠囊|膠囊|毫升|公克|克)"
_SERVING_PREFIX_RE = re.compile(
    rf"^(?:本品|本產品)?每(?:一)?(?:\s*{_NUM}\s*(?:mg|g|mL|ml|毫升|公克|克|毫克)(?:/{_SERVING_UNIT})?"
    rf"|\s*(?:{_NUM}|一|兩|二|三|四)?\s*{_SERVING_UNIT}(?:/{_SERVING_UNIT})?"
    rf"|\s*±\s*{_NUM}\s*%?|\s*[（(][^）)]*[)）])+\s*(?:之?(?:內容物)?含量)?\s*中?(?:含有|含)?\s*[:,]?\s*"
)
_LABEL_RE = re.compile(
    r"^(?:保健功效|品管|品質管制|保健品管|功效|保健)?(?:之)?(?:相關)?(?:品管)?(?:指標)?(?:之相關)?成[分份](?:含量)?\s*[:,]?\s*"
    r"|^(?:品管|品質管制)指標(?:之相關成[分份])?\s*[:,]?\s*"
    r"|^(?:本產品|本品)(?:之)?(?:保健功效)?(?!以)"
    r"|^含量(?:為)?\s*:?\s*"
)
_DOSAGE_FORM_RE = re.compile(r"^(?:膠囊|粉劑|錠劑|液劑|顆粒|粉末|飲品)\s*[-:]\s*")
_INDICATOR_PREFIX_RE = re.compile(r"^(?:本產品|本品)?(?:之)?以")
_INDICATOR_SUFFIX_RE = re.compile(r"(?:為|作為|做為|作)[^:,;]{0,15}?(?:指標(?:成[分份])?|成[分份])(?:之相關成[分份])?[。.]?$")
_NAME_TAIL_RE = re.compile(r"(?:之?每份內容物含量為|每[^每]{0,8}?含有?|含量為|含量|成[分份]|規格|[一二三四五六兩\d]種\S*|以上|達|數|量大於|大於|約|為|作|每)$")
# 「飽和」是化學名詞的一部分（不飽和脂肪酸），不是並列連接詞
_CONJ_RE = re.compile(r"(?<=[^\s])\s*(?:及|(?<!飽)和|與)\s*(?=[^\s])")
_LATIN_PAREN_RE = re.compile(rf"^(?P<zh>.*?)\s*[（(](?P<en>[A-Za-zͰ-Ͽ][^（）(){_CJK}]*)[）)]\s*$")
_PAREN_GROUP_RE = re.compile(r"\s*[（(「『【\[][^（）()「」『』【】\[\]]*[）)」』】\]]")

# 非成分：規格、劑型、說明。只要名稱中出現就不當成 Ingredient。
_NON_INGREDIENT_WORDS = ("純度", "規格", "賦形劑", "膠囊殼", "其中", "本產品", "本品", "綜合結果", "動物實驗", "扣除")
# 含量前的名稱若是這些，代表是重量／規格說明而非成分
_SPEC_NAME_RE = re.compile(r"^(?:共|總重|淨重|重量|內容物|膠囊|軟膠囊|粒|顆|錠)$|內容物|純度|規格|膠囊殼")
# 單一字母代號（例如「A菌」）只是標籤，有英文全名時以全名為準
_LETTER_LABEL_RE = re.compile(r"^[A-Za-z]菌$")
# 純說明文字（不是成分，也不算解析失敗）
_EXPLANATION_WORDS = ("綜合結果",)
# 名稱中不應出現的框架用字（代表抽取不乾淨）
_FORBIDDEN_IN_NAME = ("每", "含量", "指標", "成分", "成份", "為", ":", "。", "以", "#")


# Word「Symbol」字型的希臘字母會落在私用區（U+F061 起），常見於官方文件轉出的文字
_SYMBOL_FONT_GREEK = str.maketrans({"": "α", "": "β", "": "γ", "": "δ", "": "μ", "": "ω"})


def _clean(text: object) -> str:
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text).translate(_SYMBOL_FONT_GREEK)).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", " ").replace("　", " ")
    return "\n".join(line.strip() for line in text.split("\n")).strip()


_PAIRS = ("()", "（）", "「」", "『』", "【】", "[]")


def _balance_brackets(text: str) -> str:
    """官方原文偶有括號未閉合；未閉合的那一種括號直接移除，避免整段被當成括號內文字。"""
    for o, c in _PAIRS:
        if text.count(o) != text.count(c):
            text = text.replace(o, "").replace(c, "")
    return text


def _leading_group(s: str) -> tuple[str, str] | None:
    """字串開頭若是一組平衡括號，回傳 (括號內文字, 其餘文字)。"""
    if not s or s[0] not in _OPEN:
        return None
    depth = 0
    for i, ch in enumerate(s):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
            if depth == 0:
                return s[1:i].strip(), s[i + 1:].strip()
    return None


def _depth_split(text: str, is_sep) -> list[str]:
    parts, buf, depth = [], [], 0
    for i, ch in enumerate(text):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if depth == 0 and is_sep(text, i):
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _is_separator(text: str, i: int) -> bool:
    ch = text[i]
    if ch in "、;\n。":
        # 小數點以外的「。」才切；句號後接數字的情形極少，仍視為分隔
        return True
    if ch in ",，":
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        # 數字中的千分位或化學位置編號（4,000 / 2,3,4-）不切
        return not (prev.isdigit() and nxt.isdigit())
    return False


def _join_continuations(text: str) -> str:
    """把「名稱:」與下一行的含量接回同一行；下一行若是含量或「(以…計)」說明也接回。"""
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if out and line and (
            out[-1].endswith(":") or _AMOUNT_RE.match(line) or line.startswith(("(以", "（以"))
        ):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return "\n".join(out)


def split_segments(text: str) -> list[str]:
    """括號外以、，,；;。換行與行內列表編號切分。"""
    text = _join_continuations(text)
    segments: list[str] = []
    for line in _depth_split(text, lambda t, i: t[i] in ";\n。"):
        for item in (s.strip() for s in _INLINE_MARKER_RE.split(line) if s.strip()):
            for part in _depth_split(_drop_title(item), _is_separator):
                # 空白後接立體化學標記 (-)/(+) 代表另一個化合物開始
                segments.extend(p.strip() for p in re.split(r"\s+(?=[-])", part) if p.strip())
    return segments


def _drop_title(item: str) -> str:
    """「標題:成分、成分…」→ 去掉標題（功效、劑型、標籤）。

    只在結構明確時處理：標題沒有含量、冒號後不是直接接含量，
    且冒號只有一個（或第二個冒號後是含量，例如「功效:名稱:含量」）。
    """
    colon = _depth_split(item, lambda t, i: t[i] == ":")
    if len(colon) < 2 or _AMOUNT_RE.search(colon[0]):
        return item
    if len(colon) > 3 or (len(colon) == 3 and not _AMOUNT_RE.match(_SERVING_PREFIX_RE.sub("", colon[2]))):
        return item
    if _AMOUNT_RE.match(_SERVING_PREFIX_RE.sub("", colon[1])):
        return item
    return item[item.index(colon[1], len(colon[0])):]


def _strip_framing(seg: str) -> str:
    """去除列表編號、劑型標題、份量前綴、標籤用語與「以…為指標成分」框架。"""
    prev = None
    s = seg.strip()
    while s != prev:
        prev = s
        s = _MARKER_RE.sub("", s).strip(" .-:")
        s = re.sub(r"^【[^】]*】", "", s).strip()
        s = _DOSAGE_FORM_RE.sub("", s)
        s = _SERVING_PREFIX_RE.sub("", s)
        s = _LABEL_RE.sub("", s)
        if s.startswith("含") and len(s) > 2 and not s.startswith("含量"):
            s = s[2:] if s.startswith("含有") else s[1:]
        s = s.lstrip("#*※ ")
        group = _leading_group(s)
        if group and group[0].startswith("每"):
            # 開頭「(每份…)」份量說明括號 → 拿掉
            s = group[1]
        elif group and (not group[1].strip(" 。.") or _leading_group(group[1]) == (group[1][1:-1].strip(), "")):
            # 整段被括號／引號包住（後面頂多再接一組補充說明括號）→ 解開
            s = group[0]
    s = _INDICATOR_PREFIX_RE.sub("", s)
    s = _INDICATOR_SUFFIX_RE.sub("", s).strip()
    # 「以X為指標成分:含量」：指標用語在含量之前
    s = re.sub(r"(?:為|作為)(?:品管|品質管制)?指標(?:成分)?(?=\s*:)", "", s)
    return s.strip()


def _cut_amount(s: str) -> tuple[str, str]:
    """回傳 (名稱, 含量)。含量在前時（例如「0.2 mg X」）取後面為名稱。"""
    parts = _depth_split(s, lambda t, i: t[i] == ":")
    if len(parts) > 1:
        last = parts[-1]
        if _AMOUNT_RE.match(_SERVING_PREFIX_RE.sub("", last)):
            # 名稱:含量（前面可能還有功效標題，例如「功效:名稱:含量」）
            return parts[-2], last
        if _AMOUNT_RE.search(last):
            # 標題:…:名稱含量 —— 最後一段自帶含量
            s = last
        elif len(parts) == 2:
            # 標題:名稱 —— 例如「A菌:某菌名」，取右邊
            s = last
        else:
            # 多個冒號又沒有含量，無法可靠判斷
            return s, ""
    lead = _AMOUNT_RE.match(s)
    if lead:
        rest = re.sub(r"^\s*中?含有?\s*", "", s[lead.end():]).strip()
        if _AMOUNT_RE.search(rest):
            # 開頭是份量（例如「105毫升含有X 1470毫克」）→ 以後面的含量為準
            return _cut_amount(rest)
        return rest, s[: lead.end()].strip()
    for m in _AMOUNT_RE.finditer(s):
        start = m.start()
        before = s[start - 1] if start else ""
        # 名稱內的數字（維生素D3、Q10、NTU101）前面是英數，不能當含量起點
        if before and (before.isalnum() and not ("一" <= before <= "鿿")) and s[start] not in "≧≥>＞<≦≤":
            continue
        return s[:start].strip(), s[start:].strip()
    return s, ""


def _split_name(name: str) -> tuple[str, str | None]:
    """拆出括號內英文同義名；移除其餘說明性括號。"""
    name_en = None
    m = _LATIN_PAREN_RE.match(name)
    if m and m.group("zh").strip():
        name, name_en = m.group("zh").strip(), m.group("en").strip()
    group = _leading_group(name)
    if group and not group[1]:
        # 名稱只有一組括號／引號（例如「「某成分」」）→ 取其內容
        name = group[0]
    prev = None
    while prev != name:
        prev = name
        name = _PAREN_GROUP_RE.sub("", name).strip()
    m = _LATIN_PAREN_RE.match(name)
    if m and m.group("zh").strip() and not name_en:
        name, name_en = m.group("zh").strip(), m.group("en").strip()
    return name, name_en


def _clean_name(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = _NAME_TAIL_RE.sub("", name).strip(" -.:,;、。")
    name = re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _restore(text: str) -> str:
    for mark, original in _STEREO_RESTORE.items():
        text = text.replace(mark, original)
    return text


def is_valid_ingredient_name(name: str) -> bool:
    if not name or not re.search(rf"[A-Za-z{_CJK}]", name):
        return False
    cjk = len(re.findall(rf"[{_CJK}]", name))
    if cjk > 20 or len(name) > 60:
        return False
    # 數字開頭只允許化學位置編號（2,3,4-… / 4-…）
    if name[0].isdigit() and not re.match(r"^\d+(?:,\d+)*-", name):
        return False
    if any(ch in name for ch in _OPEN + _CLOSE):
        return False
    if any(w in name for w in _NON_INGREDIENT_WORDS + _FORBIDDEN_IN_NAME):
        return False
    if _AMOUNT_RE.fullmatch(name.strip()):
        return False
    return True


def _is_non_ingredient(seg: str) -> bool:
    """片段本身只是標題、份量、含量或規格（不是成分，也不算解析失敗）。"""
    if not seg:
        return True
    if any(w in seg for w in _EXPLANATION_WORDS):
        return True
    # 整段是「(…以…計)」計算基準說明
    if re.fullmatch(r"[（(][^（）()]*以[\s\S]*計[)）][。.]?", seg):
        return True
    seg = _PAREN_GROUP_RE.sub("", seg).replace("(", "").replace(")", "")
    if not _AMOUNT_ONLY_RE.match(seg):
        return False
    # 以含量開頭：拿掉含量、單位與份量用字後，若沒有像名稱的文字（≥3 個中文字或英文字母）就是純含量
    rest = re.sub(rf"{_AMOUNT_RE.pattern}|{_UNIT}|每|以上|計", "", seg)
    return not re.search(rf"[{_CJK}]{{3,}}|[A-Za-z]{{3,}}", rest)


def _parse_segment(seg: str) -> tuple[list[ParsedIngredient], bool]:
    """回傳 (成分, 是否為可忽略的非成分片段)。"""
    if _is_non_ingredient(seg):
        return [], True
    body = _strip_framing(seg)
    if _is_non_ingredient(body):
        return [], True
    # 整段被括號包住（例如「(某成分) :含量」或補充說明行）→ 解開
    m = re.match(r"^[（(]([^（）()]+)[)）]\s*(.*)$", body)
    if m and (not m.group(2) or m.group(2).lstrip().startswith(":")):
        body = (m.group(1) + m.group(2)).strip()
    name_part, amount = _cut_amount(body)
    name_part = _strip_framing(name_part)
    if _SPEC_NAME_RE.search(_clean_name(_PAREN_GROUP_RE.sub("", name_part))):
        return [], True
    results: list[ParsedIngredient] = []
    # 「A及B」並列 → 多個成分（只在括號外切）
    pieces = _depth_split(name_part, lambda t, i: bool(_CONJ_RE.match(t, i)) and t[i] in "及和與")
    if len(pieces) > 1 and not all(is_valid_ingredient_name(_clean_name(_split_name(p)[0])) for p in pieces):
        pieces = [name_part]
    for piece in pieces:
        name, name_en = _split_name(piece)
        name = _clean_name(name)
        if (not name or _LETTER_LABEL_RE.match(name)) and name_en:
            name, name_en = name_en, None
        if not is_valid_ingredient_name(name):
            return [], False
        results.append(ParsedIngredient(
            name_zh=_restore(name), name_en=_restore(name_en) if name_en else None,
            amount_text=_restore(amount) if amount and len(pieces) == 1 else None,
            source_segment=_restore(seg),
        ))
    if not results:
        return [], _is_non_ingredient(name_part)
    return results, False


def parse_ingredient_text(raw: object) -> IngredientParseResult:
    raw_text = _clean(raw)
    if not raw_text:
        return IngredientParseResult(status=IngredientParseStatus.NO_SOURCE_INGREDIENT, raw_text="")
    text = _balance_brackets(_STEREO_RE.sub(lambda m: _STEREO_MARK[m.group(1)], raw_text))

    ingredients: list[ParsedIngredient] = []
    unresolved: list[str] = []
    index: dict[str, ParsedIngredient] = {}
    for seg in split_segments(text):
        parsed, ignorable = _parse_segment(seg)
        if not parsed:
            if not ignorable:
                unresolved.append(_restore(seg))
            continue
        for ing in parsed:
            keys = {normalize_name(ing.name_zh)} | ({normalize_name(ing.name_en)} if ing.name_en else set())
            existing = next((index[k] for k in keys if k in index), None)
            if existing:
                # 同一成分的中英文或不同份量重複出現 → 合併，不重複建立
                existing.name_en = existing.name_en or (ing.name_en if ing.name_en != existing.name_zh else None) \
                    or (ing.name_zh if ing.name_zh != existing.name_zh else None)
                existing.amount_text = existing.amount_text or ing.amount_text
                index.update({k: existing for k in keys})
                continue
            ingredients.append(ing)
            index.update({k: ing for k in keys})

    if not ingredients:
        status = IngredientParseStatus.UNPARSED
    elif unresolved:
        status = IngredientParseStatus.PARTIALLY_PARSED
    else:
        status = IngredientParseStatus.PARSED
    return IngredientParseResult(status=status, raw_text=raw_text, ingredients=ingredients, unresolved_segments=unresolved)
