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


def similarity(a: str, b: str) -> float:
    """두 키워드의 토큰 자카드 유사도 (0.0 ~ 1.0)."""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
