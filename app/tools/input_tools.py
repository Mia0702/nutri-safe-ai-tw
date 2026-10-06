"""Agent A（Multimodal Intake / Understanding）的 Input Tools。

Input Tool 只把非文字輸入轉成「可理解的文字內容」，不做查證、不判斷風險：
* analyze_image()：讀出圖片中的文字並描述包裝 / 廣告內容（OpenAI 圖片理解）
* transcribe_audio()：語音轉文字（OpenAI 語音轉錄）
* fetch_url_content()：抓取使用者提供網址的正文

文字輸入不需要 Input Tool。所有輸入最後都由 Agent A 的同一段語意理解轉成 NormalizedUserRequest，
不為 image / audio / URL 各建一套分析 pipeline。

使用者提供的網址多半是廣告或社群頁：內容只能用來抽取「對方宣稱了什麼」，查證仍由 Agent B 回到官方證據。
"""

import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

from app.config.project import USER_AGENT
from app.schemas.enums import ErrorCode, UrlSourceRole
from app.schemas.image_bundle import MAX_BUNDLE_IMAGES, MIN_BUNDLE_IMAGES, ImageBundleObservation
from app.services.errors import ServiceError
from app.services.openai_service import MediaClient
from app.services.text_normalization import normalize_transcript
from app.services.url_access import REASON_ACCESS_RESTRICTED, REASON_INVALID_URL, REASON_NETWORK, REASON_UNSUPPORTED
from app.tools.html_text import html_to_text
from app.tools.url_source import classify_url_source

INPUT_TOOL_NAMES = ("analyze_image", "transcribe_audio", "fetch_url_content", "reinspect_image_identity")
BUNDLE_INPUT_TOOL_NAMES = ("analyze_image_bundle",)  # multi-image（2–3 張同一產品）；單張圖片仍用 analyze_image

IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_URL_BYTES = 2 * 1024 * 1024
MAX_URL_TEXT_CHARS = 6000
MAX_REDIRECTS = 3
URL_TIMEOUT_SECONDS = 15.0  # 單次連線 / 讀取上限
URL_TOTAL_DEADLINE_SECONDS = 20.0  # 整體上限（含重新導向、慢速傳輸），超過即 TIMEOUT，不會卡住
DNS_TIMEOUT_SECONDS = 5.0
URL_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml+xml")

NOTE_UNOFFICIAL_URL = "使用者提供的非官方網頁：內容只代表對方宣稱了什麼，不是官方證據"
NOTE_OFFICIAL_URL = "使用者提供的網址屬於台灣官方網域：頁面內容是官方來源的資料，不是廣告或流傳說法"
NOTE_UNKNOWN_URL = "使用者提供的網址無法確認來源角色：內容不可當成官方證據"
URL_ROLE_NOTES = {
    UrlSourceRole.OFFICIAL_REGULATORY_SOURCE: NOTE_OFFICIAL_URL,
    UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE: NOTE_UNOFFICIAL_URL,
    UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE: NOTE_UNKNOWN_URL,
}

IMAGE_ANALYSIS_INSTRUCTIONS = """\
你是「呷安心」的圖片讀取工具。只做「讀取與描述」，不要判斷真假、不要評論功效、不要提供建議。
請用繁體中文輸出：
1. 圖片中可辨識的文字（產品名稱、品牌、公司、許可證字號、成分、宣稱用語），盡量逐字照抄。
2. 圖片類型（產品包裝、廣告、截圖、文件等）與一句簡短描述。
3. 認證標章：描述標章的圖形與種類（例如健康食品標章、其他認證標章）。標章上的弧形或很小的文字看不清楚時，
   寫「標章文字無法辨識」，不要硬讀；標章文字不是產品名稱。
看不清楚的部分寫「無法辨識」，不可猜測或補上圖片中沒有的資訊。
"""

