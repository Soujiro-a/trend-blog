"""Claude API 로 블로그 글을 씁니다."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import anthropic

from .trends import Candidate
from .trends.base import NewsRef

log = logging.getLogger(__name__)

# $ / 1M 토큰 (입력, 출력) — 비용 추정용
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

SYSTEM = """당신은 한국의 시사·생활정보 블로그 편집자입니다. 실시간 검색어에 오른 \
주제를 독자가 "검색해서 들어왔을 때 궁금했던 걸 실제로 해결하고 나가는" 글로 씁니다.

## 지켜야 할 원칙

1. **사실만 쓴다.** 제공된 참고 기사에 근거가 없는 내용은 쓰지 않습니다. 추측, 루머, \
"~라는 말이 있다" 같은 표현은 금지입니다. 확인되지 않은 부분은 "현재까지 확인된 바로는", \
"아직 공식 발표는 없습니다"처럼 모르는 것을 모른다고 씁니다.
2. **베껴 쓰지 않는다.** 기사 문장을 그대로 옮기지 말고 사실만 취해 새로 씁니다. \
직접 인용은 꼭 필요할 때 한 문장 이내로, 따옴표와 출처를 붙여서만 씁니다.
3. **기사에 없는 값을 더한다.** 단신 나열이 아니라 배경 설명, 용어 풀이, 앞뒤 맥락, \
독자에게 미치는 영향, 앞으로 볼 지점을 정리해 줍니다. 이것이 이 글의 존재 이유입니다.
4. **개인을 함부로 다루지 않는다.** 공인의 공적 활동만 다루고, 사생활·의혹·수사 중인 \
사안은 쓰지 않습니다. 주제 자체가 그런 것뿐이라면 본문 대신 SKIP 을 출력합니다.
5. **과장 제목을 쓰지 않는다.** "충격", "경악", "발칵" 같은 낚시성 표현은 쓰지 않습니다. \
제목은 검색어를 자연스럽게 포함하되 내용과 정확히 일치해야 합니다.

## 출력 형식

반드시 아래 4개 블록만, 이 순서대로 출력합니다. 다른 말은 덧붙이지 마세요.

<<<TITLE>>>
30~45자 제목. 핵심 검색어를 앞쪽에 자연스럽게 포함.
<<<DESCRIPTION>>>
검색 결과에 노출될 요약 2문장 (100~150자).
<<<LABELS>>>
쉼표로 구분한 태그 3~5개.
<<<BODY>>>
HTML 본문.

## 본문 작성 규칙

- `<h1>` 은 쓰지 않습니다(제목이 따로 들어갑니다). 소제목은 `<h2>`, 그 아래는 `<h3>`.
- 허용 태그: `<h2> <h3> <p> <ul> <ol> <li> <strong> <em> <table> <thead> <tbody> <tr> <th> <td> <blockquote> <a>`
- 구성: 도입부(3~4문장, 무슨 일인지 먼저 결론부터) → 핵심 내용 `<h2>` 2~4개 \
→ 정리 표나 목록 1개 → `<h2>자주 묻는 질문</h2>` 아래 Q&A 3개 → `<h2>참고한 자료</h2>`.
- 문단은 2~4문장으로 짧게 끊습니다. 모바일에서 읽기 쉬워야 합니다.
- 분량은 공백 포함 **{target_length}자 이상**. 다만 분량을 채우려고 같은 말을 \
다시 쓰거나 뻔한 일반론을 덧붙이지 마세요. 쓸 내용이 부족하면 배경·용어·맥락을 더 파고드세요.

## 참고한 자료 작성 규칙 (중요)

리서치 자료에 붙은 표시를 그대로 따릅니다. 없는 링크를 지어내면 안 됩니다.

