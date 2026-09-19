"""수익화 모듈.

애드센스는 Blogger 가 템플릿에 광고를 알아서 넣어 주므로 코드가 필요 없습니다.
여기서는 글 본문에 직접 넣어야 하는 것(제휴 링크)을 다룹니다.
"""

from __future__ import annotations

import logging

from ..reviewer import Review
from ..writer import Article
from . import coupang

log = logging.getLogger(__name__)


def apply(cfg: dict, article: Article, review: Review) -> list[str]:
    """설정과 검수 결과에 따라 본문을 수정합니다. 적용한 항목 이름 목록을 돌려줍니다."""
    applied: list[str] = []

    cp = cfg.get("monetize", {}).get("coupang", {})
    if cp.get("enabled") and review.commercial_intent and review.product_query:
        if coupang.configured():
            try:
                block = coupang.product_block(cfg, review.product_query)
            except Exception as exc:  # 제휴 실패는 글 발행을 막을 이유가 못 됩니다
                log.warning("[%s] 쿠팡 상품 조회 실패: %s", article.keyword, exc)
                block = ""
            if block:
                article.body_html = coupang.insert(article.body_html, block)
                applied.append("coupang")
        else:
            log.debug("쿠팡파트너스 키가 없어 건너뜁니다.")

    return applied
