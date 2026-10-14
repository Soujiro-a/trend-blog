"""Google Search Console 조회 (읽기 전용).

주간 보고(scripts/weekly_report.py)·관리 에이전트(src/manager.py)가 따로 갖고 있던 조회 코드를 한곳에 모았고,
글감 기획(src/demand.py)과 사실 갱신(scripts/refresh_facts.py)도 같은 함수를 씁니다.

토큰은 Blogger 와 같은 refresh token 이며 `scripts/get_blogger_token.py --full` 로 받아야
webmasters.readonly 권한이 들어 있습니다. 권한이 없으면 sites() 가 None 을 돌려주고 호출한 쪽은 조용히 건너뜁니다.
GSC 데이터는 약 2일 늦게 집계되므로 조회 끝 날짜는 오늘-2일입니다.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import net
from .state import KST

log = logging.getLogger(__name__)

API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
SITES_API = "https://searchconsole.googleapis.com/webmasters/v3/sites"
LAG_DAYS = 2


def sites(token: str) -> list[str] | None:
    """Search Console 에 등록된 속성 목록. 권한이 없거나 조회에 실패하면 None."""
    try:
        resp = net.session().get(SITES_API, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    return [s.get("siteUrl", "") for s in resp.json().get("siteEntry", [])]


def property_for(site: str, registered: list[str]) -> str | None:
    """블로그 주소에 맞는 GSC 속성 이름. URL 접두어 속성이 없으면 도메인 속성(sc-domain:)을 씁니다."""
    host = urllib.parse.urlparse(site).hostname or ""
    for cand in (site, site.rstrip("/") + "/", f"sc-domain:{host}"):
        if cand in registered:
            return cand
    # www. 유무 차이까지 허용
    for r in registered:
        if r.startswith("sc-domain:") and host.endswith(r.split(":", 1)[1]):
            return r
    return None


def query(token: str, prop: str, start: date, end: date, dimensions: list[str] | None = None, row_limit: int = 10):
    """searchAnalytics.query 원응답(requests.Response). dimensions 기본값은 ["page"]."""
    url = API.format(site=urllib.parse.quote(prop, safe=""))
    return net.session().post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"startDate": start.isoformat(), "endDate": end.isoformat(),
              "dimensions": dimensions or ["page"], "rowLimit": row_limit},
        timeout=30,
    )


def window(days: int, now: datetime | None = None) -> tuple[date, date]:
    end = (now or datetime.now(KST)).date() - timedelta(days=LAG_DAYS)
    return end - timedelta(days=days), end


def rows(token: str, site: str, days: int, dimensions: list[str], row_limit: int = 1000) -> list[dict] | None:
    """블로그 주소로 속성을 찾아 조회한 행 목록. 권한 없음·미등록·오류면 None, 데이터가 없으면 []."""
    registered = sites(token)
    if not registered:
        return None
    prop = property_for(site, registered)
    if not prop:
        return None
    start, end = window(days)
    try:
        resp = query(token, prop, start, end, dimensions, row_limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("Search Console 조회 실패 (%s): %s", site, exc)
        return None
    if resp.status_code != 200:
        log.warning("Search Console 조회 실패 (%s, %s)", site, resp.status_code)
        return None
    return list(resp.json().get("rows", []))


def clicks(token: str, site: str, days: int) -> int | None:
    got = rows(token, site, days, ["page"])
    return None if got is None else int(sum(r.get("clicks", 0) for r in got))


def top_pages(token: str, site: str, days: int = 28) -> list[tuple[str, int, int]]:
    """(주소, 클릭, 노출) 를 클릭 많은 순으로. 조회할 수 없으면 빈 목록."""
    got = rows(token, site, days, ["page"]) or []
    out = [(r["keys"][0], int(r.get("clicks", 0)), int(r.get("impressions", 0))) for r in got if r.get("keys")]
    return sorted(out, key=lambda x: (-x[1], -x[2]))


@dataclass
class Opportunity:
    """노출은 되는데 클릭이나 순위가 낮은 검색어 — 새 글이나 보강으로 더 가져올 수 있는 수요."""

    query: str
    impressions: int
    clicks: int
    position: float

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0


def opportunities(
    token: str, site: str, days: int = 28, *, min_impressions: int = 10, min_position: float = 8.0,
    max_ctr: float = 0.02, limit: int = 15,
) -> list[Opportunity]:
    """노출 ≥ min_impressions 이면서 (평균 순위가 min_position 보다 뒤 **또는** CTR 이 max_ctr 이하)인 검색어.

    노출 많은 순. 순위가 앞이고 클릭도 잘 나오는 검색어는 이미 잘 되는 것이라 뺍니다.
    조회할 수 없으면 빈 목록 — 호출한 쪽은 검색 수요 없이 진행합니다.
    """
    got = rows(token, site, days, ["query"]) or []
    out = []
    for r in got:
        if not r.get("keys"):
            continue
        o = Opportunity(query=str(r["keys"][0]), impressions=int(r.get("impressions", 0)),
                        clicks=int(r.get("clicks", 0)), position=float(r.get("position", 0.0)))
        if o.impressions < min_impressions:
            continue
        if o.position > min_position or o.ctr <= max_ctr:
            out.append(o)
    out.sort(key=lambda o: (-o.impressions, o.position))
    return out[:limit]


def select_opportunities(raw_rows: list[dict], **kw) -> list[Opportunity]:
    """opportunities() 의 걸러내기 부분만 (테스트용, 네트워크 없음)."""
    min_impressions = kw.get("min_impressions", 10)
    min_position = kw.get("min_position", 8.0)
    max_ctr = kw.get("max_ctr", 0.02)
    limit = kw.get("limit", 15)
    out = []
    for r in raw_rows:
        o = Opportunity(query=str(r["keys"][0]), impressions=int(r.get("impressions", 0)),
                        clicks=int(r.get("clicks", 0)), position=float(r.get("position", 0.0)))
        if o.impressions >= min_impressions and (o.position > min_position or o.ctr <= max_ctr):
            out.append(o)
    out.sort(key=lambda o: (-o.impressions, o.position))
    return out[:limit]
