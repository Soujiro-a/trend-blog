"""Claude 호출 공통 설정.

두 가지 사고에서 나온 규칙을 한곳에 모았습니다.

1. **재시도를 넉넉히.** 2026-09-22 에 `overloaded_error` 로 글 한 건이 날아갔습니다.
   SDK 기본 재시도(2회)로는 부족해 6회로 늘립니다. 재시도는 지수 백오프라 몰아치지 않습니다.

2. **응답이 max_tokens 에서 잘렸는지 반드시 확인.** 2026-09-23 에 글감 기획 응답(JSON 배열)이
   중간에 끊겨 그날 슬롯 하나가 통째로 날아갔습니다. Sonnet 5 는 thinking 을 따로 끄지 않으면
   적응형 thinking 이 켜지는데, thinking 토큰도 max_tokens 안에서 쓰입니다. 2000 토큰으로 잡아둔
   호출은 생각하는 데 대부분을 쓰고 본문이 잘릴 수 있습니다.
"""

from __future__ import annotations

import logging

import anthropic

log = logging.getLogger(__name__)

MAX_RETRIES = 6


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=MAX_RETRIES)


class Truncated(Exception):
    """응답이 max_tokens 에서 잘렸습니다."""


def text_of(response, what: str, *, allow_truncated: bool = False) -> str:
    """응답의 텍스트를 꺼냅니다. 잘렸으면 로그를 남기고, allow_truncated 가 아니면 예외.

    allow_truncated=True 는 잘린 응답에서도 쓸 만한 부분을 건질 수 있는 호출용입니다
    (예: 글감 목록 — 8개 중 5개만 와도 오늘 쓸 1개는 충분합니다).
    """
    raw = "".join(b.text for b in response.content if b.type == "text")
    if response.stop_reason == "max_tokens":
        usage = getattr(response, "usage", None)
        detail = f"출력 {usage.output_tokens} 토큰" if usage else ""
        if allow_truncated:
            log.warning("%s 응답이 max_tokens 에서 잘렸습니다 (%s). 완성된 부분만 씁니다.", what, detail)
        else:
            raise Truncated(f"{what} 응답이 max_tokens 에서 잘렸습니다 ({detail}). max_tokens 를 늘리세요.")
    return raw
