"""장수(evergreen) 주제 생성.

실시간 이슈 글은 검색 유입 수명이 1~3일입니다. 광고 수익이 매일 0에서 다시 시작하는
구조라, 한 달 뒤에도 검색되는 해설·안내 글을 주 1회 따로 씁니다.

주제는 최근 이력(어떤 이슈가 떴는지)을 재료로 모델이 뽑습니다.
예) '비트코인 시세' 이슈 → '비트코인 현물 ETF 란 무엇인가, 구조와 장단점'
    '레버리지 ETF 발언'  → '레버리지 ETF 수수료·괴리율 이해하기'
"""

from __future__ import annotations

import json
import logging
import re

import anthropic

from .trends import Candidate, Variant

log = logging.getLogger(__name__)

SYSTEM = """당신은 한국어 검색 트래픽을 잘 아는 블로그 기획자입니다. 최근 화제가 된 \
이슈 목록을 보고, 그 배경에 있는 **오래 검색될 해설 주제**를 고릅니다.

좋은 주제의 조건:
- 이슈가 식은 뒤에도 사람들이 계속 찾아볼 개념·제도·방법 (용어 설명, 신청 방법, 비교, 계산법)
- 특정 개인·사건이 아닌 일반 주제
- 한국 독자가 실제로 검색창에 칠 만한 표현 (4~8 단어)
- 정치적 논쟁, 의료 진단, 투자 권유는 피함

출력은 아래 JSON 배열만. 설명이나 코드 펜스는 붙이지 마세요.
[{"topic": "글 주제 (검색어 형태)", "why": "왜 오래 검색될지 한 줄", "search": "뉴스 검색용 짧은 키워드 2~3 단어"}]"""

USER_TEMPLATE = """## 최근 {days}일 동안 다룬 이슈

{recent}

## 이미 쓴 장수 주제 (피할 것)

{done}

위 재료를 바탕으로 장수 주제 {n}개를 고르세요."""


def _parse(raw: str) -> list[dict]:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        raise ValueError(f"주제 응답에 JSON 배열이 없습니다: {raw[:200]!r}")
    items = json.loads(m.group(0))
    out = []
    for it in items:
        topic = str(it.get("topic", "")).strip()
        if not topic:
            continue
        out.append(
            {
                "topic": topic,
                "why": str(it.get("why", "")).strip(),
                "search": str(it.get("search") or topic).strip(),
            }
        )
    return out


def propose(
    cfg: dict,
    history: list[dict],
    client: anthropic.Anthropic | None = None,
) -> list[Candidate]:
    """이력을 재료로 장수 주제 후보를 만듭니다. Candidate 형태로 돌려줘 기존 파이프라인을 그대로 탑니다."""
    ev = cfg["evergreen"]
    client = client or anthropic.Anthropic()

    recent = [h for h in history if h.get("mode", "trend") != "evergreen"][-40:]
    done = [h for h in history if h.get("mode") == "evergreen"][-30:]

    recent_lines = "\n".join(f"- {h.get('keyword')} — {h.get('title', '')}" for h in recent) or "- (없음)"
    done_lines = "\n".join(f"- {h.get('keyword')}" for h in done) or "- (없음)"

    response = client.messages.create(
        model=ev["topic_model"],
        max_tokens=1500,
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    days=ev.get("lookback_days", 30),
                    recent=recent_lines,
                    done=done_lines,
                    n=ev.get("candidates", 6),
                ),
            }
        ],
    )
    raw = "".join(b.text for b in response.content if b.type == "text")
    topics = _parse(raw)
    log.info("장수 주제 후보 %d개 (토큰 %d/%d)", len(topics), response.usage.input_tokens, response.usage.output_tokens)

    candidates: list[Candidate] = []
    for rank, t in enumerate(topics, start=1):
        c = Candidate(
            keyword=t["topic"],
            seen=[Variant(t["topic"], "evergreen", rank), Variant(t["search"], "evergreen", rank)],
            sources={"evergreen": rank},
            score=round(1.0 - rank * 0.05, 4),
        )
        c.headline_hits = [t["why"]] if t["why"] else []
        candidates.append(c)
    return candidates
