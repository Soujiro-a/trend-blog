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

# 모델별 (입력, 출력) 가격, USD / 1M 토큰. 캐시 읽기는 입력의 0.1배, 캐시 쓰기는 1.25배(5분 캐시).
PRICES = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_READ_RATE, CACHE_WRITE_RATE = 0.1, 1.25


def cached(text: str) -> list[dict]:
    """프롬프트 캐싱용 블록. 같은 지시문·자료를 되풀이해 보내는 호출(고쳐 쓰기, 같은 실행의 다른 글)에서
    캐시된 부분은 입력 가격의 1/10 입니다. 최소 길이(약 1024 토큰)보다 짧으면 API 가 그냥 무시합니다."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def plain(content) -> str:
    """cached() 로 감싼 블록(또는 그냥 문자열)을 글자로 되돌립니다. 지시서 출력·테스트용."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


def cost_of(usage, model: str, default=(5.00, 25.00)) -> float:
    """응답 usage 로 계산한 비용(USD). 캐시 읽기·쓰기 토큰을 따로 쳐서, 캐싱을 켠 뒤에도 실제 청구액과 맞습니다."""
    price_in, price_out = PRICES.get(model, default)
    plain = getattr(usage, "input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return (plain * price_in + read * price_in * CACHE_READ_RATE + write * price_in * CACHE_WRITE_RATE
            + out * price_out) / 1_000_000


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
