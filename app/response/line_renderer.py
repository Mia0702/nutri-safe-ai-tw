"""AdaptiveLineRenderer：把 Agent C 的 ResponsePayload / DetailPayload 轉成「一則」LINE Flex Message。

UI 原則
* LINE 原生、白色 / 中性色；不使用大面積綠 / 橘 / 紅背景，風險只用 🟢 🟠 🔴 標示。
* 只有 response_type=RISK 才顯示風險等級；說法查證（CLAIM_RESULT）另以 🟢 / 🟠 / 🔴 標示「宣稱風險」
  （取自 payload.claim_risk_level，renderer 不自行推論），產品資訊等其他型態不顯示風險燈。
* 版面：標題區 / 結論 / 重點 / 附註各自是一個 section（margin + separator 分隔）；每個重點是獨立的 box
  （符號與內容分開的 text），不把整段內容塞進同一個 Text。所有 text wrap + scaling，內文 md、輔助資訊 sm。
* Primary card 只放：結果標題、一句結論、最多 3 個重點、必要警語、按鈕。
  超出的內容留在 payload，由按鈕 on-demand 取得（template 不限制 Agent C 的完整內容）。
* 每個按鈕都是帶 request_id 的 postback（app/response/actions.py）；唯一例外是資料來源卡的「開啟來源」
  URI 按鈕，網址只來自該 request 保存的來源 metadata 且通過 safe_source_url 驗證。
* query_label（例如「圖片辨識｜…」）由原 request 保存，標示這張卡片在回答哪一則輸入。

本模組只負責排版，不呼叫任何 Agent / Tool / 資料來源。
"""

import re
from typing import Any, Callable

from app.response.actions import ACTION_LABELS, build_postback_data, build_select_product_data
from app.response.sources import SourceLink, safe_source_url
from app.schemas.enums import ResponseAction, ResponseType
from app.schemas.response import CLAIM_RISK_LABEL, RISK_EMOJI, DetailPayload, ProductChoice, ResponsePayload

MAX_KEY_POINTS = 3
MAX_WARNINGS = 2
MAX_BUTTONS = 3
MAX_POINT_CHARS = 80
MAX_ALT_TEXT_CHARS = 400
MAX_DETAIL_PARAGRAPHS = 30
MAX_DETAIL_PARAGRAPH_CHARS = 1000
MAX_QUERY_LABEL_CHARS = 30
MAX_SOURCE_ENTRIES = 8
MAX_SOURCE_TITLE_CHARS = 60
OFFICIAL_SOURCE_TEXT = "官方來源"
EXTERNAL_SOURCE_TEXT = "外部來源"
OPEN_OFFICIAL_LABEL = "開啟官方來源"
OPEN_EXTERNAL_LABEL = "開啟原始頁面"
NO_SOURCES_TEXT = "這次沒有可列出的資料來源。"

# Typography（LINE 字體大小設定友善）：所有 text / button 都 scaling=true，text 一律 wrap；
# 各 template 只指定「角色」，實際大小集中在這裡
TEXT_SIZES = {
    "section": "sm",  # 卡片類型標籤（風險判斷 / 產品資訊 …）
    "title": "xl",  # 標題
    "body": "md",  # 結論、重點、候選名稱
    "note": "sm",  # 次要說明、警語、來源 / 查詢標示
}
BUTTON_HEIGHT = "md"
# 多行文字的行距（只加在內文 / 輔助資訊；大字時仍清楚分行）
LINE_SPACING = {"body": "4px", "note": "2px"}
SECTION_MARGIN = "lg"
BULLET = "・"
# 原始文字自帶的「純列表編號」（1. / 1、/ (1) / （1））：顯示時改由 bullet component 表示，不改原始資料
SUB_BULLET = "◦"  # 列表子項目（與第一層重點區分，不算在 FC-E8 的 3 個重點內）
MAX_LIST_ITEMS = 6  # primary card 每個重點最多展開的子項目數；其餘提示「還有 N 項」
# 句中的列表編號：只在行首或標點 / 空白之後，且編號後不是數字（排除 1.5、0.5%、第1條、日期、字號）
_ITEM_MARK_RE = re.compile(r"(?:^|(?<=[\s:：。；;，,]))(?:[(（]\s*(\d{1,2})\s*[)）]|(\d{1,2})\s*(?:[.．](?!\d)|、))")
_LIST_NUMBER_RE = re.compile(r"^(?:[(（]\s*\d{1,2}\s*[)）]|\d{1,2}\s*(?:[.．](?!\d)|[、)）]))\s*")
_MARKERS = tuple(RISK_EMOJI.values())

