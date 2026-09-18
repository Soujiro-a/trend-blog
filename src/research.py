"""키워드별 리서치 자료 수집.

기사 본문을 긁어오지 않고 제목·요약·출처만 모읍니다.
저작권 문제를 피하고, 글은 이 자료를 바탕으로 새로 쓰게 합니다.
"""

from __future__ import annotations

import logging
import urllib.parse

from .trends import Candidate
from .trends import google_news
from .trends.base import NewsRef

log = logging.getLogger(__name__)

# 검색 결과 페이지라 기사가 내려가도 링크가 깨지지 않습니다.
NAVER_NEWS_SEARCH = "https://search.naver.com/search.naver?where=news&query={q}"


def more_news_url(keyword: str) -> str:
    return NAVER_NEWS_SEARCH.format(q=urllib.parse.quote(keyword))


def gather(cfg: dict, candidate: Candidate) -> list[NewsRef]:
    """구글 트렌드가 준 기사 + 키워드 검색 결과를 합칩니다."""
    limit = cfg["research"]["articles_per_keyword"]

    refs: list[NewsRef] = list(candidate.news)
    for variant in [candidate.keyword, *candidate.variants]:
        if len(refs) >= limit * 2:
            break
        refs.extend(google_news.search(variant, limit=limit))

    # 제목 기준 중복 제거
    seen: set[str] = set()
    unique: list[NewsRef] = []
    for r in refs:
        key = r.title.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(r)

    return unique[:limit]


def to_context(cfg: dict, candidate: Candidate, refs: list[NewsRef]) -> str:
    """모델에 넘길 리서치 블록을 만듭니다."""
    max_chars = cfg["research"]["max_context_chars"]

    lines = [
        f"# 실시간 인기 키워드: {candidate.keyword}",
        "",
        f"- 다른 표현: {', '.join(candidate.variants) or candidate.keyword}",
        f"- 수집된 소스: {', '.join(f'{s}(#{r})' for s, r in candidate.sources.items())}",
        f"- 통합 점수: {candidate.score}",
    ]
    if candidate.headline_hits:
        lines.append("- 관련 주요 헤드라인:")
        lines.extend(f"  - {h}" for h in candidate.headline_hits[:5])

    lines += ["", "## 참고 기사", ""]
    for i, r in enumerate(refs, start=1):
        lines.append(f"{i}. {r.title}")
        if r.source:
            lines.append(f"   - 매체: {r.source}")
        if r.published:
            lines.append(f"   - 보도: {r.published}")
        if r.linkable:
            lines.append(f"   - 링크(걸어도 됨): {r.url}")
        else:
            lines.append("   - 링크 없음 → 텍스트로만 인용할 것")
        if r.snippet:
            lines.append(f"   - 요약: {r.snippet}")
        lines.append("")

    lines += [
        "## 독자용 '더 읽기' 링크 (항상 이 주소를 쓸 것)",
        "",
        more_news_url(candidate.keyword),
        "",
    ]

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(생략)"
    return text
