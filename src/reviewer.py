"""작성된 글을 공개해도 되는지 심사하는 AI 검수관.

사람이 매일 초안을 읽고 '게시'를 누르던 일을 대신합니다.
작성 모델과 다른 세션에서, 참고 기사와 본문을 대조해 아래를 점검합니다.

* 사실 근거 — 참고 기사에 없는 주장, 지어낸 숫자·인용·링크
* 법적 위험 — 개인 사생활, 확인 안 된 의혹, 명예훼손 소지
* 광고 정책 — 애드센스가 막는 충격적·선정적 표현, 낚시 제목
* 품질 — 같은 말 반복으로 분량만 채운 글, 형식 깨짐

결과는 publish / hold / reject 셋 중 하나입니다.
  publish → 바로 공개
  hold    → 임시저장으로 남김 (사람이 시간 날 때 보면 됨)
  reject  → 올리지 않음 (이력에만 기록)

부가로 '상품 구매 의도가 있는 주제인지'도 판단해 제휴 링크 삽입 여부를 정합니다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from .writer import PRICES, Article

log = logging.getLogger(__name__)

SYSTEM = """당신은 한국 시사·생활정보 블로그의 발행 책임자입니다. AI 가 쓴 글이 \
그대로 공개돼도 되는지 최종 판단합니다. 공개된 글은 블로그 운영자의 법적 책임이 되고 \
구글 애드센스 심사 대상이 되므로, 아래 기준으로 엄격하게 봅니다.

## 판정 기준

**reject (올리지 않음)** — 하나라도 해당하면:
- 특정인의 사생활·건강·가족·연애, 수사 중인 사건, 확인되지 않은 의혹을 다룸
- 참고 기사에 없는 사실을 단정적으로 주장 (지어낸 숫자, 발언, 날짜, 인용)
- 참고 자료에 없는 링크 주소를 만들어 넣음
  (단, 자료의 `## 이 블로그의 다른 글` 목록에 있는 주소를 본문에 건 것은 정상입니다. \
그 목록에 없는 같은 블로그 주소를 지어냈을 때만 문제입니다.)
- 특정 개인·집단을 비하하거나 혐오·선정·폭력 묘사가 있음
- 투자·의료·법률에 대해 "사라/팔아라/복용하라" 식의 직접 권유

**hold (임시저장으로 보류)** — reject 는 아니지만:
- 근거가 약한 문장이 2개 이상 (추측, "~로 알려졌다" 식의 출처 불명 서술)
- 제목이 본문과 다르거나 과장됨
- 분량을 채우기 위한 뻔한 일반론·반복이 본문의 1/3 이상
- HTML 형식이 깨져 있음 (닫히지 않은 태그, 허용되지 않은 태그)
- 정치적으로 한쪽 편을 드는 서술
- "왜 지금 검색되는가", "요즘 화제인 이유" 처럼 **독자가 왜 검색했는지 설명하는 도입부 섹션**이 있음 \
(독자는 이미 알고 들어왔습니다. 답부터 나와야 합니다)
- 내부 링크가 문맥과 무관한 자리에 억지로 들어갔거나, `관련 글` 같은 별도 나열 섹션으로 붙어 있음

**publish (공개)** — 위에 걸리는 것이 없고, 검색해 들어온 독자의 궁금증을 \
실제로 해결해 주는 글.

## 부가 판단

- `commercial_intent`: 독자가 이 주제로 **물건을 살 가능성**이 있는가. \
(예: 신제품 출시, 특정 기기·식품·도서·게임 → true / 정치·사건·스포츠 경기 결과 → false)
- `product_query`: commercial_intent 가 true 면 쇼핑몰에서 검색할 짧은 상품명 \
(2~4 단어, 브랜드+품목). false 면 null.
- `evergreen`: 이 글이 한 달 뒤에도 검색될 만한 해설·안내 성격인가.

## 출력 형식

아래 JSON 하나만 출력합니다. 설명이나 마크다운 코드 펜스는 붙이지 마세요.

{"verdict": "publish" | "hold" | "reject",
 "score": 0~100 정수 (공개 적합도. 80 이상이면 publish 가 자연스럽습니다),
 "issues": ["구체적 문제 1", "구체적 문제 2"],
 "commercial_intent": true | false,
 "product_query": "상품 검색어" | null,
 "evergreen": true | false}"""

USER_TEMPLATE = """## 참고 기사 (작성 모델에게 주어진 자료 전부)

{context}

## 심사 대상 글

제목: {title}
요약: {description}
태그: {labels}

본문(HTML):
{body}

위 기준대로 판정하고 JSON 만 출력하세요."""


@dataclass
class Review:
    verdict: str = "hold"
    score: int = 0
    issues: list[str] = field(default_factory=list)
    commercial_intent: bool = False
    product_query: str | None = None
    evergreen: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def cost_usd(self) -> float:
        price_in, price_out = PRICES.get(self.model, (2.00, 10.00))
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000

    def approved(self, min_score: int) -> bool:
        return self.verdict == "publish" and self.score >= min_score


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse(raw: str, model: str) -> Review:
    """모델 응답에서 JSON 을 꺼냅니다. 코드 펜스가 섞여도 처리합니다."""
    m = _JSON_BLOCK.search(raw)
    if not m:
        raise ValueError(f"검수 응답에 JSON 이 없습니다: {raw[:200]!r}")
    data = json.loads(m.group(0))

    verdict = str(data.get("verdict", "hold")).lower().strip()
    if verdict not in ("publish", "hold", "reject"):
        verdict = "hold"

    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0

    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]

    query = data.get("product_query")
    query = str(query).strip() if query else None

    return Review(
        verdict=verdict,
        score=max(0, min(100, score)),
        issues=[str(i) for i in issues][:8],
        commercial_intent=bool(data.get("commercial_intent", False)),
        product_query=query,
        evergreen=bool(data.get("evergreen", False)),
        model=model,
    )


def review(
    cfg: dict,
    article: Article,
    context: str,
    client: anthropic.Anthropic | None = None,
) -> Review:
    r = cfg["review"]
    model = r["model"]
    client = client or anthropic.Anthropic()

    user = USER_TEMPLATE.format(
        context=context,
        title=article.title,
        description=article.description,
        labels=", ".join(article.labels),
        body=article.body_html,
    )

    response = client.messages.create(
        model=model,
        max_tokens=r.get("max_tokens", 2000),
        system=SYSTEM,
        output_config={"effort": r.get("effort", "medium")},
        messages=[{"role": "user", "content": user}],
    )

    if response.stop_reason == "refusal":
        # 검수 모델이 내용 자체를 거부했다면 그 글은 올리지 않는 게 맞습니다.
        result = Review(verdict="reject", score=0, issues=["검수 모델이 내용을 거부함"], model=model)
    else:
        raw = "".join(b.text for b in response.content if b.type == "text")
        result = _parse(raw, model)

    result.input_tokens = response.usage.input_tokens
    result.output_tokens = response.usage.output_tokens
    log.info(
        "[%s] 검수: %s (점수 %d) %s",
        article.keyword, result.verdict, result.score,
        f"— {'; '.join(result.issues[:3])}" if result.issues else "",
    )
    return result
