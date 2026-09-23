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

from .trends import Candidate, Variant

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
[{"topic": "글 제목이 될 검색어형 주제", "pillar": "어느 하위 축에 속하는지", "why": "왜 검색될지 한 줄", "search": "참고 기사 검색용 키워드 2~3 단어"}]"""

USER_TEMPLATE = """## 이 블로그

- 주제: {subject}
- 하위 축: {pillars}
- 독자: {audience}

## 이미 쓴 글감 (겹치면 안 됨)

{done}

위 주제 안에서 글감 {n}개를 고르세요. 하위 축이 골고루 섞이게 하고, 이미 쓴 것과 겹치지 마세요."""


def _parse(raw: str) -> list[dict]:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        raise ValueError(f"기획 응답에 JSON 배열이 없습니다: {raw[:200]!r}")
    out = []
    for it in json.loads(m.group(0)):
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


def propose(
    cfg: dict,
    blog,
    history: list[dict],
    client: anthropic.Anthropic | None = None,
) -> list[Candidate]:
    """블로그 주제 안에서 글감 후보를 만듭니다. 기존 파이프라인이 그대로 쓰도록 Candidate 로 돌려줍니다."""
    pl = cfg["planner"]
    client = client or anthropic.Anthropic()

    if not blog.subject:
        raise ValueError(
            f"블로그 '{blog.id}' 에 subject 가 없습니다. fleet/blogs.yaml 에서 고유 주제를 정하세요."
        )

    done = [h.get("keyword", "") for h in history][-60:]
    done_lines = "\n".join(f"- {k}" for k in done) or "- (없음)"

    response = client.messages.create(
        model=pl["model"],
        max_tokens=2000,
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
    raw = "".join(b.text for b in response.content if b.type == "text")
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
        candidates.append(c)
    return candidates
