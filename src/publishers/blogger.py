"""Blogger(Blogspot) 임시저장 발행.

OAuth refresh token 으로 액세스 토큰을 받아 Blogger API v3 에 글을 올립니다.
`isDraft=true` 로만 올리므로 공개는 사용자가 직접 합니다.
"""

from __future__ import annotations

import hashlib
import logging
import time

from .. import net
from ..config import env
from ..writer import Article

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/blogger/v3"


# 한 번 실행에서 글을 여러 개 올리므로 토큰을 재사용합니다.
# **자격증명별로** 따로 캐시합니다: 함대는 한 실행 안에서 여러 구글 계정을 오가며
# (fleet.apply_env 가 BLOGGER_* 환경변수를 계정별 값으로 바꿉니다) 글을 올립니다.
# 캐시가 하나뿐이면 계정을 바꿔도 이전 계정의 토큰이 그대로 나와 엉뚱한 블로그에 씁니다.
# 키는 client_id + refresh_token 의 해시라, 환경변수가 바뀌면 자동으로 다른 캐시가 됩니다.
# {키: (만료 시각, 토큰)} — 만료 60초 전에 미리 갱신합니다.
_token_cache: dict[str, tuple[float, str]] = {}


class BloggerForbidden(RuntimeError):
    """쓰기 요청이 403 으로 거부됨. 2026-09-20 에 계정의 API 쓰기가 막혔을 때 이 응답이 왔습니다.

    일시적 오류가 아니므로 재시도하지 않고, 호출부는 그 계정 전체를 멈춰야 합니다(fleet.halt_account).
    """


def _credential_key() -> str:
    raw = f"{env('BLOGGER_CLIENT_ID', required=True)}:{env('BLOGGER_REFRESH_TOKEN', required=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _access_token() -> str:
    key = _credential_key()
    cached = _token_cache.get(key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]

    token, expires_in = _fetch_token()
    _token_cache[key] = (time.monotonic() + max(0, expires_in - 60), token)
    return token


def clear_token_cache() -> None:
    """토큰 캐시를 비웁니다. 자격증명을 교체한 뒤 확인용으로 다시 받을 때 씁니다."""
    _token_cache.clear()


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


def publish(cfg: dict, article: Article, live: bool | None = None) -> dict:
    """글을 올립니다. live=True 면 공개, False 면 임시저장. Blogger 의 post 객체를 반환.

    live 를 넘기지 않으면 config 의 publish.mode 를 따릅니다
    (auto → 공개, draft → 임시저장). 옛 설정 draft_only 도 인식합니다.
    """
    blog_id = env("BLOGGER_BLOG_ID", required=True)
    if live is None:
        pub = cfg["publish"]
        live = pub.get("mode", "draft") == "auto" and not pub.get("draft_only", False)
    draft_only = not live
    labels = list(dict.fromkeys([*cfg["publish"].get("default_labels", []), *article.labels]))

    body = {
        "kind": "blogger#post",
        "title": article.title,
        "content": article.body_html,
        "labels": labels,
    }

    # 재시도 없는 세션: 서버가 글을 만든 뒤 오류를 돌려줬을 때 같은 글이 두 번 올라가는 것을 막습니다.
    resp = net.once().post(
        f"{API_BASE}/blogs/{blog_id}/posts/",
        params={"isDraft": "true" if draft_only else "false"},
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=body,
        timeout=30,
    )
    if resp.status_code == 403:
        raise BloggerForbidden(f"Blogger 발행 거부 (403): {resp.text[:500]}")
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Blogger 발행 실패 ({resp.status_code}): {resp.text[:500]}")

    post = resp.json()
    log.info("%s 완료: %s (id=%s)", "공개" if live else "임시저장", post.get("title"), post.get("id"))
    return post


def published_since(start_iso: str, blog_id: str | None = None) -> int:
    """이 블로그가 start_iso 이후 **실제로 공개한** 글 수를 Blogger 에 직접 물어봅니다.

    로컬 이력과 무관한 진짜 기준입니다. 2026-09-20 사고에서 로컬 이력이 깨지자
    상한 계산이 통째로 풀려 하루 8건이 나갔습니다. 서버에 세면 로컬이 어떻게 망가져도
    같은 일이 반복되지 않습니다.
    """
    blog_id = blog_id or env("BLOGGER_BLOG_ID", required=True)
    total, page_token = 0, None
    while True:
        params = {
            "status": "live",
            "startDate": start_iso,
            "maxResults": 100,
            "fetchBodies": "false",
            "view": "ADMIN",
        }
        if page_token:
            params["pageToken"] = page_token
        resp = net.get(
            f"{API_BASE}/blogs/{blog_id}/posts",
            params=params,
            headers={"Authorization": f"Bearer {_access_token()}"},
        )
        payload = resp.json()
        total += len(payload.get("items", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return total


def list_posts(status: str = "live", max_results: int = 50) -> list[dict]:
    """관리자 시점으로 글 목록을 봅니다. 주간 보고서용. status: live | draft"""
    blog_id = env("BLOGGER_BLOG_ID", required=True)
    resp = net.get(
        f"{API_BASE}/blogs/{blog_id}/posts",
        params={
            "status": status,
            "maxResults": max_results,
            "fetchBodies": "false",
            "view": "ADMIN",
        },
        headers={"Authorization": f"Bearer {_access_token()}"},
    )
    return resp.json().get("items", [])


def find_blog_id(access_token: str, blog_url: str) -> str:
    """블로그 주소로 blogId 를 찾습니다. 최초 설정용."""
    resp = net.get(
        f"{API_BASE}/blogs/byurl",
        params={"url": blog_url},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return resp.json()["id"]
