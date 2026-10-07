"""키워드별 리서치 자료 수집.

기사 본문을 긁어오지 않고 제목·요약·출처만 모읍니다.
저작권 문제를 피하고, 글은 이 자료를 바탕으로 새로 쓰게 합니다.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field

import anthropic

from . import llm
from .trends import Candidate
from .trends import google_news
from .trends.base import NewsRef
from .writer import PRICES

log = logging.getLogger(__name__)

# 검색 결과 페이지라 기사가 내려가도 링크가 깨지지 않습니다.
NAVER_NEWS_SEARCH = "https://search.naver.com/search.naver?where=news&query={q}"


def more_news_url(keyword: str) -> str:
    return NAVER_NEWS_SEARCH.format(q=urllib.parse.quote(keyword))


def is_planned(candidate: Candidate) -> bool:
    """블로그 주제 안에서 기획한 글감인가 (planner.propose · scripts/agent_brief.py 가 만든 후보)."""
    return "planner" in candidate.sources


def gather(cfg: dict, candidate: Candidate) -> list[NewsRef]:
    """구글 트렌드가 준 기사 + 키워드 검색 결과를 합칩니다."""
    limit = cfg["research"]["articles_per_keyword"]

    # variants 에는 대표 키워드도 들어 있어서 그대로 돌리면 같은 검색을 두 번 합니다.
    terms = list(dict.fromkeys([candidate.keyword, *candidate.variants]))

    refs: list[NewsRef] = list(candidate.news)
    for term in terms:
        if len(refs) >= limit * 2:
            break
        refs.extend(google_news.search(term, limit=limit))

    # 제목 기준 중복 제거
    seen: set[str] = set()
    unique: list[NewsRef] = []
    for r in refs:
        key = r.title.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(r)

    return unique[:limit]


# ---------------------------------------------------------------------------
# 공식 안내 자료 — 정부·공공기관 페이지
#
# 뉴스 제목·요약만으로는 독자가 검색한 핵심 숫자(기한·금액·신청처)가 비는 일이 많았습니다.
# 예: 청약철회 글이 "하자가 있으면 더 긴 기간이 적용된다"고만 쓰고 기간은 "법령정보센터에서 확인"으로
# 넘겼습니다(2026-09). 작성 규칙상 자료에 없는 숫자는 쓸 수 없기 때문입니다. 규칙을 풀지 않고,
# 숫자가 적힌 공식 안내 페이지를 자료에 넣어 줍니다. 작성·검수 모델이 같은 자료를 보므로
# 검수관도 그 숫자를 근거 있는 값으로 확인할 수 있습니다.
# ---------------------------------------------------------------------------

# 웹 검색 요금 $10 / 1,000회 (2026-09 기준)
SEARCH_PRICE_USD = 0.01

OFFICIAL_SYSTEM = """당신은 생활정보 블로그의 자료 조사원입니다. 주어진 글감에 대해 **정부·공공기관의 공식 안내 \
페이지**를 웹 검색으로 찾고, 검색해 들어온 독자가 가장 궁금해할 사실을 페이지에서 뽑아 옵니다.

- 독자가 읽기 쉬운 **안내 페이지**를 고릅니다. 기관의 민원·제도 안내, 정부24, 찾기쉬운 생활법령정보, \
정책브리핑, 소비자24 같은 곳입니다. 법령 조문 원문(국가법령정보센터)·논문·첨부파일은 고르지 마세요.
- 같은 내용을 여러 곳이 안내하면 **제도를 직접 운영하는 기관**(발급·접수·심사하는 곳)의 페이지를 앞에 둡니다. \
지자체·경찰서 게시판 FAQ처럼 오래됐을 수 있는 페이지는 운영 기관 페이지가 없을 때만 씁니다.
- 사실은 **페이지에 적힌 내용만** 씁니다. 금액·기한·비율·신청처·필요 서류처럼 독자가 바로 쓸 수 있는 것을 \
우선합니다. 숫자는 페이지에 쓰인 그대로 옮기고, 페이지에 없는 내용을 보태거나 추측하지 않습니다.
- 사실 하나는 한 문장(80자 안팎)으로 짧게 정리합니다. 페이지 문장을 길게 베끼지 않습니다.
- 기준 연도나 시행일이 페이지에 있으면 그 사실에 함께 적습니다.
- 글감과 직접 관련된 페이지가 없으면 빈 배열 [] 을 냅니다. 억지로 채우지 마세요.

