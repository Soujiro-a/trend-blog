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

from . import llm
from .writer import PRICES, Article

log = logging.getLogger(__name__)

SYSTEM = """당신은 한국 시사·생활정보 블로그의 발행 책임자입니다. AI 가 쓴 글이 \
그대로 공개돼도 되는지 최종 판단합니다. 공개된 글은 블로그 운영자의 법적 책임이 되고 \
구글 애드센스 심사 대상이 되므로, 아래 기준으로 엄격하게 봅니다.

## 판정 기준

**reject (올리지 않음)** — 하나라도 해당하면:
- 특정인의 사생활·건강·가족·연애, 수사 중인 사건, 확인되지 않은 의혹을 다룸
- 참고 자료(`공식 안내 자료`·참고 기사)에 없는 사실을 단정적으로 주장 (지어낸 숫자, 발언, 날짜, 인용). \
공식 안내 자료에 적힌 기한·금액을 본문에 옮긴 것은 근거 있는 서술입니다. \
`## 공식 확인처` 에 적힌 기관·전화·사이트를 안내한 것도 근거 있는 서술입니다. \
`가정 예시` 라고 밝히고 자료에 적힌 요율·공식으로 계산한 숫자는 지어낸 숫자가 아닙니다(계산이 틀렸으면 문제입니다). \
글 맨 끝의 `○○ 기준으로 정리한 내용입니다` 같은 작성일 안내 문구는 운영자가 붙이게 한 것이라 근거를 따지지 않습니다.
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
(독자는 이미 알고 들어왔습니다. 답부터 나와야 합니다. 답을 먼저 준 뒤 자료에 있는 불이익·기한을 짚는 단락과, \
본문에 무엇이 더 있는지 알리는 도입부 끝 한 문장은 정상입니다)
- **가치가 별로 없는 콘텐츠 (구글 애드센스 거절 사유, 2026-10-29 이 함대에서 실제로 거절됨)** — 아래 중 하나라도:
  - 본문이 공식 안내 페이지 한 곳이나 기사 몇 개의 내용을 순서·표현만 바꿔 옮긴 수준이고, 독자 상황별 판단표, \
여러 자료를 모은 정리, 예외·기한 경과 같은 경우 정리, 헷갈리는 제도의 구분처럼 **이 글만의 정리**가 없음
  - 본문 뼈대가 "○○ 보도에 따르면"으로 이어지는 언론·블로그 요약형
  - 검색해 들어온 독자가 자료로 답할 수 있는 핵심(할 수 있는지, 기한, 신청처, 준비물)을 얻지 못해 다시 검색해야 함
  - 제목이 묻는 핵심 질문(할 수 있는지, 얼마인지, 어떻게 계산하는지)의 답을 "○○(126·1355·1350 등)에 문의하세요"나 \
"공식 안내에 없습니다"로 넘김 (2026-10-30 점검: 구글이 수집하고도 색인하지 않은 글의 공통점)
  - 자료의 `## 이 블로그의 다른 글` 목록에 **같은 검색 의도**의 글이 이미 있고, 이 글이 그 글과 거의 같은 답을 되풀이함 \
(같은 제도라도 다른 상황·절차를 다루면 정상)
  이 항목으로 보류할 때는 issues 에 "가치 부족:" 으로 시작해 무엇이 빠졌는지 구체적으로 적습니다.
- 내부 링크가 본문 문맥과 무관한 자리에 억지로 들어갔거나, 본문 중간에 `관련 글` 같은 나열 섹션으로 붙어 있음 \
(`참고한 자료` 앞쪽의 `함께 읽으면 좋은 글` 섹션에 목록 주소를 최대 3개 건 것은 정상입니다)

**publish (공개)** — 위에 걸리는 것이 없고, 검색해 들어온 독자의 궁금증을 \
실제로 해결해 주는 글.

## 부가 판단

- `commercial_intent`: 독자가 이 주제로 **물건을 살 가능성**이 있는가. \
(예: 신제품 출시, 특정 기기·식품·도서·게임 → true / 정치·사건·스포츠 경기 결과 → false)
- `product_query`: commercial_intent 가 true 면 쇼핑몰에서 검색할 짧은 상품명 \
(2~4 단어, 브랜드+품목). false 면 null.
- `evergreen`: 이 글이 한 달 뒤에도 검색될 만한 해설·안내 성격인가.
- `card_ok`: `요약 카드`의 모든 줄이 **본문 내용과 일치**하는가. 본문에 없는 숫자·단정, 본문과 다른 값이 하나라도 있으면 false (카드만 빼고 발행하므로 글 판정·점수에는 반영하지 마세요). 카드가 비어 있으면 true.

## 출력 형식

아래 JSON 하나만 출력합니다. 설명이나 마크다운 코드 펜스는 붙이지 마세요.

{"verdict": "publish" | "hold" | "reject",
 "score": 0~100 정수 (공개 적합도. 80 이상이면 publish 가 자연스럽습니다),
 "issues": ["구체적 문제 1", "구체적 문제 2"],
 "commercial_intent": true | false,
 "product_query": "상품 검색어" | null,
 "evergreen": true | false,
 "card_ok": true | false}"""

