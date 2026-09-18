"""로컬 HTML 저장. API 키 없이 파이프라인을 시험할 때 씁니다."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from ..config import OUT_DIR
from ..writer import Article

log = logging.getLogger(__name__)

PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<style>
  body {{ max-width: 720px; margin: 2rem auto; padding: 0 1rem;
         font-family: system-ui, -apple-system, "Malgun Gothic", sans-serif;
         line-height: 1.75; color: #1a1a1a; }}
  h1 {{ font-size: 1.6rem; line-height: 1.35; }}
  h2 {{ margin-top: 2.2rem; font-size: 1.25rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: .5rem .6rem; text-align: left; }}
  .meta {{ color: #666; font-size: .875rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta">키워드: {keyword} · 태그: {labels}</p>
{body}
</body>
</html>
"""


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣]+", "-", text).strip("-")
    return cleaned[:60] or "post"


def publish(cfg: dict, article: Article) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT_DIR / f"{stamp}-{_slug(article.keyword)}.html"

    path.write_text(
        PAGE.format(
            title=article.title,
            description=article.description,
            keyword=article.keyword,
            labels=", ".join(article.labels),
            body=article.body_html,
        ),
        encoding="utf-8",
    )
    log.info("로컬 저장: %s", path)
    return {"id": "", "url": str(path), "title": article.title}