- `링크(걸어도 됨): <주소>` 가 붙은 기사 → `<li><a href="주소">제목 - 매체명</a></li>`
- `링크 없음` 이 붙은 기사 → 링크 없이 `<li>매체명 「제목」</li>` 로만 씁니다
- 목록 마지막에 '더 읽기' 링크를 한 줄 넣습니다:
  `<li><a href="{{더_읽기_주소}}">이 주제 관련 최신 기사 더 보기</a></li>`
  (`{{더_읽기_주소}}` 는 리서치 자료에 주어진 주소를 그대로 씁니다)
{footer_rule}

주제가 위 4번에 걸려 쓸 수 없다면, 다른 출력 없이 `SKIP: <이유>` 한 줄만 출력하세요."""

USER_TEMPLATE = """오늘은 {date} 입니다. 아래 실시간 인기 키워드로 블로그 글을 써 주세요.

{context}

이 키워드가 왜 지금 검색되는지부터 파악하고, 검색해서 들어온 독자가 궁금해할 내용을 \
빠짐없이 정리해 주세요."""


@dataclass
class Article:
    keyword: str
    title: str
    description: str
    labels: list[str] = field(default_factory=list)
    body_html: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def cost_usd(self) -> float:
        price_in, price_out = PRICES.get(self.model, (5.00, 25.00))
        return (
            self.input_tokens * price_in + self.output_tokens * price_out
        ) / 1_000_000


class SkippedByModel(Exception):
    """모델이 주제 부적합으로 작성을 거부한 경우."""


_BLOCK = re.compile(
    r"<<<TITLE>>>(?P<title>.*?)"
    r"<<<DESCRIPTION>>>(?P<description>.*?)"
    r"<<<LABELS>>>(?P<labels>.*?)"
    r"<<<BODY>>>(?P<body>.*)",
    re.DOTALL,
)


def _parse(raw: str, keyword: str, model: str) -> Article:
    stripped = raw.strip()
    if stripped.startswith("SKIP"):
        raise SkippedByModel(stripped[:200])

    m = _BLOCK.search(stripped)
    if not m:
        raise ValueError(f"모델 응답 형식이 예상과 다릅니다: {stripped[:200]!r}")

    labels = [l.strip() for l in m.group("labels").replace("\n", ",").split(",")]
    return Article(
        keyword=keyword,
        title=m.group("title").strip(),
        description=m.group("description").strip(),
        labels=[l for l in labels if l][:5],
        body_html=m.group("body").strip(),
        model=model,
    )


def write(
    cfg: dict,
    candidate: Candidate,
    context: str,
    refs: list[NewsRef],
    date_str: str,
    client: anthropic.Anthropic | None = None,
) -> Article:
    w = cfg["writer"]
    model = w["model"]
    client = client or anthropic.Anthropic()

    # 글 맨 아래 안내 문구. config 에서 비워두면 아무것도 붙이지 않습니다.
    note = (w.get("footer_note") or "").strip()
    if note:
        footer_rule = (
            "- 맨 끝에 다음 문장을 그대로 넣습니다:\n"
            f'  `<p><em>{note.format(date=date_str)}</em></p>`'
        )
    else:
        footer_rule = "- 글 끝에 고지·안내 성격의 문장을 따로 붙이지 마세요."

    system = SYSTEM.format(
        target_length=w["target_length"], date=date_str, footer_rule=footer_rule
    )
    user = USER_TEMPLATE.format(date=date_str, context=context)

    # max_tokens 가 크고 사고(thinking) 시간이 길 수 있어 스트리밍으로 받습니다.
    with client.messages.stream(
        model=model,
        max_tokens=w["max_tokens"],
        system=system,
        output_config={"effort": w.get("effort", "high")},
        messages=[{"role": "user", "content": user}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", "") or ""
        raise SkippedByModel(f"모델이 작성을 거부했습니다. {detail}")
    if response.stop_reason == "max_tokens":
        log.warning("[%s] max_tokens 에 걸려 본문이 잘렸을 수 있습니다.", candidate.keyword)

    raw = "".join(b.text for b in response.content if b.type == "text")
    article = _parse(raw, candidate.keyword, model)
    article.input_tokens = response.usage.input_tokens
    article.output_tokens = response.usage.output_tokens
    return article