USER_TEMPLATE = """## 참고 자료 (작성 모델에게 주어진 자료 전부)

{context}

## 심사 대상 글

제목: {title}
요약: {description}
태그: {labels}

요약 카드(글 맨 앞 이미지에 들어갈 글):
{card}

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
    card_ok: bool = True
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str = ""

    @property
    def cost_usd(self) -> float:
        price_in, price_out = PRICES.get(self.model, (2.00, 10.00))
        return (self.input_tokens * price_in
                + self.cache_read_tokens * price_in * llm.CACHE_READ_RATE
                + self.cache_write_tokens * price_in * llm.CACHE_WRITE_RATE
                + self.output_tokens * price_out) / 1_000_000

    def approved(self, min_score: int) -> bool:
        return self.verdict == "publish" and self.score >= min_score


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# 복제 콘텐츠 (애드센스 정책 '복제된 콘텐츠', 거절 사유 '가치가 별로 없는 콘텐츠'): 참고 자료 문장을 길게 그대로 옮긴 곳.
# 공식 안내의 기관명·서식 이름처럼 짧게 같은 것은 정상이라, 공백을 정리한 뒤 COPY_SPAN 자 넘게 이어서 같은 곳만 셉니다.
# 검수 모델은 대조를 눈으로 하므로 길게 베낀 곳을 놓칠 수 있어 코드로 봅니다(2026-10-31).
COPY_SPAN = 80
COPY_MAX = 2   # 이보다 많으면 publish 를 hold 로


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def copied_spans(body_html: str, context: str, span: int = COPY_SPAN) -> list[str]:
    """본문에서 참고 자료와 span 자 넘게 그대로 같은 구간들."""
    text, ctx = _plain(body_html), _plain(context)
    out, i, step = [], 0, max(10, span // 4)
    while i + span <= len(text):
        if text[i:i + span] in ctx:
            j = i + span
            while j < len(text) and text[i:j + 1] in ctx:
                j += 1
            out.append(text[i:j])
            i = j
        else:
            i += step
    return out


def flag_copies(result: "Review", article: Article, context: str) -> "Review":
    spans = copied_spans(article.body_html, context)
    if len(spans) > COPY_MAX:
        result.issues = [f"복제 콘텐츠: 참고 자료 문장을 {COPY_SPAN}자 넘게 그대로 옮긴 곳 {len(spans)}군데 "
                         f"(예: \"{spans[0][:40]}…\") — 사실만 취해 새 문장으로 쓰고 이 글만의 정리를 더할 것",
                         *result.issues][:8]
        if result.verdict == "publish":
            result.verdict = "hold"
    return result


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
        card_ok=bool(data.get("card_ok", True)),
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
    client = client or llm.client()

    user = USER_TEMPLATE.format(
        context=context,
        title=article.title,
        description=article.description,
        labels=", ".join(article.labels),
        body=article.body_html,
        card=article.card or "(없음)",
    )

    response = client.messages.create(
        model=model,
        max_tokens=r.get("max_tokens", 6000),
        system=llm.cached(SYSTEM),
        output_config={"effort": r.get("effort", "medium")},
        messages=[{"role": "user", "content": user}],
    )

    if response.stop_reason == "refusal":
        # 검수 모델이 내용 자체를 거부했다면 그 글은 올리지 않는 게 맞습니다.
        result = Review(verdict="reject", score=0, issues=["검수 모델이 내용을 거부함"], model=model)
    else:
        raw = llm.text_of(response, f"[{article.keyword}] 검수")
        result = _parse(raw, model)
        result = flag_copies(result, article, context)

    result.input_tokens = response.usage.input_tokens
    result.output_tokens = response.usage.output_tokens
    result.cache_read_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    result.cache_write_tokens = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    log.info(
        "[%s] 검수: %s (점수 %d) %s",
        article.keyword, result.verdict, result.score,
        f"— {'; '.join(result.issues[:3])}" if result.issues else "",
    )
    return result


# ---- 기존 글 다듬기 확인 (scripts/retrofit_posts.py) ----
# 숫자·링크·구조는 writer.polish_problems 가 코드로 봅니다. 여기서는 숫자 없이도 뜻이 바뀌는 경우
# ("할 수 있습니다" → "해야 합니다", 조건·예외가 빠짐, 단정이 세짐)를 검수 모델이 전후 비교로 봅니다.
# 2026-10-19: same true/false 한 값만 받았더니 모델이 "표현만 바뀌고 사실은 같음", "경미"라고 적고도 false 를
# 내 다듬기 8편 중 6편이 버려졌습니다. 바뀐 곳마다 사실/표현을 나누게 하고, 사실이 하나라도 있을 때만 버립니다.
SAME_MEANING_SYSTEM = """당신은 한국어 생활정보 블로그의 발행 책임자입니다. 이미 공개된 글의 문장을 교정 편집자가 다듬었습니다. 다듬은 글이 원래 글과 **같은 사실을 같은 강도로** 말하는지만 확인합니다. 문체가 나아졌는지는 판단하지 않습니다.
문장을 다듬었으니 표현이 달라진 곳은 당연히 많습니다. 바뀐 곳을 하나씩 보고 `사실` 인지 `표현` 인지 나눕니다.

