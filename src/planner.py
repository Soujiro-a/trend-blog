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

from . import demand, llm
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

## 넓은 검색어보다 구체적인 상황
이 블로그는 새로 생겨 검색 신뢰가 낮습니다. "타이어 교체 주기", "여권 재발급 기간"처럼 넓은 검색어는 \
정부 기관·언론사·대형 블로그가 상위를 차지해 새 블로그 글이 보이지 않습니다(2026-10 확인: 노출 거의 0). \
대신 **이미 문제가 생긴 사람이 자기 상황을 그대로 치는 검색어**를 고르세요.
- 조건이 붙은 상황: "자동차 검사 기간 지났을 때 과태료 계산" ○ / "자동차 정기검사 기간" ×
- 막힌 지점: "해외직구 반품 배송비 판매자가 과다 청구할 때" ○ / "해외직구 반품 방법" ×
- 예외·경계: "여권 유효기간 6개월 안 남았는데 출국 가능한 나라" ○ / "여권 유효기간" ×
- 비교·선택: "니트 보풀 제거기와 테이프 차이" ○ / "니트 관리법" ×
넓은 주제는 이미 쓴 글감 목록에 비슷한 것이 있으면 그 글에서 다루지 못한 구체적인 상황으로 좁히세요. \
다만 너무 좁아 한 달에 몇 명도 검색하지 않을 표현(지역·제품 모델명까지 붙은 것)은 피합니다.

## 실제 검색 수요가 있으면 그것부터
요청에 '실제 검색 수요' 목록이 있으면, 그것은 이 블로그가 구글에 **이미 노출되는데 순위나 클릭이 낮은 검색어**입니다. 그 검색어에 바로 답하는 글감을 먼저 고르세요(이미 쓴 글과 겹치면 그 글이 놓친 상황으로 좁혀서). 주제 밖의 검색어는 무시합니다.

## 피해야 할 것
- 특정 개인의 사생활·의혹·수사, 사건사고
- 의료 진단, 투자 권유, 법률 자문으로 읽힐 수 있는 단정
- "TOP 10", "총정리" 같은 속 빈 나열형 제목
- 이 블로그 주제와 함대 다른 블로그 몫을 한 글에 섞는 글감 (예: 자동차 블로그에서 "자동차·가정용 에어컨 필터 교체 주기"). \
'다루지 않음' 목록에 딱 그 말이 없어도, 독자가 다른 블로그 주제(살림·가전, 쇼핑, 여행, 세금 등)를 찾으러 온 검색어면 고르지 않습니다.

## 출력
아래 JSON 배열만. 설명이나 코드 펜스는 붙이지 마세요.
[{"topic": "글 제목이 될 검색어형 주제", "pillar": "하위 축 목록에 적힌 이름을 글자 그대로 하나", "why": "왜 검색될지 한 줄", "search": "참고 기사 검색용 키워드 2~3 단어"}]
pillar 는 글 분류 라벨이 되므로 목록의 이름을 바꾸거나 줄이지 말고 그대로 복사하세요."""

USER_TEMPLATE = """## 이 블로그

- 주제: {subject}
- 하위 축: {pillars}
- 독자: {audience}
- 다루지 않음 (함대의 다른 블로그 몫이라 이 블로그 글감으로 고르면 안 됨): {excludes}

## 실제 검색 수요 (Search Console, 최근 4주 — 노출은 되는데 순위·클릭이 낮은 검색어)

{demand}