COLOR_TITLE = "#111111"
COLOR_BODY = "#333333"
COLOR_MUTED = "#888888"
COLOR_SEPARATOR = "#EEEEEE"

TYPE_LABELS = {
    ResponseType.RISK: "風險判斷",
    ResponseType.PRODUCT_INFO: "產品資訊",
    ResponseType.INGREDIENT_LIST: "成分資訊",
    ResponseType.CLAIM_RESULT: "說法查證",
    ResponseType.SOURCE_SUMMARY: "官方資料",
    ResponseType.MISSING_INFORMATION: "需要補充",
    ResponseType.OUT_OF_SCOPE: "呷安心",
    ResponseType.ERROR: "查詢未完成",
}


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(text: str, role: str = "body", *, color: str = COLOR_BODY, weight: str = "regular",
          **extra: Any) -> dict[str, Any]:
    """所有文字都換行（不截斷）且跟隨 LINE 字體大小設定放大；不使用 shrink-to-fit 縮小內文。"""
    node = {"type": "text", "text": text, "size": TEXT_SIZES[role], "color": color, "weight": weight,
            "wrap": True, "scaling": True, **extra}
    if role in LINE_SPACING:
        node["lineSpacing"] = LINE_SPACING[role]
    return node


def _section(contents: list[dict[str, Any]], margin: str = SECTION_MARGIN) -> dict[str, Any]:
    """一個內容區塊（標題區 / 結論 / 重點 / 附註）；區塊之間用 margin + separator 分開。"""
    return {"type": "box", "layout": "vertical", "spacing": "sm", "margin": margin, "contents": contents}


def _bullet(text: str, role: str = "body", *, color: str = COLOR_BODY) -> dict[str, Any]:
    """一個重點 = 一個 box：符號（・ 或 🟢🟠🔴）與內容是分開的 text，內容換行時不會跑到符號下方。"""
    marker, content = BULLET, text.strip()
    for mark in _MARKERS:
        if content.startswith(mark):
            marker, content = mark, content[len(mark):].strip()
            break
    if content.startswith(BULLET):
        content = content[len(BULLET):].strip()
    content = _LIST_NUMBER_RE.sub("", content, count=1) or content
    return {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
        _text(marker, role, color=color, flex=0),
        _text(content, role, color=color, flex=1),
    ]}


def split_list_items(text: str) -> tuple[str, list[str]]:
    """DISPLAY-ONLY：把「說明:1.… 2.… 3.…」拆成（前言, [項目…]）；不是列表時回傳 (text, [])。

    只接受從 1 開始、依序遞增的編號，且至少 2 項（一般句中的數字、單一編號不拆）。不修改任何原始資料。
    """
    text = (text or "").strip()
    marks, expected = [], 1
    for m in _ITEM_MARK_RE.finditer(text):
        if int(m.group(1) or m.group(2)) == expected:
            marks.append(m)
            expected += 1
    if len(marks) < 2:
        return text, []
    items = [text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))].strip(" 　\n;；,，")
             for i, m in enumerate(marks)]
    if not all(items):
        return text, []
    return text[:marks[0].start()].strip(), items


def _sub_bullet(text: str, role: str = "body", *, color: str = COLOR_BODY) -> dict[str, Any]:
    return {"type": "box", "layout": "horizontal", "spacing": "sm", "paddingStart": "lg", "margin": "sm", "contents": [
        _text(SUB_BULLET, role, color=color, flex=0),
        _text(text, role, color=color, flex=1),
    ]}


def _list_rows(text: str, *, bulleted: bool, clip: int | None = None,
               max_items: int | None = None) -> list[dict[str, Any]]:
    """一段文字（可能含列表）→ 前言（text 或 bullet）＋ 每個列表項目一個獨立 component。"""
    def cut(value: str) -> str:
        return _clip(value, clip) if clip else value

    body = text.strip()
    if bulleted and body.startswith(BULLET):
        body = body[len(BULLET):].strip()
    intro, items = split_list_items(body)
    if not items:
        return [_bullet(cut(text)) if bulleted else _text(cut(text))]
    rows = [_bullet(cut(intro)) if bulleted else _text(cut(intro))] if intro else []
    shown = items[:max_items] if max_items else items
    item_row = _sub_bullet if intro else _bullet  # 沒有前言時，列表項目本身就是第一層 bullet
    rows += [item_row(cut(item)) for item in shown]
    if len(items) > len(shown):
        rows.append(_text(f"還有 {len(items) - len(shown)} 項，可按下方按鈕查看", "note", color=COLOR_MUTED))
    return rows


