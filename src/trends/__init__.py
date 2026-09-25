"""여러 실시간 검색어 소스를 모아 하나의 순위로 합칩니다.

소스는 두 종류입니다.

* 키워드 소스 (google_trends / signal_bz / nate)
  - 실제로 사람들이 검색한 말이므로 후보 키워드가 됩니다.
* 헤드라인 소스 (naver_news / google_news)
  - 문장이라 그 자체로는 검색어가 아닙니다. 후보 키워드가 진짜 뉴스로
    뒷받침되는지 확인해 점수만 올려줍니다. 검색어만 뜨고 기사가 없는
    (= 쓸 내용이 없는) 키워드를 걸러내는 장치입니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import google_news, google_trends, naver_news, nate, signal_bz
from .base import NewsRef, TrendItem, normalize, similarity, tokenize

log = logging.getLogger(__name__)

KEYWORD_SOURCES = {
    "google_trends": google_trends,
    "signal_bz": signal_bz,
    "nate": nate,
}
HEADLINE_SOURCES = {
    "naver_news": naver_news,
    "google_news": google_news,
}

# 헤드라인이 키워드를 뒷받침한다고 볼 최소 토큰 포함율
_HEADLINE_CONTAINMENT = 0.5


@dataclass
class Variant:
    """한 소스가 표현한 키워드 형태."""

    keyword: str
    source: str
    rank: int


@dataclass
class Candidate:
    """여러 소스에서 합쳐진 하나의 이슈."""

    keyword: str
    seen: list[Variant] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)  # source -> rank
    score: float = 0.0
    headline_hits: list[str] = field(default_factory=list)
    news: list[NewsRef] = field(default_factory=list)
    # 기획 단계의 메모(이 글감을 왜 골랐는지). **글쓰기 자료로 넘기지 않습니다.**
    # 이걸 리서치 컨텍스트에 넣었더니 모델이 "왜 지금 검색되는가" 섹션을 만들어 버렸습니다.
    # 보고서·로그 표시용입니다.
    note: str = ""
    # 기획 모드에서 이 글감이 속한 하위 축(blogs.yaml 의 pillars 중 하나). 글의 분류 라벨이 됩니다.
    pillar: str = ""

    @property
    def variants(self) -> list[str]:
        return list(dict.fromkeys(v.keyword for v in self.seen))

    @property
    def normalized(self) -> str:
        return normalize(self.keyword)


def collect(cfg: dict) -> tuple[list[TrendItem], list[TrendItem]]:
    """설정된 모든 소스에서 수집합니다. 개별 소스 실패는 건너뜁니다."""
    weights = cfg["trends"]["sources"]
    top_n = cfg["trends"]["top_n_per_source"]

    keyword_items: list[TrendItem] = []
    headline_items: list[TrendItem] = []

    for name, module in {**KEYWORD_SOURCES, **HEADLINE_SOURCES}.items():
        if weights.get(name, 0) <= 0:
            continue
        try:
            items = module.fetch(top_n=top_n)
        except Exception as exc:  # 한 소스가 죽어도 전체는 계속
            log.warning("[%s] 수집 실패: %s", name, exc)
            continue

        log.info("[%s] %d개 수집", name, len(items))
        if name in KEYWORD_SOURCES:
            keyword_items.extend(items)
        else:
            headline_items.extend(items)

    return keyword_items, headline_items


def aggregate(
    cfg: dict,
    keyword_items: list[TrendItem],
    headline_items: list[TrendItem],
) -> list[Candidate]:
    """수집 결과를 점수순 후보 목록으로 합칩니다."""
    weights = cfg["trends"]["sources"]
    top_n = cfg["trends"]["top_n_per_source"]
    bonus = cfg["trends"]["multi_source_bonus"]
    sim_threshold = cfg["dedupe"]["similarity_threshold"]

    candidates: list[Candidate] = []

    # 헤드라인 토큰은 후보마다 다시 계산할 필요가 없어 한 번만 만들어 둡니다.
    headline_tokens = [(h, tokenize(h.keyword)) for h in headline_items]

    # 1) 비슷한 키워드끼리 묶기
    for item in sorted(keyword_items, key=lambda i: i.rank):
        match = next(
            (
                c
                for c in candidates
                if any(similarity(item.keyword, v) >= sim_threshold for v in c.variants)
            ),
            None,
        )
        if match is None:
            match = Candidate(keyword=item.keyword)
            candidates.append(match)

        match.seen.append(Variant(item.keyword, item.source, item.rank))
        # 같은 소스가 두 번 들어오면 더 높은(작은) 순위를 남깁니다.
        prev = match.sources.get(item.source)
        match.sources[item.source] = (
            min(prev, item.rank) if prev is not None else item.rank
        )
        match.news.extend(item.news)

    # 2) 점수 계산
    for c in candidates:
        base = 0.0
        for source, rank in c.sources.items():
            weight = weights.get(source, 0.0)
            base += weight * max(0.0, (top_n - rank + 1) / top_n)
        # 여러 소스에 동시에 뜬 이슈일수록 가산
        base *= 1.0 + bonus * (len(c.sources) - 1)

        # 3) 헤드라인 보강 — 실제 기사로 뒷받침되는 키워드에 가산점
        ktokens: set[str] = set()
        for v in [c.keyword, *c.variants]:
            ktokens |= tokenize(v)

        if ktokens:
            for h, htokens in headline_tokens:
                containment = len(ktokens & htokens) / len(ktokens)
                if containment >= _HEADLINE_CONTAINMENT:
                    base += weights.get(h.source, 0.0) * 0.5
                    if h.keyword not in c.headline_hits:
                        c.headline_hits.append(h.keyword)

        c.score = round(base, 4)

        # 대표 키워드는 "가장 신뢰도 높은 소스에서 가장 위에 뜬 표현"으로 고릅니다.
        # 짧은 표현을 고르면 '수능 수학 문제' 가 '수학' 이 되어 맥락이 날아가므로,
        # 점수가 같으면 더 구체적인(긴) 쪽을 씁니다.
        if c.seen:
            c.keyword = max(
                c.seen,
                key=lambda v: (
                    weights.get(v.source, 0.0) * max(0.0, (top_n - v.rank + 1) / top_n),
                    len(v.keyword),
                ),
            ).keyword

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
