"""시그널(signal.bz) 실시간 검색어 TOP 10.

네이버 실시간 검색어 폐지 이후 가장 널리 쓰이는 통합 집계 API 입니다.
"""

from __future__ import annotations

from .. import net
from .base import TrendItem

URL = "https://api.signal.bz/news/realtime"
SOURCE = "signal_bz"


def fetch(top_n: int = 20) -> list[TrendItem]:
    resp = net.get(URL, headers={"Referer": "https://signal.bz/"})
    payload = resp.json()

    items: list[TrendItem] = []
    for entry in payload.get("top10", [])[:top_n]:
        keyword = (entry.get("keyword") or "").strip()
        if not keyword:
            continue
        items.append(
            TrendItem(
                keyword=keyword,
                source=SOURCE,
                rank=int(entry.get("rank") or len(items) + 1),
            )
        )
    return items