def _sections(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把非空的區塊依序排好，區塊之間插入 separator。"""
    out: list[dict[str, Any]] = []
    for group in groups:
        if not group:
            continue
        if out:
            out.append(_separator())
        out.append(_section(group, margin="none" if not out else SECTION_MARGIN))
    return out


def _button_component(action: dict[str, Any], style: str = "link", **extra: Any) -> dict[str, Any]:
    """按鈕跟隨字體設定放大；label 過長時 shrink-to-fit 只是保險，label 本身仍維持短字（ACTION_LABELS 等）。"""
    return {"type": "button", "style": style, "height": BUTTON_HEIGHT, "adjustMode": "shrink-to-fit",
            "scaling": True, **extra, "action": action}


def _separator() -> dict[str, Any]:
    return {"type": "separator", "margin": SECTION_MARGIN, "color": COLOR_SEPARATOR}


def _button(action: ResponseAction, request_id: str) -> dict[str, Any]:
    label = ACTION_LABELS[action]
    return _button_component(
        {"type": "postback", "label": label, "data": build_postback_data(action, request_id), "displayText": label})


def _bubble(body: list[dict[str, Any]], footer: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    bubble: dict[str, Any] = {
        "type": "bubble", "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body},
    }
    if footer:
        bubble["footer"] = {"type": "box", "layout": "vertical", "spacing": "none", "contents": footer}
    return bubble


def _message(alt_text: str, bubble: dict[str, Any]) -> dict[str, Any]:
    return {"type": "flex", "altText": _clip(alt_text, MAX_ALT_TEXT_CHARS), "contents": bubble}


def _points(payload: ResponsePayload) -> list[dict[str, Any]]:
    """Primary card 最多 3 個重點（FC-E8），每個重點是獨立的 box。"""
    shown = payload.key_points[:MAX_KEY_POINTS]
    rows = [row for p in shown for row in _list_rows(p, bulleted=True, clip=MAX_POINT_CHARS, max_items=MAX_LIST_ITEMS)]
    hidden = len(payload.key_points) - len(shown)
    if hidden > 0:
        rows.append(_text(f"還有 {hidden} 項，可按下方按鈕查看", "note", color=COLOR_MUTED))
    return rows


def _standard_body(payload: ResponsePayload) -> list[dict[str, Any]]:
    return _sections([_text(payload.summary, "body", color=COLOR_BODY)], _points(payload))


def _source_summary_body(payload: ResponsePayload) -> list[dict[str, Any]]:
    return _standard_body(payload)


MAX_MISSING_INFO_POINTS = 6


RECOGNIZED_HEADING = "圖片辨識到："


def _recognized_rows(payload: ResponsePayload) -> list[dict[str, Any]]:
    if not payload.recognized_content:
        return []
    return [_text(RECOGNIZED_HEADING, "note", color=COLOR_MUTED, weight="bold"),
            *[_text(f"・{line}", "note", color=COLOR_MUTED) for line in payload.recognized_content]]


def _minimal_body(payload: ResponsePayload) -> list[dict[str, Any]]:
    return [_text(payload.summary, "body")] + [_text(p, "note", color=COLOR_MUTED) for p in payload.key_points[:1]]


def _missing_info_body(payload: ResponsePayload) -> list[dict[str, Any]]:
    """需要補充：要讓使用者看到所有可選的官方候選（例如同名的多個品項）才能回覆。"""
    recognized = _recognized_rows(payload)
    return [_text(payload.summary, "body"), *([_separator(), *recognized, _separator()] if recognized else [])] + [
        _text(p, "note", color=COLOR_MUTED) for p in payload.key_points[:MAX_MISSING_INFO_POINTS]
    ]


# response_type → body template；新增型態只需在這裡註冊
TEMPLATES: dict[ResponseType, Callable[[ResponsePayload], list[dict[str, Any]]]] = {
    ResponseType.RISK: _standard_body,
    ResponseType.PRODUCT_INFO: _standard_body,
    ResponseType.INGREDIENT_LIST: _standard_body,
    ResponseType.CLAIM_RESULT: _standard_body,
    ResponseType.SOURCE_SUMMARY: _source_summary_body,
    ResponseType.MISSING_INFORMATION: _missing_info_body,
    ResponseType.OUT_OF_SCOPE: _minimal_body,
    ResponseType.ERROR: _minimal_body,
}


MAX_INLINE_CHOICES = 3  # 超過就改用一則 carousel
MAX_CHOICE_BUTTON_CHARS = 20  # LINE action label 上限


def _choice_detail(choice: ProductChoice) -> str:
    parts = [f"許可證字號 {choice.permit_number}" if choice.permit_number else "無許可證字號"]
    if not choice.is_active:
        parts.append(f"⚠️ {choice.status_text}（仍可查詢）")
    return "・".join(parts)


def _choice_rows(choice: ProductChoice, request_id: str) -> list[dict[str, Any]]:
    label = f"選擇：{choice.label}"
    return [
        _text(choice.label, "body", color=COLOR_TITLE, weight="bold"),
        *([_text(choice.full_name, "note", color=COLOR_MUTED)] if choice.full_name != choice.label else []),
        _text(_choice_detail(choice), "note", color=COLOR_MUTED),
        _button_component({"type": "postback", "label": _clip(label, MAX_CHOICE_BUTTON_CHARS),
                           "data": build_select_product_data(request_id, choice.product_id),
                           "displayText": _clip(f"選擇：{choice.full_name}", MAX_ALT_TEXT_CHARS)},
                          style="secondary", margin="sm"),
    ]


def _paragraph_rows(paragraph: str) -> list[dict[str, Any]]:
    """明細的一段 = 一個 section；段內每一行是獨立的 component（・／🟢🟠🔴 開頭的行是 bullet）。"""
    rows = []
    for line in (paragraph or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        rows += _list_rows(line, bulleted=line.startswith((BULLET, *_MARKERS)))  # 句中的 1. 2. 3. 拆成獨立項目
    return rows


def _query_label_row(query_label: str | None) -> list[dict[str, Any]]:
    return [_text(_clip(query_label, MAX_QUERY_LABEL_CHARS), "note", color=COLOR_MUTED)] if query_label else []


def _uri_button(label: str, url: str) -> dict[str, Any] | None:
    safe = safe_source_url(url)  # renderer 再驗證一次：不合格不建立按鈕
    if safe is None:
        return None
    return _button_component({"type": "uri", "label": label, "uri": safe})


def _source_rows(link: SourceLink) -> list[dict[str, Any]]:
    kind = OFFICIAL_SOURCE_TEXT if link.is_official else EXTERNAL_SOURCE_TEXT
    rows = [_text(_clip(link.title, MAX_SOURCE_TITLE_CHARS), "body", color=COLOR_TITLE, weight="bold"),
            _text(f"{kind}｜{link.domain}" if link.domain else kind, "note", color=COLOR_MUTED)]
    button = _uri_button(OPEN_OFFICIAL_LABEL if link.is_official else OPEN_EXTERNAL_LABEL, link.url) if link.url else None
    return rows + ([button] if button else [])


class AdaptiveLineRenderer:
    def heading(self, payload: ResponsePayload) -> str:
        if payload.response_type == ResponseType.RISK and payload.risk_level is not None:
            return f"{RISK_EMOJI[payload.risk_level]} {payload.title}"
        return payload.title

    @staticmethod
    def claim_badge(payload: ResponsePayload) -> str | None:
        """說法查證的宣稱風險標示（只取 payload.claim_risk_level）。"""
        if payload.response_type == ResponseType.CLAIM_RESULT and payload.claim_risk_level is not None:
            return CLAIM_RISK_LABEL[payload.claim_risk_level]
        return None

    def render_choices(self, payload: ResponsePayload, query_label: str | None = None) -> dict[str, Any]:
        """產品確認卡：候選 <= 3 放在同一張卡片；更多候選用一則 carousel（仍是一則訊息）。
        Flex 按鈕只處理這一題；「開始新問題 / 結束本次查證」仍由 Quick Reply 負責。"""
        heading = self.heading(payload)
        intro = [_text(TYPE_LABELS[payload.response_type], "section", color=COLOR_MUTED),
                 *_query_label_row(query_label),
                 _text(heading, "title", color=COLOR_TITLE, weight="bold"),
                 _text(payload.summary, "body", color=COLOR_BODY)]
        note = [_text(payload.choice_note, "note", color=COLOR_MUTED)] if payload.choice_note else []
        recognized = _recognized_rows(payload)
        if recognized:
            intro = intro + [_separator(), *recognized]
        alt = f"{heading}：{payload.summary}" + "、".join(c.full_name for c in payload.product_choices)
        choices = payload.product_choices
        if len(choices) <= MAX_INLINE_CHOICES:
            body = list(intro)
            for choice in choices:
                body += [_separator(), *_choice_rows(choice, payload.request_id)]
            return _message(alt, _bubble(body + ([_separator()] + note if note else [])))
        bubbles = [_bubble(intro + [_text("請左右滑動，點選你要查的品項。", "note", color=COLOR_MUTED)] + note)]
        bubbles += [_bubble(_choice_rows(choice, payload.request_id)) for choice in choices]
        return {"type": "flex", "altText": _clip(alt, MAX_ALT_TEXT_CHARS),
                "contents": {"type": "carousel", "contents": bubbles}}

    def render_primary(self, payload: ResponsePayload, query_label: str | None = None) -> dict[str, Any]:
        """每個 request 唯一的 primary response：一則 Flex Message（query_label 標示回答的是哪一則輸入）。"""
        if payload.product_choices:
            return self.render_choices(payload, query_label)
        heading = self.heading(payload)
        badge = self.claim_badge(payload)
        header = [
            _text(TYPE_LABELS[payload.response_type], "section", color=COLOR_MUTED),
            *_query_label_row(query_label),
            _text(heading, "title", color=COLOR_TITLE, weight="bold"),
            *([_text(badge, "body", color=COLOR_TITLE, weight="bold")] if badge else []),
        ]
        notes = [_text(_clip(w, 120), "note", color=COLOR_MUTED) for w in payload.warnings[:MAX_WARNINGS]]
        if payload.source_count:
            notes.append(_text(f"官方資料來源 {payload.source_count} 筆", "note", color=COLOR_MUTED))
        body = [_section(header, margin="none"), _separator(), *TEMPLATES[payload.response_type](payload)]
        if notes:
            body += [_separator(), _section(notes)]
        buttons = [_button(a, payload.request_id) for a in payload.available_actions[:MAX_BUTTONS]]
        title = f"{heading}｜{badge}" if badge else heading
        alt = f"{query_label}｜{title}：{payload.summary}" if query_label else f"{title}：{payload.summary}"
        return _message(alt, _bubble(body, buttons))

    def render_sources(self, links: list[SourceLink], query_text: str = "") -> dict[str, Any]:
        """資料來源卡：每筆來源顯示標題 / 網域 / 官方或外部；有合格網址才有「開啟」URI 按鈕。"""
        body = [_text("資料來源", "title", color=COLOR_TITLE, weight="bold")]
        if query_text:
            body.append(_text(f"查詢：{_clip(query_text, MAX_QUERY_LABEL_CHARS)}", "note", color=COLOR_MUTED))
        shown = links[:MAX_SOURCE_ENTRIES]
        for link in shown:
            body += [_separator(), *_source_rows(link)]
        if not shown:
            body += [_separator(), _text(NO_SOURCES_TEXT)]
        if len(links) > len(shown):
            body.append(_text(f"（另有 {len(links) - len(shown)} 筆未顯示）", "note", color=COLOR_MUTED))
        alt = "資料來源：" + ("、".join(link.title for link in shown) or NO_SOURCES_TEXT)
        return _message(alt, _bubble(body))

    def render_detail(self, detail: DetailPayload) -> dict[str, Any]:
        """按鈕觸發的 on-demand 內容：一則 Flex Message，標明屬於哪一題。"""
        header = [_text(detail.title, "title", color=COLOR_TITLE, weight="bold")]
        if detail.query_text:
            header.append(_text(f"查詢：{_clip(detail.query_text, MAX_QUERY_LABEL_CHARS)}", "note", color=COLOR_MUTED))
        paragraphs = detail.body[:MAX_DETAIL_PARAGRAPHS]
        body = _sections(header, *[_paragraph_rows(_clip(p, MAX_DETAIL_PARAGRAPH_CHARS)) for p in paragraphs])
        if len(detail.body) > len(paragraphs):
            body.append(_text(f"（另有 {len(detail.body) - len(paragraphs)} 項未顯示）", "note", color=COLOR_MUTED))
        buttons = [_button(a, detail.request_id) for a in detail.available_actions[:MAX_BUTTONS]]
        return _message(f"{detail.title}：{detail.body[0] if detail.body else ''}", _bubble(body, buttons))

    def render_notice(self, text: str, retry_request_id: str | None = None,
                      query_label: str | None = None) -> dict[str, Any]:
        """按鈕已失效、格式錯誤等系統提示；需要時附「重新查詢」。"""
        footer = [_button(ResponseAction.RETRY, retry_request_id)] if retry_request_id else None
        return _message(text, _bubble([*_query_label_row(query_label), _text(text, "body")], footer))
