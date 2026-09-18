"""후보 키워드 걸러내기.

실시간 검색어에는 개인 사건사고·범죄·사망·사생활 관련 키워드가 늘 섞여 있습니다.
이런 주제로 AI가 글을 쓰면 (1) 사실 확인이 안 된 내용으로 명예훼손 위험이 있고
(2) 애드센스 '충격적인 콘텐츠' 정책에 걸리기 쉽습니다. 기본으로 차단합니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .trends import Candidate

log = logging.getLogger(__name__)


@dataclass
class Rejection:
    keyword: str
    reason: str


def _hits_sensitive(text: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if p in text:
            return p
    return None


def _keyword_problem(keyword: str, f: dict) -> str | None:
    """이 표현을 글 주제로 쓸 수 없는 이유. 쓸 수 있으면 None."""
    min_len = f.get("min_keyword_length", 2)
    max_len = f.get("max_keyword_length", 30)
    min_standalone = f.get("min_standalone_length", 3)

    if not (min_len <= len(keyword) <= max_len):
        return f"길이 {len(keyword)}자"

    # '축구', '수학' 처럼 띄어쓰기 없는 짧은 한 단어는 주제가 너무 넓습니다.
    # 검색 유입도 약하고 글도 얄팍해져서 거릅니다.
    if len(keyword.split()) == 1 and len(keyword) < min_standalone:
        return f"단독 {len(keyword)}글자 (주제가 너무 포괄적)"

    if any(b in keyword for b in f.get("block_keywords", [])):
        return "제외 키워드"

    return None


def apply(cfg: dict, candidates: list[Candidate]) -> tuple[list[Candidate], list[Rejection]]:
    f = cfg["filters"]
    patterns = f.get("sensitive_patterns", []) if f.get("block_sensitive", True) else []

    kept: list[Candidate] = []
    rejected: list[Rejection] = []

    for c in candidates:
        # 민감 주제는 표현을 바꿔도 주제 자체가 문제이므로 먼저 통째로 거릅니다.
        haystack = " ".join([c.keyword, *c.variants, *(n.title for n in c.news)])
        hit = _hits_sensitive(haystack, patterns)
        if hit:
            rejected.append(Rejection(c.keyword, f"민감 주제('{hit}')"))
            continue

        # 대표 표현이 부적합하면 버리기 전에, 같은 이슈를 더 구체적으로 표현한
        # 다른 소스의 키워드로 바꿔봅니다. ('축구' → '축구 국가대표 명단')
        problem = _keyword_problem(c.keyword, f)
        if problem:
            alternatives = sorted(
                (v for v in c.variants if v != c.keyword), key=len, reverse=True
            )
            replacement = next(
                (v for v in alternatives if _keyword_problem(v, f) is None), None
            )
            if replacement is None:
                rejected.append(Rejection(c.keyword, problem))
                continue
            log.info("키워드 교체: '%s' → '%s' (%s)", c.keyword, replacement, problem)
            c.keyword = replacement

        kept.append(c)

    return kept, rejected
