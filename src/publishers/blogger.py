"""Blogger(Blogspot) 임시저장 발행.

OAuth refresh token 으로 액세스 토큰을 받아 Blogger API v3 에 글을 올립니다.
`isDraft=true` 로만 올리므로 공개는 사용자가 직접 합니다.
"""

from __future__ import annotations

import logging
import time

from .. import net
from ..config import env
from ..writer import Article

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/blogger/v3"


# 한 번 실행에서 글을 여러 개 올리므로 토큰을 재사용합니다.
# (만료 시각, 토큰) — 만료 60초 전에 미리 갱신합니다.
_token_cache: tuple[float, str] | None = None


def _access_token() -> str:
    global _token_cache
    if _token_cache and time.monotonic() < _token_cache[0]:
        return _token_cache[1]

    token, expires_in = _fetch_token()
    _token_cache = (time.monotonic() + max(0, expires_in - 60), token)
    return token


def _fetch_token() -> tuple[str, int]:
    resp = net.session().post(
        TOKEN_URL,
        data={
            "client_id": env("BLOGGER_CLIENT_ID", required=True),
            "client_secret": env("BLOGGER_CLIENT_SECRET", required=True),
            "refresh_token": env("BLOGGER_REFRESH_TOKEN", required=True),
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"액세스 토큰 발급 실패 ({resp.status_code}): {resp.text[:300]}\n"
            "refresh token 이 만료됐을 수 있습니다. "
            "scripts/get_blogger_token.py 를 다시 실행하세요."
        )
    payload = resp.json()
    return payload["access_token"], int(payload.get("expires_in", 3600))


def publish(cfg: dict, article: Article) -> dict:
    """글을 임시저장합니다. Blogger 가 돌려준 post 객체를 반환."""
    blog_id = env("BLOGGER_BLOG_ID", required=True)
    draft_only = cfg["publish"].get("draft_only", True)
    labels = list(dict.fromkeys([*cfg["publish"].get("default_labels", []), *article.labels]))

    body = {
        "kind": "blogger#post",
        "title": article.title,
        "content": article.body_html,
        "labels": labels,
    }

    resp = net.session().post(
        f"{API_BASE}/blogs/{blog_id}/posts/",
        params={"isDraft": "true" if draft_only else "false"},
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=body,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Blogger 발행 실패 ({resp.status_code}): {resp.text[:500]}")

    post = resp.json()
    log.info("임시저장 완료: %s (id=%s)", post.get("title"), post.get("id"))
    return post


def find_blog_id(access_token: str, blog_url: str) -> str:
    """블로그 주소로 blogId 를 찾습니다. 최초 설정용."""
    resp = net.get(
        f"{API_BASE}/blogs/byurl",
        params={"url": blog_url},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return resp.json()["id"]