# 產品身份無法確認時的第二次局部重讀：只看許可證字號與產品名稱區域（不讀標章、宣稱、成分）
IMAGE_IDENTITY_REINSPECTION_INSTRUCTIONS = """你是「呷安心」的圖片局部重讀工具。這是第二次讀取，只需要重新看清楚兩個區域：
1. 許可證字號（例如「衛部健食字第A00000號」「衛署健食規字第000000號」），通常在包裝背面或側面的小字區；
2. 產品名稱（包裝正面最主要的品名文字，不含標章、口號、規格、容量）。
不要讀認證標章、功效宣稱、成分。逐字照抄，看不清楚就留空，不可猜測或補字。
只輸出一行 JSON：{"permit_number": "...", "product_name": "...", "permit_readable": true, "name_readable": true}
看不清楚的欄位給空字串，並把對應的 readable 設為 false。
"""


class ImageIdentityReading(BaseModel):
    """第二次局部重讀的結果（只有許可證字號與產品名稱）。"""

    permit_number: str = ""
    product_name: str = ""
    permit_readable: bool = False
    name_readable: bool = False


class InputToolOutput(BaseModel):
    tool_name: str
    text: str
    raw_text: str | None = None  # 語音：轉錄原文（text 是字形正規化後的繁體逐字稿；log / debug 用）
    source_url: str | None = None
    title: str | None = None
    notes: list[str] = Field(default_factory=list)


def analyze_image(client: MediaClient, image: bytes, mime_type: str) -> InputToolOutput:
    if not image:
        raise ServiceError(ErrorCode.INVALID_INPUT, "圖片內容是空的", "analyze_image")
    if mime_type not in IMAGE_MIME_TYPES:
        raise ServiceError(ErrorCode.INVALID_INPUT, f"不支援的圖片格式：{mime_type}", "analyze_image")
    if len(image) > MAX_IMAGE_BYTES:
        raise ServiceError(ErrorCode.INVALID_INPUT, "圖片太大", "analyze_image")
    text = client.describe_image(instructions=IMAGE_ANALYSIS_INSTRUCTIONS, image=image, mime_type=mime_type)
    return InputToolOutput(tool_name="analyze_image", text=text.strip(), notes=["內容由圖片讀取而來，可能有辨識誤差"])


IMAGE_BUNDLE_INSTRUCTIONS = """\
你是「呷安心」的多張圖片讀取工具。使用者一次傳來同一件保健食品的多張照片（Image 1、Image 2…，例如正面、背面成分、許可證 / 標示區）。
只做「逐張讀取」，不要判斷真假、不要評論功效、不要下產品結論，也不要假設這些圖片一定是同一個產品。
對每一張圖片各自回傳一筆觀察（image_index 必須等於圖片編號）：
1. product_names：產品名稱（包裝正面最主要的品名；只拍到一部分時 is_complete=false）；成分、宣稱、規格不是產品名稱。
2. brands / companies / permit_numbers（許可證字號，逐字照抄）。
3. ingredients：成分表上的每一個成分名稱，一項一筆；成分不是產品名稱。
4. claims：包裝上的功效 / 健康宣稱原句；warnings：警語與注意事項；certification_marks：標章種類（標章弧形小字看不清楚就不要讀）。
5. other_visible_text：容量、規格、口味、價格等其他文字。
逐字照抄；看不清楚的內容 readable=false、value 給空字串，不可猜測或補字，也不要用另一張圖片的內容補這一張。
每一筆都要給 confidence（0~1）。image_kind 選：front_package / back_label / permit_area / advertisement / other。
"""


class ImageBundleResult(BaseModel):
    tool_name: str = "analyze_image_bundle"
    observation: ImageBundleObservation
    image_count: int
    notes: list[str] = Field(default_factory=list)


