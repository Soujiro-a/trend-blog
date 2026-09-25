"""블로그별 주제 기획 — 실시간 검색어를 쫓지 않고, 블로그 고유 주제 안에서 글감을 만듭니다.

왜 바꿨나
---------
실시간 검색어 방식은 구조적으로 위험했습니다. 모든 블로그가 같은 날 같은 상위 키워드를
보기 때문에, 선점 규칙을 아무리 조여도 같은 사건이 여러 블로그에 실릴 여지가 남습니다
(2026-09-20: 최두호·UFC 사건이 3개 블로그에 동시 게재). 게다가 실시간 이슈는 수명이
1~3일이라 매일 새 글을 쏟아내야 트래픽이 유지되는 구조라, 발행량을 줄이기도 어렵습니다.

대신 블로그마다 **겹치지 않는 고유 주제(subject)와 하위 축(pillars)** 을 정하고,
그 안에서 검색 수요가 있을 만한 글감을 기획합니다. 블로그 간 주제가 설계 단계에서
분리되므로 중복이 발생할 수 없고, 글 수명이 길어 하루 2건으로도 누적이 쌓입니다.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic

from . import llm
from .trends import Candidate, Variant
from .trends.base import similarity

log = logging.getLogger(__name__)

SYSTEM = """당신은 한국어 블로그의 콘텐츠 기획자입니다. 주어진 블로그의 **고유 주제 안에서만** \
글감을 고릅니다. 다른 분야로 넘어가면 안 됩니다.

## 좋은 글감의 조건
- 블로그의 주제·하위 축에 정확히 들어맞습니다. 벗어나면 그 글감은 버리세요.
- 한국 독자가 **검색창에 실제로 칠 만한** 구체적인 표현입니다. ("연말정산 의료비 공제 조건" ○ / "경제 이야기" ×)
- 읽고 나면 **행동하거나 이해할 수 있는** 것이 남습니다. 방법, 기준, 절차, 비교, 계산, 용어 해설.
- 몇 달 뒤에 검색해도 유효합니다. 특정 인물의 오늘 사건, 경기 결과, 속보는 고르지 마세요.
- 이미 쓴 글감과 **주제가 겹치지 않습니다**. 표현만 바꾼 재탕은 안 됩니다.

## 피해야 할 것
- 특정 개인의 사생활·의혹·수사, 사건사고
- 의료 진단, 투자 권유, 법률 자문으로 읽힐 수 있는 단정
- "TOP 10", "총정리" 같은 속 빈 나열형 제목

## 출력
아래 JSON 배열만. 설명이나 코드 펜스는 붙이지 마세요.
[{"topic": "글 제목이 될 검색어형 주제", "pillar": "하위 축 목록에 적힌 이름을 글자 그대로 하나", "why": "왜 검색될지 한 줄", "search": "참고 기사 검색용 키워드 2~3 단어"}]
pillar 는 글 분류 라벨이 되므로 목록의 이름을 바꾸거나 줄이지 말고 그대로 복사하세요."""

USER_TEMPLATE = """## 이 블로그

- 주제: {subject}
- 하위 축: {pillars}
- 독자: {audience}

## 이미 쓴 글감 (겹치면 안 됨)

{done}

