"""실시간 검색어 수집기 공통 타입."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 한글/영문/숫자만 남기고 나머지는 공백으로
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]+")
# 의미 없는 조사/접미 토큰
_STOPWORDS = {
    "의", "가", "이", "은", "는", "을", "를", "에", "와", "과", "도", "로", "으로",
    "뉴스", "속보", "오늘", "현재", "관련", "및", "등", "첫", "전", "후",
}


@dataclass
class NewsRef:
    """키워드와 함께 수집된 참고 기사."""

    title: str
    url: str = ""
    source: str = ""
    published: str = ""
    snippet: str = ""

    @property
    def linkable(self) -> bool:
        """독자에게 링크로 걸어도 되는 주소인지.

        구글 뉴스 RSS 가 주는 주소는 실제 기사 URL 이 아니라 불투명한 토큰이라
        (서버에서 리다이렉트되지 않고 디코딩도 불가능) 링크로 걸면 안 됩니다.
        이런 기사는 매체명과 제목만 텍스트로 인용합니다.
        """
        return bool(self.url) and "news.google.com" not in self.url


@dataclass
class TrendItem:
    """한 소스에서 수집한 하나의 인기 키워드."""

    keyword: str
    source: str
    rank: int
    traffic: str = ""
    news: list[NewsRef] = field(default_factory=list)


def tokenize(keyword: str) -> set[str]:
    """키워드를 비교 가능한 토큰 집합으로 변환."""
    cleaned = _NON_WORD.sub(" ", keyword)
    tokens = {t for t in cleaned.split() if len(t) >= 2 and t not in _STOPWORDS}
    return tokens or {keyword.strip()}


def normalize(keyword: str) -> str:
    """중복 판정용 정규화 키. 토큰을 정렬해 붙입니다."""
    return " ".join(sorted(tokenize(keyword)))


# 토큰이 정확히 같지 않고 한쪽이 다른 쪽에 포함될 때 주는 점수.
# '사후강평' 과 '사후강평회의' 같은 한국어 합성어를 같은 말로 보기 위한 것입니다.
_PARTIAL_CREDIT = 0.7


def _best_match(token: str, others: set[str]) -> float:
    """한 토큰이 상대 토큰 집합과 얼마나 맞는지 (0.0 ~ 1.0)."""
    if token in others:
        return 1.0
    for other in others:
        if len(token) < 2 or len(other) < 2:
            continue
        shorter, longer = sorted((token, other), key=len)
        # 짧은 쪽이 긴 쪽에 통째로 들어있고, 길이 차이가 두 배 이내일 때만 인정.
        # ('한' 이 '한국' 에 들어간다고 같은 말로 보면 안 되므로)
        if shorter in longer and len(shorter) * 2 >= len(longer):
            return _PARTIAL_CREDIT
    return 0.0


def similarity(a: str, b: str) -> float:
    """두 키워드가 같은 이슈를 가리키는 정도 (0.0 ~ 1.0).

    단순 자카드 유사도를 쓰면 한국어에서 같은 사건이 갈라집니다.
    '을지연습 사후강평' 과 '2026년 을지연습 사후강평회의' 는 토큰이 정확히
    겹치는 게 '을지연습' 하나뿐이라 0.25 밖에 안 나옵니다. 그러면 같은 사건으로
    글을 두 번 쓰게 되므로, 부분 일치에도 점수를 줍니다.
    """
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0

    # 양쪽에서 각각 상대를 얼마나 설명하는지 재고 평균 냅니다(대칭성 확보).
    matched = sum(_best_match(t, tb) for t in ta) + sum(_best_match(t, ta) for t in tb)
    return matched / (len(ta) + len(tb))