def analyze_image_bundle(client: MediaClient, images: list[tuple[bytes, str]]) -> ImageBundleResult:
    """Multi-image perception：一次 Vision request 看完所有圖片（每張都標示 Image k），回傳逐張的結構化觀察。

    只做觀察，不做產品結論；圖片數量限制 2–3 張（超過直接拒絕，不默默丟掉其中幾張）。
    """
    if not (MIN_BUNDLE_IMAGES <= len(images) <= MAX_BUNDLE_IMAGES):
        raise ServiceError(ErrorCode.INVALID_INPUT, f"一次比對的圖片需為 {MIN_BUNDLE_IMAGES}–{MAX_BUNDLE_IMAGES} 張",
                           "analyze_image_bundle")
    for content, mime_type in images:
        if not content:
            raise ServiceError(ErrorCode.INVALID_INPUT, "圖片內容是空的", "analyze_image_bundle")
        if mime_type not in IMAGE_MIME_TYPES:
            raise ServiceError(ErrorCode.INVALID_INPUT, f"不支援的圖片格式：{mime_type}", "analyze_image_bundle")
        if len(content) > MAX_IMAGE_BYTES:
            raise ServiceError(ErrorCode.INVALID_INPUT, "圖片太大", "analyze_image_bundle")
    observation = client.describe_images_structured(
        instructions=IMAGE_BUNDLE_INSTRUCTIONS, images=images, output_model=ImageBundleObservation)
    return ImageBundleResult(observation=observation, image_count=len(images),
                             notes=["內容由多張圖片讀取而來，可能有辨識誤差"])


def reinspect_image_identity(client: MediaClient, image: bytes, mime_type: str) -> ImageIdentityReading:
    """產品身份無法確認時，最多一次的局部重讀（同一個 OpenAI 圖片理解，只看字號 / 品名區域）。"""
    if not image or mime_type not in IMAGE_MIME_TYPES or len(image) > MAX_IMAGE_BYTES:
        raise ServiceError(ErrorCode.INVALID_INPUT, "圖片無法重新讀取", "reinspect_image_identity")
    text = client.describe_image(instructions=IMAGE_IDENTITY_REINSPECTION_INSTRUCTIONS, image=image, mime_type=mime_type)
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        reading = ImageIdentityReading.model_validate_json(match.group(0)) if match else None
    except ValueError:
        reading = None
    if reading is None:
        raise ServiceError(ErrorCode.UPSTREAM_ERROR, "圖片重讀結果格式不正確", "reinspect_image_identity")
    return reading.model_copy(update={
        "permit_number": reading.permit_number.strip() if reading.permit_readable else "",
        "product_name": reading.product_name.strip() if reading.name_readable else "",
    })


def transcribe_audio(client: MediaClient, audio: bytes, filename: str = "audio.m4a",
                     prompt: str | None = None) -> InputToolOutput:
    if not audio:
        raise ServiceError(ErrorCode.INVALID_INPUT, "語音內容是空的", "transcribe_audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ServiceError(ErrorCode.INVALID_INPUT, "語音檔太大", "transcribe_audio")
    # prompt = 領域詞彙提示（動態由 Interaction Corpus 建立；沒有時不傳，行為與原本相同）
    raw = (client.transcribe_audio(audio=audio, filename=filename, prompt=prompt) if prompt
           else client.transcribe_audio(audio=audio, filename=filename)).strip()
    transcript = normalize_transcript(raw)  # 簡體字形 → 繁體字形（只處理字形；已是繁體則 no-op）
    notes = ["內容由語音轉文字而來，可能有同音錯字"]
    if transcript.changed:
        notes.append("逐字稿含簡體字，已轉為繁體字形（只處理字形，不改詞意）")
    return InputToolOutput(tool_name="transcribe_audio", text=transcript.normalized, raw_text=raw, notes=notes)


_dns_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="url-dns")


def _resolve_host(host: str) -> list[str]:
    """getaddrinfo 本身沒有 timeout：放到背景 thread 並限時，避免 DNS 卡住整個 request。"""
    future = _dns_executor.submit(socket.getaddrinfo, host, None)
    try:
        infos = future.result(timeout=DNS_TIMEOUT_SECONDS)
    except FutureTimeout as exc:
        raise OSError("DNS lookup timeout") from exc
    return [info[4][0] for info in infos]


