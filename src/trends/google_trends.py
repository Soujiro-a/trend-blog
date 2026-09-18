"""구글 트렌드 급상승 검색어 (대한민국).

RSS 에 관련 뉴스 기사까지 들어 있어서 키워드 수집 + 1차 리서치를 동시에 해결합니다.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .. import net
from .base import NewsRef, TrendItem

URL = "https://trends.google.com/trending/rss?geo=KR"
NS = {"ht": "https://trends.google.com/trending/rss"}

SOURCE = "google_trends"


def fetch(top_n: int = 20) -> list[TrendItem]:
    resp = net.get(URL)
    root = ET.fromstring(resp.content)

    items: list[TrendItem] = []
    for rank, item in enumerate(root.iterfind(".//item"), start=1):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue

        news: list[NewsRef] = []
        for n in item.findall("ht:news_item", NS):
            news.append(
                NewsRef(
                    title=(n.findtext("ht:news_item_title", namespaces=NS) or "").strip(),
                    url=(n.findtext("ht:news_item_url", namespaces=NS) or "").strip(),
                    source=(n.findtext("ht:news_item_source", namespaces=NS) or "").strip(),
                    snippet=(n.findtext("ht:news_item_snippet", namespaces=NS) or "").strip(),
                )
            )

        items.append(
            TrendItem(
                keyword=title,
                source=SOURCE,
                rank=rank,
                traffic=(item.findtext("ht:approx_traffic", namespaces=NS) or "").strip(),
                news=news,
            )
        )
        if len(items) >= top_n:
            break

    return items