## 출력 형식

검색을 마친 뒤 아래 JSON 배열 하나만 출력합니다. 설명이나 코드 펜스는 붙이지 마세요.
[{"title": "페이지 제목", "url": "검색 결과에 나온 주소 그대로", "org": "운영 기관", "facts": ["사실 1", "사실 2"]}]"""

OFFICIAL_USER = """글감: {keyword}
검색 표현: {variants}

이 글감으로 글을 쓸 때 필요한 공식 안내 페이지를 최대 {max_pages}곳 찾아, 페이지마다 사실을 최대 {max_facts}개 뽑아 주세요."""


@dataclass
class OfficialRef:
    """정부·공공기관 안내 페이지 하나와, 그 페이지에서 확인한 사실."""

    title: str
    url: str
    org: str = ""
    facts: list[str] = field(default_factory=list)


def _json_arrays(raw: str) -> list:
    """응답에서 최상위 JSON 배열을 찾아 **마지막 것**을 돌려줍니다 (검색 중간의 설명 문장은 건너뜀).

    읽어 낸 배열 안쪽(`"facts": [...]`)은 다시 보지 않습니다. 안쪽 빈 배열을 결과로 착각하지 않게요.
    """
    decoder = json.JSONDecoder()
    found: list = []
    pos = raw.find("[")
    while pos != -1:
        try:
            data, end = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            pos = raw.find("[", pos + 1)
            continue
        if isinstance(data, list) and all(isinstance(x, dict) for x in data):
            found = data
        pos = raw.find("[", end)
    return found


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _domain_ok(url: str, domains: list[str], exclude: list[str]) -> bool:
    host = _host(url)
    under = lambda d: host == d or host.endswith("." + d)  # noqa: E731
    return any(under(d) for d in domains) and not any(under(d) for d in exclude)


def parse_official(raw: str, seen_urls: list[str], oc: dict) -> list[OfficialRef]:
    """조사 응답을 읽어, **실제 검색 결과에 나온 공식 도메인 주소**만 남깁니다.

    모델이 주소를 줄여 쓰는 일이 있어(쿼리 문자열 생략 등) 경로가 같은 검색 결과 주소로 바꿔 씁니다.
    검색 결과에 없는 주소는 지어낸 것일 수 있으므로 버립니다. 독자에게 거는 링크가 되기 때문입니다.
    """
    domains = [d.lower() for d in oc.get("domains", [])]
    exclude = [d.lower() for d in oc.get("exclude", [])]
    max_pages = int(oc.get("max_pages", 4))
    max_facts = int(oc.get("max_facts", 6))

    by_path: dict[str, str] = {}
    for u in seen_urls:
        by_path.setdefault(u.split("?")[0].split("#")[0].rstrip("/"), u)

    out: list[OfficialRef] = []
    used: set[str] = set()
    for it in _json_arrays(raw):
        url = str(it.get("url", "")).strip()
        if url not in seen_urls:
            url = by_path.get(url.split("?")[0].split("#")[0].rstrip("/"), "")
        facts = [str(f).strip() for f in (it.get("facts") or []) if str(f).strip()]
        if not url or url in used or not facts or not _domain_ok(url, domains, exclude):
            continue
        used.add(url)
        out.append(OfficialRef(
            title=str(it.get("title", "")).strip() or _host(url),
            url=url,
            org=str(it.get("org", "")).strip(),
            facts=facts[:max_facts],
        ))
        if len(out) >= max_pages:
            break
    return out


def official(cfg: dict, candidate: Candidate, *, client=None) -> tuple[list[OfficialRef], float]:
    """글감에 맞는 정부·공공기관 안내 페이지를 웹 검색으로 찾아 사실을 뽑습니다. (자료, 비용 USD)

    Claude 웹 검색 도구를 공식 도메인(config research.official.domains)으로만 좁혀 씁니다.
    보조 자료라서 실패해도 예외를 올리지 않고 빈 목록을 돌려줍니다. 글은 뉴스 자료만으로도 씁니다.
    """
    oc = (cfg.get("research") or {}).get("official") or {}
    if not oc.get("enabled") or not oc.get("domains"):
        return [], 0.0

    variants = ", ".join(dict.fromkeys([candidate.keyword, *candidate.variants]))
    request = dict(
        model=oc.get("model", "claude-sonnet-5-5"),
        max_tokens=int(oc.get("max_tokens", 8000)),
        output_config={"effort": oc.get("effort", "low")},
        system=OFFICIAL_SYSTEM,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": int(oc.get("max_searches", 3)),
            "allowed_domains": list(oc["domains"]),
        }],
    )
    messages: list[dict] = [{"role": "user", "content": OFFICIAL_USER.format(
        keyword=candidate.keyword, variants=variants,
        max_pages=int(oc.get("max_pages", 4)), max_facts=int(oc.get("max_facts", 6)),
    )}]

    client = client or llm.client()
    cost = 0.0
    seen_urls: list[str] = []
    raw = ""
    try:
        # 서버 도구가 길게 돌면 stop_reason 이 pause_turn 으로 끊겨 옵니다. 그대로 이어 붙여 한 번 더 부릅니다.
        for _ in range(3):
            response = client.messages.create(messages=messages, **request)
            usage = response.usage
            price_in, price_out = PRICES.get(request["model"], (2.00, 10.00))
            searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
            cost += (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
            cost += searches * SEARCH_PRICE_USD
            for block in response.content:
                if block.type == "web_search_tool_result" and isinstance(block.content, list):
                    seen_urls.extend(r.url for r in block.content if getattr(r, "url", None))
            raw += llm.text_of(response, "공식 자료 조사", allow_truncated=True)
            if response.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": response.content}]
    except anthropic.APIError as exc:
        log.warning("[%s] 공식 자료 조사 실패 — 뉴스 자료만으로 씁니다: %s", candidate.keyword, exc)
        return [], cost

    pages = parse_official(raw, seen_urls, oc)
    log.info("[%s] 공식 안내 자료 %d곳 (검색 결과 %d건, $%.3f)", candidate.keyword, len(pages), len(seen_urls), cost)
    return pages, cost


def official_block(pages: list[OfficialRef]) -> str:
    """작성·검수 모델에 넘길 공식 안내 자료 블록."""
    if not pages:
        return ""
    lines = [
        "",
        "## 공식 안내 자료 (정부·공공기관 페이지)",
        "",
        "금액·기한·절차 같은 구체적인 값은 이 자료를 우선합니다. 여기 적힌 값은 본문에 그대로 써도 됩니다.",
        "",
    ]
    for i, p in enumerate(pages, start=1):
        lines.append(f"{i}. {p.title}")
        if p.org:
            lines.append(f"   - 기관: {p.org}")
        lines.append(f"   - 링크(걸어도 됨): {p.url}")
        lines.append("   - 페이지에서 확인한 내용:")
        lines.extend(f"     - {f}" for f in p.facts)
        lines.append("")
    return "\n".join(lines)


def contacts_block(contacts: list[str]) -> str:
    """블로그가 안내하는 공식 확인처 (fleet/blogs.yaml contacts). 운영자가 확인해 둔 값입니다.

    작성 모델과 검수 모델에 **같은 자료로** 넘깁니다. 검수관은 자료에 없는 연락처·사이트를
    근거 없는 서술로 보므로, 블로그 성격(persona)이 안내하라고 시킨 곳은 여기 있어야 합니다.
    """
    rows = [f"- {c}" for c in contacts if str(c).strip()]
    if not rows:
        return ""
    return (
        "\n\n## 공식 확인처 (블로그 운영자가 확인한 안내처)\n\n"
        "독자에게 '어디서 확인·신청하는지' 안내할 때 쓸 수 있는 기관·전화·사이트입니다. "
        "링크로 걸지 말고 이름과 주소·번호를 글자로만 적습니다. 여기와 공식 안내 자료에 없는 연락처는 쓰지 않습니다.\n\n"
        + "\n".join(rows)
    )


def internal_links_block(history: list[dict], current_keyword: str = "", limit: int = 12) -> str:
    """같은 블로그에서 이미 공개된 글 목록. 본문에서 내부 링크로 쓸 후보입니다.

    작성 모델과 검수 모델에 **같은 자료로** 넘겨야 합니다. 검수관은 자료에 없는 주소를
    '지어낸 링크'로 보고 거부하므로, 내부 링크 후보가 자료에 없으면 멀쩡한 글이 반려됩니다.
    """
    seen: set[str] = set()
    rows: list[str] = []
    for entry in reversed(history):           # 최근 글부터
        if entry.get("status") != "live":
            continue
        url = (entry.get("url") or "").strip()
        title = (entry.get("title") or "").strip()
        if not url.startswith("http") or not title or url in seen:
            continue
        if current_keyword and entry.get("keyword") == current_keyword:
            continue                          # 지금 쓰는 글 자신은 제외
        seen.add(url)
        rows.append(f"- {title}\n  {url}")
        if len(rows) >= limit:
            break

    if not rows:
        return ""
    return (
        "\n\n## 이 블로그의 다른 글 (내부 링크 후보)\n\n"
        "지금 쓰는 글의 흐름과 정말로 이어지는 자리에만, 최대 2개까지 문장 안에 자연스럽게 겁니다.\n"
        "넣을 곳이 없으면 하나도 넣지 않아도 됩니다. 주소는 아래 것을 그대로 쓰세요.\n\n"
        + "\n".join(rows)
    )


def to_context(
    cfg: dict, candidate: Candidate, refs: list[NewsRef], official_pages: list[OfficialRef] | None = None
) -> str:
    """모델에 넘길 리서치 블록을 만듭니다. 공식 안내 자료는 머리말에 붙여 글자수 제한에 잘리지 않게 합니다."""
    max_chars = cfg["research"]["max_context_chars"]

    if is_planned(candidate):
        # 블로그 주제 안에서 기획한 안내 글입니다. '실시간 인기 키워드'라고 적어 주면 모델이 뉴스 정리로
        # 받아들여 "○○일보 보도에 따르면" 식의 기사 요약 글이 됩니다(2026-09 함대 글에서 확인).
        # 뉴스 검색 '더 읽기' 링크도 붙이지 않습니다. 안내 글에서 독자를 뉴스 검색으로 내보낼 이유가 없습니다.
        lines = [
            f"# 글감: {candidate.keyword}",
            "",
            f"- 검색 표현: {', '.join(candidate.variants) or candidate.keyword}",
        ]
    else:
        lines = [
            f"# 실시간 인기 키워드: {candidate.keyword}",
            "",
            f"- 다른 표현: {', '.join(candidate.variants) or candidate.keyword}",
            f"- 수집된 소스: {', '.join(f'{s}(#{r})' for s, r in candidate.sources.items())}",
            f"- 통합 점수: {candidate.score}",
        ]
        if candidate.headline_hits:
            lines.append("- 관련 주요 헤드라인:")
            lines.extend(f"  - {h}" for h in candidate.headline_hits[:5])

        # '더 읽기' 링크는 프롬프트가 반드시 쓰도록 요구하는 값이라 머리말에 둡니다.
        # 기사 목록 뒤에 두면 글자수 제한에 잘려나가고, 그러면 모델이 없는 주소를
        # 지어낼 위험이 있습니다.
        lines += [
            "",
            "## 독자용 '더 읽기' 링크 (항상 이 주소를 쓸 것)",
            "",
            more_news_url(candidate.keyword),
        ]
    header = "\n".join(lines) + official_block(official_pages or [])

    article_lines = ["", "## 참고 기사", ""]
    for i, r in enumerate(refs, start=1):
        article_lines.append(f"{i}. {r.title}")
        if r.source:
            article_lines.append(f"   - 매체: {r.source}")
        if r.published:
            article_lines.append(f"   - 보도: {r.published}")
        if r.linkable:
            article_lines.append(f"   - 링크(걸어도 됨): {r.url}")
        else:
            article_lines.append("   - 링크 없음 → 텍스트로만 인용할 것")
        if r.snippet:
            article_lines.append(f"   - 요약: {r.snippet}")
        article_lines.append("")

    # 글자수를 넘기면 기사 목록만 줄입니다. 머리말은 그대로 둡니다.
    body = "\n".join(article_lines)
    budget = max_chars - len(header)
    if len(body) > budget:
        body = body[: max(0, budget)] + "\n…(생략)"

    return header + body
