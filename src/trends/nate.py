"""네이트 실시간 이슈 키워드.

EUC-KR 로 인코딩된 JS 배열 파일을 내려줍니다:
    [["1", "키워드", "n", "0", "표시용 키워드"], ...]
"""

from __future__ import annotations

import json
import re

from .. import net
from .base import TrendItem

URL = "https://www.nate.com/js/data/jsonLiveKeywordDataV1.js"
SOURCE = "nate"

_ARRAY = re.compile(r"\[\s*\[.*\]\s*\]", re.DOTALL)


def fetch(top_n: int = 20) -> list[TrendItem]:
    resp = net.get(URL, headers={"Referer": "https://www.nate.com/"})
    text = resp.content.decode("euc-kr", errors="replace")

    match = _ARRAY.search(text)
    if not match:
        return []

    rows = json.loads(match.group(0))

    items: list[TrendItem] = []
    for row in rows[:top_n]:
        # row = [순위, 키워드, 변동상태, 변동폭, 표시용 키워드]
        if len(row) < 2:
            continue
        keyword = str(row[1]).strip()
        if not keyword or "�" in keyword:
            continue
        try:
            rank = int(str(row[0]).strip())
        except ValueError:
            rank = len(items) + 1
        items.append(TrendItem(keyword=keyword, source=SOURCE, rank=rank))
    return items