위 주제 안에서 글감 {n}개를 고르세요. 하위 축이 골고루 섞이게 하고, 이미 쓴 것과 겹치지 마세요."""


def _items(raw: str) -> list[dict]:
    """JSON 배열을 읽습니다. 배열이 중간에 잘렸으면 **완성된 항목만** 건집니다.

    2026-09-23 에 응답이 두 번째 항목 중간에서 끊겨 배열 전체를 버렸고, 그날 슬롯이 날아갔습니다.
    글감은 하나만 있어도 오늘 글을 쓸 수 있으므로, 온전한 `{...}` 만이라도 살립니다.
    """
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass
    # 잘린 배열: 중괄호가 짝이 맞는 객체만 하나씩 읽습니다. (글감 항목에는 중첩 객체가 없습니다)
    salvaged = []
    for chunk in re.findall(r"\{[^{}]*\}", raw):
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            salvaged.append(obj)
    if salvaged:
        log.warning("기획 응답이 온전한 배열이 아니라 완성된 항목 %d개만 건졌습니다.", len(salvaged))
        return salvaged
    raise ValueError(f"기획 응답에서 글감을 하나도 읽지 못했습니다: {raw[:200]!r}")


def _parse(raw: str) -> list[dict]:
    out = []
    for it in _items(raw):
        topic = str(it.get("topic", "")).strip()
        if not topic:
            continue
        out.append(
            {
                "topic": topic,
                "pillar": str(it.get("pillar", "")).strip(),
                "why": str(it.get("why", "")).strip(),
                "search": str(it.get("search") or topic).strip(),
            }
        )
    return out


def match_pillar(pillar: str, pillars: list[str]) -> str:
    """모델이 적은 하위 축 이름을 blogs.yaml 의 pillars 중 하나로 맞춥니다. 못 맞추면 빈 문자열.

    이 값이 글의 라벨이 됩니다. 모델이 매번 조금씩 다르게 적은 이름을 그대로 쓰면
    라벨이 글마다 새로 생겨 블로그 분류가 엉망이 됩니다(2026-09-25: 글 7개에 라벨 25개).
    """
    if not pillar or not pillars:
        return ""
    if pillar in pillars:
        return pillar
    squash = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    for p in pillars:
        if squash(p) == squash(pillar) or squash(pillar) in squash(p) or squash(p) in squash(pillar):
            return p
    best = max(pillars, key=lambda p: similarity(p, pillar))
    return best if similarity(best, pillar) >= 0.3 else ""


def propose(
    cfg: dict,
    blog,
    history: list[dict],
    client: anthropic.Anthropic | None = None,
) -> list[Candidate]:
    """블로그 주제 안에서 글감 후보를 만듭니다. 기존 파이프라인이 그대로 쓰도록 Candidate 로 돌려줍니다."""
    pl = cfg["planner"]
    client = client or llm.client()

    if not blog.subject:
        raise ValueError(
            f"블로그 '{blog.id}' 에 subject 가 없습니다. fleet/blogs.yaml 에서 고유 주제를 정하세요."
        )

    done = [h.get("keyword", "") for h in history][-60:]
    done_lines = "\n".join(f"- {k}" for k in done) or "- (없음)"

    # 글감 목록 뽑기는 깊이 생각할 일이 아닙니다. effort 를 낮춰 thinking 이 출력 자리를 먹지 않게 하고,
    # max_tokens 도 넉넉히 둡니다(thinking 토큰도 이 안에서 씁니다 — src/llm.py 참고).
    response = client.messages.create(
        model=pl["model"],
        max_tokens=int(pl.get("max_tokens", 8000)),
        output_config={"effort": pl.get("effort", "low")},
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    subject=blog.subject,
                    pillars=", ".join(blog.pillars) or "(지정 없음 — 주제에서 스스로 나누세요)",
                    audience=blog.audience or "해당 주제를 검색해서 들어오는 일반 독자",
                    done=done_lines,
                    n=pl.get("candidates", 8),
                ),
            }
        ],
    )
    raw = llm.text_of(response, f"[{blog.id}] 글감 기획", allow_truncated=True)
    topics = _parse(raw)
    log.info(
        "[%s] 글감 후보 %d개 (주제: %s, 토큰 %d/%d)",
        blog.id, len(topics), blog.subject,
        response.usage.input_tokens, response.usage.output_tokens,
    )

    candidates: list[Candidate] = []
    for rank, t in enumerate(topics, start=1):
        c = Candidate(
            keyword=t["topic"],
            seen=[Variant(t["topic"], "planner", rank), Variant(t["search"], "planner", rank)],
            sources={"planner": rank},
            score=round(1.0 - rank * 0.02, 4),
        )
        # why/pillar 는 글감 선정 근거일 뿐 글의 소재가 아닙니다.
        # 리서치 자료에 섞이면 "왜 지금 검색되는가" 같은 도입부 섹션으로 나옵니다.
        c.note = " · ".join(x for x in (t["pillar"], t["why"]) if x)
        c.pillar = match_pillar(t["pillar"], blog.pillars)
        candidates.append(c)
    return candidates
