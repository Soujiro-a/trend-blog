"""구글 뉴스 한국판 헤드라인.

네이버 뉴스와 마찬가지로 헤드라인(보강) 소스입니다.
키워드별 기사 검색(`search`)에도 같은 피드를 씁니다.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET

from .. import net
from .base import NewsRef, TrendItem

HEADLINES_URL = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
SEARCH_URL = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
SOURCE = "google_news"


def _parse_items(xml_bytes: bytes) -> list[tuple[str, str, str, str]]:
    """(title, link, source, pubDate) 목록을 돌려줍니다."""
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iterfind(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        out.append(
            (
                title,
                (item.findtext("link") or "").strip(),
                (item.findtext("source") or "").strip(),
                (item.findtext("pubDate") or "").strip(),
            )
        )
    return out


def fetch(top_n: int = 20) -> list[TrendItem]:
    resp = net.get(HEADLINES_URL)
    items: list[TrendItem] = []
    for title, _link, _source, _pub in _parse_items(resp.content)[:top_n]:
        items.append(TrendItem(keyword=title, source=SOURCE, rank=len(items) + 1))
    return items


def search(keyword: str, limit: int = 8) -> list[NewsRef]:
    """키워드로 최근 기사를 검색합니다. 글 작성용 리서치 자료."""
    url = SEARCH_URL.format(q=urllib.parse.quote(keyword))
    try:
        resp = net.get(url)
    except Exception:
        return []

    refs: list[NewsRef] = []
    for title, link, source, pub in _parse_items(resp.content)[:limit]:
        # 구글 뉴스 제목은 "기사 제목 - 언론사" 형태입니다.
        clean_title = title
        if source and title.endswith(f" - {source}"):
            clean_title = title[: -len(f" - {source}")]
        refs.append(NewsRef(title=clean_title, url=link, source=source, published=pub))
    return refs