`사실` — 독자가 아는 내용이나 할 일이 달라지는 변경입니다.
- 가능·권장이 의무로, 또는 그 반대로 바뀜 ("할 수 있습니다" ↔ "해야 합니다", "~하면 좋습니다" → "~하세요")
- 조건·예외·대상·기한·금액이 빠지거나 넓어지거나 좁아짐
- 나열한 예시·항목이 빠지거나 바뀜 (예: "물에 젖음·찢어짐·낙서" → "낙서 등")
- 기관명·사이트 주소·전화번호·서식 이름이 빠짐
- 원래 글에 없던 이유·조언·전망·사실이 더해짐
- 범위·강도를 정하는 말이 더해지거나 빠짐 ("약", "보통", "만", "반드시", "~로 안내합니다" → "~입니다")
- 독자가 할 일을 누가 하는지(신청인·기관·회사)가 바뀜

`표현` — 같은 사실을 다르게 말한 것입니다. 이것만 있으면 같은 글입니다.
- 어순·문장 구조·문장 나누기와 합치기, 쉬운 말로 바꿈 ("빈발한다" → "자주 일어난다", "판독" → "읽기")
- 같은 강도의 의무 표현끼리 바꿈 ("~하는 것이 필요합니다" ↔ "~해야 합니다")
- 피동과 능동 사이를 바꿨지만 할 일과 하는 사람은 그대로임
- "결국", "핵심은", "참고로" 같은 연결어가 빠지거나 바뀜
- 글의 구성을 안내하는 문장("이 글은 ~을 정리했습니다")이 빠짐 — 본문의 사실이 아닙니다
- 시제·어미가 바뀌었지만 사실은 그대로임 ("거부하는 경우도 보도됐습니다" → "거부한 사례도 보도됐습니다")

헷갈리면 "원래 글만 읽은 독자와 다듬은 글만 읽은 독자가 다르게 행동하거나 다른 사실을 알게 되는가"로 판단합니다. 그렇다면 `사실`, 아니면 `표현` 입니다.

