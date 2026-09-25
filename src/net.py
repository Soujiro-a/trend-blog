"""공용 HTTP 세션. 재시도와 브라우저 User-Agent 를 기본 적용합니다."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is not None:
        return _session

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"})
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    _session = s
    return s


_once: requests.Session | None = None


def once() -> requests.Session:
    """재시도하지 않는 세션. 글 발행처럼 **두 번 실행되면 안 되는** 요청에 씁니다.

    Blogger 가 글을 만든 뒤 5xx 를 돌려주면 자동 재시도가 같은 글을 한 번 더 올립니다.
    중복 게시는 그 자체로 스팸 신호이고, 429(한도)에 곧바로 다시 두드리는 것도 좋을 게 없습니다.
    """
    global _once
    if _once is None:
        _once = requests.Session()
        _once.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"})
    return _once


def get(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 20)
    resp = session().get(url, **kwargs)
    resp.raise_for_status()
    return resp