def _check_public_url(url: str, resolve: Callable[[str], list[str]]) -> None:
    """只允許 http(s) 且解析到公開 IP 的網址，避免被用來存取內部網路（SSRF）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ServiceError(ErrorCode.INVALID_INPUT, "只接受 http / https 網址", "fetch_url_content", REASON_INVALID_URL)
    try:
        addresses = resolve(parsed.hostname)
    except OSError as exc:
        raise ServiceError(ErrorCode.UNAVAILABLE, "無法解析網址主機", "fetch_url_content", REASON_INVALID_URL) from exc
    if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise ServiceError(ErrorCode.INVALID_INPUT, "不允許存取非公開網路位址", "fetch_url_content", REASON_INVALID_URL)


def _reason_for_status(status: int) -> str:
    """HTTP 狀態 → 失敗類別：401 / 403 / 429 等是存取限制；404 / 410 是網址有誤；其餘（5xx 等）當暫時連不上。"""
    if status in (401, 402, 403, 407, 429, 451):
        return REASON_ACCESS_RESTRICTED
    if status in (400, 404, 410):
        return REASON_INVALID_URL
    return REASON_NETWORK


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ServiceError(ErrorCode.TIMEOUT, "讀取網頁逾時", "fetch_url_content", REASON_NETWORK)


def fetch_url_content(
    url: str,
    *,
    client: httpx.Client | None = None,
    resolve: Callable[[str], list[str]] = _resolve_host,
) -> InputToolOutput:
    url = (url or "").strip()
    own_client = client is None
    client = client or httpx.Client(timeout=URL_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT})
    deadline = time.monotonic() + URL_TOTAL_DEADLINE_SECONDS
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):  # 每一次重新導向都重新檢查主機
            _check_deadline(deadline)
            _check_public_url(current, resolve)
            with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                if resp.status_code >= 400:
                    raise ServiceError(ErrorCode.UPSTREAM_ERROR, f"網頁回傳 {resp.status_code}", "fetch_url_content",
                                       _reason_for_status(resp.status_code))
                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type not in URL_CONTENT_TYPES:
                    raise ServiceError(ErrorCode.INVALID_INPUT, f"不支援的網頁內容類型：{content_type}", "fetch_url_content",
                                       REASON_UNSUPPORTED)
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_URL_BYTES:
                        break
                    _check_deadline(deadline)
                html = body[:MAX_URL_BYTES].decode(resp.encoding or "utf-8", errors="replace")
            break
        else:
            raise ServiceError(ErrorCode.INVALID_INPUT, "重新導向次數過多", "fetch_url_content", REASON_ACCESS_RESTRICTED)
    except httpx.TimeoutException as exc:
        raise ServiceError(ErrorCode.TIMEOUT, "讀取網頁逾時", "fetch_url_content", REASON_NETWORK) from exc
    except httpx.HTTPError as exc:
        raise ServiceError(ErrorCode.UNAVAILABLE, f"無法讀取網頁：{type(exc).__name__}", "fetch_url_content",
                           REASON_NETWORK) from exc
    finally:
        if own_client:
            client.close()

    title, text = html_to_text(html) if content_type != "text/plain" else ("", html)
    if not text.strip():
        raise ServiceError(ErrorCode.INVALID_INPUT, "網頁沒有可讀取的文字內容", "fetch_url_content", REASON_UNSUPPORTED)
    content = (f"{title}\n{text}" if title else text)[:MAX_URL_TEXT_CHARS]
    # 來源角色以重新導向後實際讀取的網址判斷（官方網址導向非官方頁 → 非官方）
    note = URL_ROLE_NOTES[classify_url_source(current)]
    return InputToolOutput(tool_name="fetch_url_content", text=content, source_url=current,
                           title=title or None, notes=[note])