JSON 한 덩어리만 출력합니다. 바뀐 곳마다 먼저 원래와 다듬은 문장을 적고, 이유를 쓴 다음, 그 이유에 맞게 kind 를 정합니다. 이유에 "표현", "같은 내용", "의미 동일" 이라고 썼다면 kind 는 `표현` 입니다. reason 은 비워 두지 않습니다.
{"changes": [{"where": "바뀐 곳: 원래 → 다듬은 글", "reason": "독자가 아는 사실·할 일이 달라지는지", "kind": "사실" 또는 "표현"}, ...]}"""


def meaning_verdict(data: dict) -> tuple[bool, list[str]]:
    """same_meaning 응답 JSON → (같음, 사실 변경 목록). `사실` 이 하나도 없으면 같은 글입니다."""
    changes = data.get("changes")
    if not isinstance(changes, list):
        # 옛 형식({"same": ..., "problems": [...]}) 이나 이상한 응답은 안전하게 옛 판단을 따릅니다.
        problems = data.get("problems") or []
        problems = [str(p) for p in (problems if isinstance(problems, list) else [problems])][:8]
        return data.get("same") is True, problems
    facts = [str(c.get("where", "")) + (f' ({c["reason"]})' if c.get("reason") else "") for c in changes
             if isinstance(c, dict) and not _style_only(c)]
    return not facts, facts[:8]


# 이유에는 "표현 차이뿐" 이라고 쓰고 kind 는 `사실` 로 붙이는 일이 있습니다(2026-10-20 gaganam1). 이유가 표현만 바뀌었다고
# 말하고 사실이 달라졌다는 말이 없으면 이유를 따릅니다. 이유가 비었으면 kind 를 그대로 믿습니다(안전한 쪽).
_STYLE_REASON = re.compile(r"표현\s*(?:차이|만|뿐|상의|의\s*차이)|같은\s*(?:내용|뜻|의미|사실)|의미\s*(?:는|가)?\s*(?:동일|같|그대로|변화\s*없)"
                           r"|뜻\s*(?:은|이)?\s*(?:같|그대로|동일)|사실\s*(?:은|이)?\s*(?:같|그대로|동일|변화\s*없)")
_SAME_PHRASE = re.compile(r"(?:달라지|바뀌|변하|변경되)지\s*않|차이\s*(?:가|는)?\s*없|같은\s*(?:강도|의무)(?:의\s*의무)?")
_FACT_REASON = re.compile(r"아니|않|다르|달라|빠[지졌짐]|빠뜨|추가|더해|넓어|좁아|의무|강도|약해|강해")


def _style_only(c: dict) -> bool:
    if str(c.get("kind", "사실")).strip().strip("`") == "표현":
        return True
    reason = str(c.get("reason", ""))
    if not (_STYLE_REASON.search(reason) or _SAME_PHRASE.search(reason)):
        return False
    return not _FACT_REASON.search(_SAME_PHRASE.sub(" ", reason))


def same_meaning(cfg: dict, before_html: str, after_html: str, client: anthropic.Anthropic | None = None,
                 ) -> tuple[bool, list[str], float]:
    """다듬기 전후 본문의 뜻이 같은지. (같음, 사실 변경 목록, 비용)"""
    r = cfg["review"]
    client = client or llm.client()
    response = client.messages.create(
        model=r["model"],
        max_tokens=r.get("max_tokens", 6000),
        system=llm.cached(SAME_MEANING_SYSTEM),
        output_config={"effort": r.get("effort", "medium")},
        messages=[{"role": "user", "content": f"## 원래 글\n{before_html}\n\n## 다듬은 글\n{after_html}"}],
    )
    cost = llm.cost_of(response.usage, r["model"], (2.00, 10.00))
    if response.stop_reason == "refusal":
        return False, ["검수 모델이 내용을 거부함"], cost
    m = _JSON_BLOCK.search(llm.text_of(response, "다듬기 확인"))
    if not m:
        return False, ["확인 응답에 JSON 이 없음"], cost
    same, facts = meaning_verdict(json.loads(m.group(0)))
    return same, facts, cost


# ---- 사실 갱신 확인 (scripts/refresh_facts.py) ----
# 대조 모델이 찾은 from→to 변경을 코드가 본문에 치환한 뒤, 각 변경이 공식 자료와 맞는지 검수 모델이 한 번 더 봅니다.
FACT_CHANGES_SYSTEM = """당신은 한국어 생활정보 블로그의 발행 책임자입니다. 이미 공개된 글의 값(금액·기한·조건)을 공식 자료에 \
맞춰 고치려는 변경 목록을 받습니다. 변경마다 **자료에 그 값이 분명히 적혀 있는지**만 확인합니다.

이런 변경은 거부합니다.
- 자료에 없는 값으로 바꿈, 자료를 넓게 해석해 추정한 값
- 값만 고친 것이 아니라 조건·대상·예외가 달라지거나 새 주장이 들어감
- 자료의 기준일이 분명히 더 오래됨

JSON 한 덩어리만 출력합니다: {"ok": true 또는 false, "problems": ["거부 이유: 변경 번호와 근거", ...]}"""


def fact_changes_ok(cfg: dict, changes: list[dict], official_block: str, client: anthropic.Anthropic | None = None,
                    ) -> tuple[bool, list[str], float]:
    """(모든 변경이 자료와 맞음, 문제 목록, 비용)."""
    r = cfg["review"]
    client = client or llm.client()
    listed = "\n".join(
        f"{i}. {c['from']} → {c['to']}\n   근거: {c.get('source', '')} — {c.get('why', '')}"
        for i, c in enumerate(changes, start=1)
    )
    response = client.messages.create(
        model=r["model"],
        max_tokens=r.get("max_tokens", 6000),
        system=llm.cached(FACT_CHANGES_SYSTEM),
        output_config={"effort": r.get("effort", "medium")},
        messages=[{"role": "user", "content": f"## 변경 목록\n{listed}\n{official_block}"}],
    )
    cost = llm.cost_of(response.usage, r["model"], (2.00, 10.00))
    if response.stop_reason == "refusal":
        return False, ["검수 모델이 내용을 거부함"], cost
    m = _JSON_BLOCK.search(llm.text_of(response, "사실 갱신 확인"))
    if not m:
        return False, ["확인 응답에 JSON 이 없음"], cost
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, ["확인 응답 JSON 파싱 실패"], cost
    problems = data.get("problems") or []
    problems = [str(p) for p in (problems if isinstance(problems, list) else [problems])][:8]
    return data.get("ok") is True, problems, cost
