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


def apply(cfg: dict, candidates: list[Candidate]) -> tuple[list[Candidate], list[Rejection]]:
    f = cfg["filters"]
    patterns = f.get("sensitive_patterns", []) if f.get("block_sensitive", True) else []
    blocked = f.get("block_keywords", [])
    min_len = f.get("min_keyword_length", 2)
    max_len = f.get("max_keyword_length", 30)

    kept: list[Candidate] = []
    rejected: list[Rejection] = []

    for c in candidates:
        if not (min_len <= len(c.keyword) <= max_len):
            rejected.append(Rejection(c.keyword, f"길이 {len(c.keyword)}자"))
            continue

        if any(b in c.keyword for b in blocked):
            rejected.append(Rejection(c.keyword, "제외 키워드"))
            continue

        # 키워드 자체, 다른 소스에서의 표현, 그리고 직접 연결된 기사 제목까지 검사
        haystack = " ".join([c.keyword, *c.variants, *(n.title for n in c.news)])
        hit = _hits_sensitive(haystack, patterns)
        if hit:
            rejected.append(Rejection(c.keyword, f"민감 주제('{hit}')"))
            continue

        kept.append(c)

    return kept, rejected
