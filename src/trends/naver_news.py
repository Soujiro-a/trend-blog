"""네이버 뉴스 '많이 본 뉴스' 랭킹.

헤드라인 소스입니다. 여기서 나온 문장은 그 자체로 키워드가 되지 않고,
다른 소스에서 올라온 키워드가 실제 뉴스로 뒷받침되는지 확인하는 데 씁니다.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from .. import net
from .base import TrendItem

URL = "https://news.naver.com/main/ranking/popularDay.naver"
SOURCE = "naver_news"


def fetch(top_n: int = 20) -> list[TrendItem]:
    resp = net.get(URL, headers={"Referer": "https://news.naver.com/"})
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    items: list[TrendItem] = []
    seen: set[str] = set()
    for anchor in soup.select("a.list_title"):
        title = anchor.get_text(strip=True)
        if not title or title in seen:
            continue
        seen.add(title)
        items.append(
            TrendItem(
                keyword=title,
                source=SOURCE,
                rank=len(items) + 1,
            )
        )
        if len(items) >= top_n:
            break
    return items