## 이미 쓴 글감 (겹치면 안 됨 — 이 블로그의 전체 글)

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

    # 이 블로그의 **전체** 글(최근 400편). 예전엔 최근 60편만 넘겨, 글이 쌓이면 같은 검색어를 두고 내 글끼리
    # 경쟁하는 글이 나올 수 있었습니다(2026-10-14 개선점 2). 400편 ≈ 6천 토큰, Sonnet 기준 $0.01 안팎.
    done = [h.get("keyword", "") for h in history if h.get("keyword")][-400:]
    done_lines = "\n".join(f"- {k}" for k in done) or "- (없음)"
    opps = demand.search_console(blog, cfg)
    if opps:
        log.info("[%s] Search Console 검색 수요 %d개 (노출 최다: %s)", blog.id, len(opps), opps[0].query)

    # 글감 목록 뽑기는 깊이 생각할 일이 아닙니다. effort 를 낮춰 thinking 이 출력 자리를 먹지 않게 하고,
    # max_tokens 도 넉넉히 둡니다(thinking 토큰도 이 안에서 씁니다 — src/llm.py 참고).
    response = client.messages.create(
        model=pl["model"],
        max_tokens=int(pl.get("max_tokens", 8000)),
        output_config={"effort": pl.get("effort", "low")},
        system=llm.cached(SYSTEM),
        messages=[
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    subject=blog.subject,
                    pillars=", ".join(blog.pillars) or "(지정 없음 — 주제에서 스스로 나누세요)",
                    audience=blog.audience or "해당 주제를 검색해서 들어오는 일반 독자",
                    excludes=" / ".join(blog.excludes) or "(없음)",
                    demand=demand.demand_block(opps),
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

    topics, reinforce = split_cannibal(cfg, topics, history)
    if reinforce:
        log.info("[%s] 기존 글과 겹치는 글감 %d개 → 새 글 대신 보강 후보로 기록", blog.id, len(reinforce))
        record_reinforce(blog, reinforce)

    use_naver = bool(pl.get("naver_autocomplete", True))
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
        # 네이버 자동완성에 뜨는 검색 표현이면 사람들이 실제로 치는 말이므로 앞에 둡니다 (조회 실패는 무시).
        if use_naver and demand.has_naver_demand(t["search"]):
            c.score = round(c.score + 0.1, 4)
            c.note = (c.note + " · " if c.note else "") + "네이버 자동완성 ○"
        candidates.append(c)
    candidates.sort(key=lambda c: -c.score)
    return candidates


def split_cannibal(cfg: dict, topics: list[dict], history: list[dict]) -> tuple[list[dict], list[dict]]:
    """글감을 (새 글로 쓸 것, 기존 글과 겹쳐 보강으로 돌릴 것) 으로 나눕니다.

    state.filter_seen 은 최근 14일만 봅니다(실시간 이슈용). 기획 글은 몇 달 뒤에도 같은 검색어를 두고
    내 글끼리 경쟁하므로 **이력 전체**와 비교합니다. 겹친 글감은 버리지 않고, 그 기존 글을 공식 자료로 다시
    확인·보강하는 후보(scripts/refresh_facts.py)로 넘깁니다.
    """
    threshold = float(cfg["dedupe"]["similarity_threshold"])
    live = [h for h in history if h.get("status") == "live" and h.get("url")]
    fresh, reinforce = [], []
    for t in topics:
        hit = next((h for h in live if similarity(t["topic"], h.get("keyword", "")) >= threshold
                    or similarity(t["topic"], h.get("title", "")) >= threshold), None)
        if hit:
            reinforce.append({"topic": t["topic"], "url": hit["url"], "title": hit.get("title", ""),
                              "keyword": hit.get("keyword", "")})
        else:
            fresh.append(t)
    return fresh, reinforce


def record_reinforce(blog, items: list[dict]) -> None:
    """겹친 글감을 data/blogs/<id>/reinforce.json 에 남깁니다 (사실 갱신이 이 글들을 먼저 봅니다)."""
    from datetime import datetime
    from .state import KST
    path = blog.dir / "reinforce.json"
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cur = []
    known = {c.get("url") for c in cur}
    stamp = datetime.now(KST).isoformat(timespec="minutes")
    for it in items:
        if it["url"] not in known:
            cur.append({**it, "at": stamp})
            known.add(it["url"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur[-100:], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
