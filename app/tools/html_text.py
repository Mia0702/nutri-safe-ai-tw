"""HTML → 正文純文字（RAG 匯入與 Agent A 的 fetch_url_content 共用）。"""

from bs4 import BeautifulSoup


def _main_block(soup: BeautifulSoup):
    """通用正文偵測：取文字量達整頁 80% 以上的最小區塊，去掉選單、側欄等外圍內容。"""
    root = soup.body or soup
    total = len(root.get_text(strip=True))
    if total == 0:
        return root
    best, best_len = root, total
    for el in root.find_all(["main", "article", "section", "div"]):
        length = len(el.get_text(strip=True))
        if length >= 0.8 * total and length < best_len:
            best, best_len = el, length
    return best


def html_to_text(html: str) -> tuple[str, str]:
    """回傳 (title, 正文)；正文以換行分段、去除空白行。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    lines = [ln.strip() for ln in _main_block(soup).get_text("\n").splitlines()]
    return title, "\n".join(ln for ln in lines if ln)
