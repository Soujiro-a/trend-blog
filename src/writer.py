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
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
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
2-1. **구체적인 숫자는 자료에 있을 때만 쓴다.** 과태료·수수료·세율·기한·법 조항 번호처럼 \
딱 떨어지는 값은 **제공된 참고 자료에 그대로 나올 때만** 적습니다. 알고 있다고 생각되더라도 \
자료에 없으면 쓰지 마세요. 그 값이 글에 꼭 필요하면 숫자를 지어내지 말고 \
"정확한 금액은 <공식 기관/사이트 이름>에서 확인할 수 있습니다"처럼 **확인하는 방법**을 알려 주세요. \
제도는 자주 바뀌어서 틀린 숫자 하나가 글 전체의 신뢰를 깎습니다.
3. **기사에 없는 값을 더한다.** 단신 나열이 아니라 배경 설명, 용어 풀이, 앞뒤 맥락, \
독자에게 미치는 영향, 앞으로 볼 지점을 정리해 줍니다. 이것이 이 글의 존재 이유입니다.
4. **개인을 함부로 다루지 않는다.** 공인의 공적 활동만 다루고, 사생활·의혹·수사 중인 \
사안은 쓰지 않습니다. 주제 자체가 그런 것뿐이라면 본문 대신 SKIP 을 출력합니다.
5. **과장 제목을 쓰지 않는다.** "충격", "경악", "발칵" 같은 낚시성 표현은 쓰지 않습니다. \
제목은 검색어를 자연스럽게 포함하되 내용과 정확히 일치해야 합니다.
6. **"왜 검색되는가"를 쓰지 않는다.** 독자는 이미 그게 궁금해서 검색해 들어온 사람입니다. \
왜 요즘 이게 화제인지, 왜 많이 찾는지를 설명하는 문단이나 소제목(`왜 지금 ~ 검색될까`, \
`요즘 ~가 화제인 이유`, `왜 검색되는가` 등)은 **만들지 마세요.** 첫 문단부터 독자가 찾으러 온 \
답을 바로 줍니다. 배경 설명이 필요하면 답을 먼저 준 뒤에 덧붙입니다.

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
- 구성: 도입부(2~3문장, **독자가 찾으러 온 답을 바로**. 검색 이유 설명 금지) → 핵심 내용 `<h2>` 2~4개 \
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

## 이 블로그의 다른 글 링크 (있을 때만)

자료에 `## 이 블로그의 다른 글` 목록이 주어질 때가 있습니다. 같은 블로그의 이미 공개된 글입니다.

- **억지로 넣지 마세요.** 지금 쓰는 문장과 정말로 이어질 때만 넣습니다. 넣을 곳이 없으면 하나도 안 넣어도 됩니다. \
최대 2개까지만.
- **문장 안에 녹입니다.** 설명하다가 더 깊이 다룬 적이 있는 지점에서, 읽는 흐름을 끊지 않게 자연스럽게 겁니다.
  - 좋은 예: `<p>이때 필요한 서류는 <a href="주소">정기검사 준비물</a>에서 정리한 것과 같습니다.</p>`
  - 좋은 예: `<p>환급 조건은 조금 다릅니다. <a href="주소">관세 환급 절차</a>를 먼저 확인하면 이해가 빠릅니다.</p>`
  - 나쁜 예: `<h2>관련 글</h2>` 같은 별도 섹션, `<p>함께 읽어보세요: ...</p>` 같은 목록 나열
  - 나쁜 예: `여기를 클릭`, `이 글 보기` 처럼 제목을 감춘 링크
- 링크 글자는 **그 글이 무슨 내용인지 드러나게** 씁니다. 목록에 적힌 제목을 그대로 쓰거나 자연스럽게 줄여 씁니다.
- 주소는 목록에 있는 것을 **그대로** 씁니다. 목록에 없는 이 블로그 주소를 만들어 내면 안 됩니다.
- `참고한 자료` 목록에는 넣지 마세요. 거기는 외부 출처 자리입니다.

주제가 위 4번에 걸려 쓸 수 없다면, 다른 출력 없이 `SKIP: <이유>` 한 줄만 출력하세요."""

USER_TEMPLATE = """오늘은 {date} 입니다. 아래 실시간 인기 키워드로 블로그 글을 써 주세요.

{context}

검색해서 들어온 독자가 궁금해할 내용을 빠짐없이 정리해 주세요. \
왜 이 주제가 검색되는지는 설명하지 말고, 답부터 주세요."""

# 장수(evergreen) 글은 "지금 왜 뜨는가"가 아니라 "이게 무엇이고 어떻게 하는가"를 씁니다.
# 한 달 뒤에 읽어도 어색하지 않도록 시점 표현을 피하게 합니다.
USER_TEMPLATE_EVERGREEN = """오늘은 {date} 입니다. 아래 주제로 **오래 읽히는 해설 글**을 써 주세요.

{context}

이 글은 실시간 뉴스 정리가 아닙니다. 독자가 몇 달 뒤에 검색해 들어와도 그대로 유용해야 합니다.
- 개념 정의 → 작동 방식/절차 → 장단점이나 주의점 → 독자가 실제로 할 수 있는 것, 순서로 씁니다.
- "최근", "어제", "이번 주" 같은 시점 표현은 쓰지 않습니다. 날짜가 필요하면 연도까지 명시합니다.
- 참고 기사는 배경 이해용입니다. 기사 내용 요약이 아니라 주제 자체를 설명하세요."""


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
    mode: str = "trend",
    persona: str = "",
) -> Article:
    w = cfg["writer"]
    model = w["model"]
    client = client or anthropic.Anthropic()
    template = USER_TEMPLATE_EVERGREEN if mode == "evergreen" else USER_TEMPLATE

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
    # 함대 모드: 블로그마다 다른 문체·관점. 같은 이슈를 여러 블로그가 써도 글이 달라지게 하는 장치입니다.
    if persona.strip():
        system += "\n\n## 이 블로그의 성격 (위 원칙 안에서 따르세요)\n" + persona.strip()
    user = template.format(date=date_str, context=context)

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
