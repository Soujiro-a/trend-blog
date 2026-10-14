"""글감 기획에 넣는 실제 검색 수요 (src/planner.py 가 씁니다).

기획 모델은 그동안 주제·하위 축·이미 쓴 제목만 보고 "검색될 만한" 글감을 추측했습니다(2026-10-14 개선점 1).
여기서 두 가지 실제 신호를 보탭니다. 둘 다 없어도 기획은 그대로 돌아갑니다(빈 목록).

1) Search Console: 내 블로그가 구글에 **노출은 되는데 순위·클릭이 낮은 검색어**. 그 검색어에 바로 답하는 글을
   쓰면 이미 노출되는 자리에서 클릭을 가져옵니다. 데이터가 쌓이기 전(블로그당 주 노출 수백 회 미만)에는 비어 있습니다.
2) 네이버 자동완성: 글감의 검색 표현(search)이 자동완성에 뜨는지. 뜨면 사람들이 실제로 치는 말이라는 뜻이라
   그 글감을 앞에 둡니다. 비공식 주소라 실패하면 조용히 넘어갑니다.
"""

from __future__ import annotations

import logging
import urllib.parse

from . import gsc, net
from .publishers import blogger

log = logging.getLogger(__name__)

NAVER_AC = ("https://ac.search.naver.com/nx/ac?q={q}&con=1&frm=nv&ans=2&r_format=json&r_enc=UTF-8"
            "&r_unicode=0&t_koreng=1&run=2&rev=4&q_enc=UTF-8&st=100")


def search_console(blog, cfg: dict) -> list[gsc.Opportunity]:
    """이 블로그의 '노출은 있는데 클릭·순위가 낮은' 검색어. 권한·데이터가 없으면 빈 목록."""
    sd = (cfg.get("planner") or {}).get("search_demand") or {}
    if not sd.get("enabled", True):
        return []
    try:
        token = blogger._access_token()
        return gsc.opportunities(
            token, blog.home_url, int(sd.get("days", 28)),
            min_impressions=int(sd.get("min_impressions", 10)), min_position=float(sd.get("min_position", 8)),
            max_ctr=float(sd.get("max_ctr", 0.02)), limit=int(sd.get("max_queries", 15)),
        )
    except Exception as exc:  # noqa: BLE001  — 보조 신호라 기획을 멈추지 않습니다
        log.info("[%s] Search Console 검색 수요 조회 건너뜀: %s", blog.id, exc)
        return []


def demand_block(opps: list[gsc.Opportunity]) -> str:
    """기획 요청문에 넣을 검색 수요 목록. 없으면 '(아직 없음)'."""
    if not opps:
        return "(아직 없음 — 주제와 독자를 보고 고르세요)"
    return "\n".join(
        f"- {o.query} (노출 {o.impressions}회, 클릭 {o.clicks}회, 평균 순위 {o.position:.0f}위)" for o in opps
    )


def naver_suggestions(keyword: str, timeout: float = 5.0) -> list[str] | None:
    """네이버 자동완성 제안어. 제안이 없으면 [], 조회에 실패하면 None (비공식 주소)."""
    if not keyword.strip():
        return []
    try:
        resp = net.session().get(NAVER_AC.format(q=urllib.parse.quote(keyword.strip())), timeout=timeout,
                                 headers={"Referer": "https://www.naver.com/"})
        if resp.status_code != 200:
            return None
        return parse_naver(resp.json())
    except Exception:  # noqa: BLE001
        return None


def parse_naver(payload: dict) -> list[str]:
    """자동완성 응답 {"items": [[["연말정산 의료비공제", "0"], ...]]} 에서 제안어만."""
    out = []
    for group in payload.get("items", []) or []:
        for item in group or []:
            if item and isinstance(item, list) and item[0]:
                out.append(str(item[0]))
    return out


def matches(keyword: str, suggestions: list[str]) -> bool:
    """제안어 가운데 그 말을 (띄어쓰기 무시하고) 담은 것이 있는가."""
    k = keyword.replace(" ", "")
    return bool(k) and any(k in s.replace(" ", "") for s in suggestions)


def has_naver_demand(keyword: str) -> bool | None:
    """자동완성에 그 말이 있으면 True, 없으면 False, 조회 실패면 None."""
    got = naver_suggestions(keyword)
    return None if got is None else matches(keyword, got)
