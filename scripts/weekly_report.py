"""주간 운영·수익 보고서.

사람이 주 1회 5분 동안 보던 것(GitHub Actions, 사용량, Search Console, 애드센스)을
한 장의 마크다운으로 모아 줍니다. GitHub Actions 가 매주 이 결과로 이슈를 하나 만들어
운영자는 메일 한 통만 읽으면 됩니다.

    python scripts/weekly_report.py            # 화면 출력
    python scripts/weekly_report.py --out x.md # 파일로 저장

Search Console / 애드센스 수치는 Blogger 토큰에 해당 권한이 있을 때만 나옵니다
(scripts/get_blogger_token.py 를 --full 옵션으로 다시 실행하면 권한이 추가됩니다).
권한이 없으면 그 항목만 '권한 없음'으로 표시하고 나머지는 정상 출력합니다.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

import requests
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import net, state  # noqa: E402
from src.config import env, load_config  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402

GSC_API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
ADSENSE_API = "https://adsense.googleapis.com/v2"

# 이 값들을 넘으면 보고서 맨 위에 경고를 띄웁니다.
ALERT_WEEKLY_COST_USD = 6.0     # 글 4개 × 7일 × $0.15 ≈ $4.2 가 정상 범위
ALERT_MIN_LIVE_PER_WEEK = 5     # 이보다 적게 공개됐으면 필터/검수가 너무 빡빡하거나 실행이 멈춘 것
ALERT_REJECT_RATIO = 0.5        # 검수 거부·보류 비율이 절반을 넘으면 작성 프롬프트나 소스를 봐야 함


def _blog_url(token: str) -> str:
    blog_id = env("BLOGGER_BLOG_ID", required=True)
    resp = net.get(f"{blogger.API_BASE}/blogs/{blog_id}", headers={"Authorization": f"Bearer {token}"})
    return resp.json().get("url", "")


def section_history(history: list[dict], days: int) -> tuple[list[str], list[str]]:
    lines = ["## 이번 주 작성", ""]
    alerts: list[str] = []
    recent = state.recent(history, days)
    if not recent:
        lines.append("- 이번 주 작성 이력이 없습니다. **예약 실행이 멈췄을 수 있습니다** (Actions 탭 확인).")
        alerts.append("작성 이력 없음 — 예약 실행 중단 의심")
        return lines, alerts

    by_status = Counter(h.get("status", "?") for h in recent)
    by_mode = Counter(h.get("mode", "trend") for h in recent)
    cost = sum(float(h.get("cost_usd") or 0) for h in recent)
    scores = [h["review_score"] for h in recent if h.get("review_score") is not None]
    extras = Counter(e for h in recent for e in (h.get("extras") or []))

    lines.append(f"| 항목 | 값 |")
    lines.append(f"|---|---|")
    lines.append(f"| 작성 | {len(recent)}건 (실시간 {by_mode.get('trend', 0)} · 장수 {by_mode.get('evergreen', 0)}) |")
    lines.append(f"| 공개 | {by_status.get('live', 0)}건 |")
    lines.append(f"| 임시저장(보류) | {by_status.get('draft', 0)}건 |")
    lines.append(f"| 검수 거부 | {by_status.get('rejected', 0)}건 |")
    if scores:
        lines.append(f"| 검수 평균 점수 | {sum(scores) / len(scores):.0f}점 |")
    if extras:
        lines.append(f"| 제휴 링크 삽입 | {', '.join(f'{k} {v}건' for k, v in extras.items())} |")
    lines.append(f"| Claude API 비용 | 약 ${cost:.2f} |")
    lines.append("")

    live = by_status.get("live", 0)
    not_live = by_status.get("draft", 0) + by_status.get("rejected", 0)
    if cost > ALERT_WEEKLY_COST_USD:
        alerts.append(f"API 비용 ${cost:.2f} — 예상 범위(${ALERT_WEEKLY_COST_USD}) 초과")
    if live < ALERT_MIN_LIVE_PER_WEEK:
        alerts.append(f"공개 {live}건 — 주 {ALERT_MIN_LIVE_PER_WEEK}건 미만. 필터·검수 기준이나 실행 상태 확인")
    if len(recent) and not_live / len(recent) > ALERT_REJECT_RATIO:
        alerts.append(f"보류·거부 비율 {not_live / len(recent):.0%} — 작성 품질 또는 소재 문제")

    published = [h for h in recent if h.get("status") == "live"]
    if published:
        lines.append("### 공개된 글")
        for h in published:
            url = h.get("url") or ""
            lines.append(f"- [{h.get('title')}]({url})" if url else f"- {h.get('title')}")
    held = [h for h in recent if h.get("status") == "draft"]
    if held:
        lines.append("")
        lines.append("### 보류된 글 (임시저장함에 있음 · 안 봐도 됨)")
        for h in held:
            score = h.get("review_score")
            lines.append(f"- {h.get('title')}" + (f" (검수 {score}점)" if score is not None else ""))
    lines.append("")
    return lines, alerts


def section_blogger(token: str) -> list[str]:
    lines = ["## 블로그 현황", ""]
    try:
        live = blogger.list_posts("live", max_results=500)
        draft = blogger.list_posts("draft", max_results=500)
    except Exception as exc:
        return lines + [f"- Blogger 조회 실패: {exc}", ""]
    lines.append(f"- 공개 글 {len(live)}개 · 임시저장 {len(draft)}개")
    if len(live) < 20:
        lines.append(f"- 애드센스 신청은 공개 글 20~30개부터 권장. 현재 {len(live)}개 → 약 {max(0, (20 - len(live)) // 3 + 1)}일 뒤")
    if len(draft) > 15:
        lines.append(f"- 임시저장이 {len(draft)}개 쌓였습니다. Blogger 에서 한 번에 지워도 됩니다(공개에 영향 없음).")
    lines.append("")
    return lines


def _gsc_sites(token: str) -> list[str] | None:
    """Search Console 에 등록된 속성 목록. 권한이 없으면 None."""
    try:
        resp = net.session().get(
            "https://searchconsole.googleapis.com/webmasters/v3/sites",
            headers={"Authorization": f"Bearer {token}"}, timeout=20,
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    return [s.get("siteUrl", "") for s in resp.json().get("siteEntry", [])]


def _gsc_property(site: str, registered: list[str]) -> str | None:
    """블로그 주소에 맞는 GSC 속성 이름. URL 접두어 속성이 없으면 도메인 속성(sc-domain:)을 씁니다."""
    import urllib.parse
    host = urllib.parse.urlparse(site).hostname or ""
    for cand in (site, site.rstrip("/") + "/", f"sc-domain:{host}"):
        if cand in registered:
            return cand
    # www. 유무 차이까지 허용
    for r in registered:
        if r.startswith("sc-domain:") and host.endswith(r.split(":", 1)[1]):
            return r
    return None


def _gsc_query(token: str, prop: str, start, end, row_limit: int = 10):
    import urllib.parse
    url = GSC_API.format(site=urllib.parse.quote(prop, safe=""))
    return net.session().post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["page"], "rowLimit": row_limit},
        timeout=30,
    )


def section_search_console(token: str, site: str, days: int, heading: str = "## 검색 유입 (Search Console)") -> list[str]:
    lines = [heading, ""]
    if not site:
        return lines + ["- 블로그 주소를 알 수 없어 건너뜀", ""]
    end = datetime.now(KST).date() - timedelta(days=2)   # GSC 는 2일 지연
    start = end - timedelta(days=days)

    registered = _gsc_sites(token)
    if registered is None:
        return lines + [
            "- 토큰에 Search Console 권한이 없습니다.",
            "  `python scripts/get_blogger_token.py --full` 로 토큰을 다시 받아 `BLOGGER_REFRESH_TOKEN` Secret 을 교체하세요.",
            "",
        ]
    prop = _gsc_property(site, registered)
    if prop is None:
        return lines + [
            f"- 권한은 있지만 이 블로그({site})가 Search Console 에 등록돼 있지 않습니다.",
            f"  등록된 속성: {', '.join(registered) if registered else '없음'}",
            "  → https://search.google.com/search-console 에서 **속성 추가** 에 블로그 주소를 넣으세요. "
            "Blogger 블로그는 같은 구글 계정이면 소유권이 자동 확인됩니다.",
            "",
        ]
    try:
        resp = _gsc_query(token, prop, start, end)
    except Exception as exc:
        return lines + [f"- 조회 실패: {exc}", ""]
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            detail = resp.text[:200]
        return lines + [f"- 조회 실패 ({resp.status_code}, 속성 {prop}): {detail}", ""]

    rows = resp.json().get("rows", [])
    if not rows:
        return lines + [f"- 속성 {prop} · {start}~{end} 유입 데이터 없음 (색인 초기에는 정상입니다)", ""]
    clicks = sum(r["clicks"] for r in rows)
    imps = sum(r["impressions"] for r in rows)
    lines.append(f"- 속성 {prop} · {start} ~ {end}: 클릭 {clicks:.0f} · 노출 {imps:.0f} (상위 10개 페이지 합계)")
    lines.append("")
    lines.append("| 클릭 | 노출 | 페이지 |")
    lines.append("|---:|---:|---|")
    base = site.rstrip("/")
    for r in sorted(rows, key=lambda r: -r["clicks"])[:10]:
        page = r["keys"][0]
        short = page.replace(base, "").replace("https://", "") or "/"
        lines.append(f"| {r['clicks']:.0f} | {r['impressions']:.0f} | {short} |")
    lines.append("")
    return lines


def section_adsense(token: str, days: int, heading: str = "## 광고 수익 (애드센스)") -> list[str]:
    lines = [heading, ""]
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = net.session().get(f"{ADSENSE_API}/accounts", headers=headers, timeout=30)
    except Exception as exc:
        return lines + [f"- 조회 실패: {exc}", ""]
    if resp.status_code == 403:
        return lines + [
            "- 권한 없음 또는 애드센스 미승인. 승인 뒤 `python scripts/get_blogger_token.py --full` 로 토큰을 다시 받으세요.",
            "",
        ]
    if resp.status_code != 200:
        return lines + [f"- 조회 실패 ({resp.status_code}): {resp.text[:200]}", ""]
    accounts = resp.json().get("accounts", [])
    if not accounts:
        return lines + ["- 애드센스 계정이 없습니다 (아직 신청 전이면 정상).", ""]

    account = accounts[0]["name"]  # accounts/pub-xxxx
    state = accounts[0].get("state", "?")
    pending = accounts[0].get("pendingTasks") or []
    lines.append(f"- 계정 {account.split('/')[-1]} · 상태 **{state}**" + (f" · 처리 필요: {', '.join(pending)}" if pending else ""))
    # 어떤 사이트가 승인됐는지 (READY / NEEDS_ATTENTION / REQUIRES_REVIEW / GETTING_READY)
    try:
        sites_resp = net.session().get(f"{ADSENSE_API}/{account}/sites", headers=headers, timeout=30)
        if sites_resp.status_code == 200:
            for s in sites_resp.json().get("sites", []):
                lines.append(f"  - {s.get('domain')}: {s.get('state')}")
    except Exception:
        pass
    end = datetime.now(KST).date()
    start = end - timedelta(days=days)
    params = [
        ("metrics", "ESTIMATED_EARNINGS"), ("metrics", "PAGE_VIEWS"), ("metrics", "CLICKS"),
        ("startDate.year", start.year), ("startDate.month", start.month), ("startDate.day", start.day),
        ("endDate.year", end.year), ("endDate.month", end.month), ("endDate.day", end.day),
    ]
    resp = net.session().get(f"{ADSENSE_API}/{account}/reports:generate", params=params, headers=headers, timeout=30)
    if resp.status_code != 200:
        return lines + [f"- 보고서 조회 실패 ({resp.status_code}): {resp.text[:200]}", ""]
    data = resp.json()
    totals = (data.get("totals") or {}).get("cells", [])
    vals = [c.get("value", "0") for c in totals]
    currency = data.get("headers", [{}])[0].get("currencyCode", "")
    if len(vals) >= 3:
        lines.append(f"- 최근 {days}일: 예상 수익 **{vals[0]} {currency}** · 페이지뷰 {vals[1]} · 광고 클릭 {vals[2]}")
    else:
        lines.append(f"- 최근 {days}일 수익 데이터 없음 (광고가 아직 안 나가고 있으면 정상입니다)")
    lines.append("")
    return lines


# 같은 운영자의 다른 블로그. 이쪽은 PC 의 예약 작업이 돌리므로, 공개 글 수를 밖에서
# 세어 "PC 쪽 파이프라인이 멈췄는지"를 이 보고서가 대신 알려줍니다.
WP_SITES = [
    {"name": "withsmartcontent.com (WordPress, PC 예약 작업)", "url": "https://withsmartcontent.com", "min_per_week": 4},
]


def section_wordpress(days: int) -> tuple[list[str], list[str]]:
    lines = ["## 다른 블로그 현황", ""]
    alerts: list[str] = []
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    for site in WP_SITES:
        # 공용 세션의 자동 재시도를 타지 않고 한 번만 요청합니다. WordPress 쪽 보안 플러그인이
        # 짧은 시간에 여러 번 부르면 429 를 돌려주는데, 재시도 어댑터가 그걸 연타하면 더 막힙니다.
        posts = None
        for attempt in range(2):
            try:
                resp = requests.get(
                    f"{site['url']}/wp-json/wp/v2/posts",
                    params={"after": since, "per_page": 50, "_fields": "id,date,title,link"},
                    headers={"User-Agent": "trend-blog-weekly-report/1.0", "Accept": "application/json"},
                    timeout=30,
                )
                if resp.status_code == 200:
                    posts = resp.json()
                    break
                err = f"HTTP {resp.status_code}"
            except Exception as exc:
                err = str(exc)[:120]
            if attempt == 0:
                time.sleep(20)
        if posts is None:
            lines.append(f"- **{site['name']}** — 조회 실패 ({err}). 사이트가 잠시 막았을 수 있습니다. 다음 주에도 실패하면 확인하세요.")
            continue
        lines.append(f"- **{site['name']}** — 최근 {days}일 공개 {len(posts)}건")
        for p in posts[:10]:
            title = p.get("title", {}).get("rendered", "").replace("&#8211;", "–")
            lines.append(f"  - [{title}]({p.get('link')}) ({p.get('date', '')[:10]})")
        if len(posts) < site["min_per_week"]:
            alerts.append(
                f"{site['name']} 공개 {len(posts)}건 — 주 {site['min_per_week']}건 미만. "
                "PC 예약 작업(MoneyBlogDailyContent)과 logs/last-status.json 확인"
            )
    lines.append("")
    return lines, alerts


def build(days: int = 7) -> str:
    cfg = load_config()
    now = datetime.now(KST)
    try:
        history = state.load()
    except state.HistoryCorrupted:
        history = []

    body: list[str] = []
    wp_lines, wp_alerts = section_wordpress(days)

    site = ""
    alerts: list[str] = []
    alerts += wp_alerts

    # 함대(fleet/blogs.yaml)가 있으면 블로그별로, 없으면 루트 이력 한 덩어리로.
    try:
        from src import fleet as fleet_mod
        fleet = fleet_mod.load_fleet()
        blogs = fleet.blogs
    except Exception as exc:  # noqa: BLE001
        fleet, blogs = None, []
        body.append(f"> 함대 설정을 읽지 못해 단일 블로그로 보고합니다: {exc}")

    if blogs:
        # 계정마다 자격증명이 다릅니다. 블로그별로 환경변수를 바꿔 가며 토큰을 받습니다
        # (토큰 캐시는 자격증명별이라 계정이 섞여도 서로의 토큰을 쓰지 않습니다).
        cred = fleet_mod.credential_report(fleet)
        for acc, missing in cred.items():
            if missing:
                alerts.append(f"계정 '{acc}' 자격증명 없음: {', '.join(missing)}")
        token = ""  # 아래 단일 블로그용 섹션을 건너뛰게 하는 표시
        hist_lines: list[str] = []
        hist_lines += ["## 함대 슬롯표", "", fleet_mod.schedule_table(fleet), ""]
        for b in sorted(blogs, key=lambda b: (b.account, b.slot_minutes)):
            try:
                blog_history = state.load(b.history_path)
            except state.HistoryCorrupted as exc:
                hist_lines += [f"## {b.name} ({b.id}) — ⚠️ 이력 손상", "", f"- {exc}", ""]
                alerts.append(f"[{b.id}] 이력 파일 손상 — 실행이 멈춰 있습니다")
                continue
            bl, ba = section_history(blog_history, days)
            bl[0] = f"## {b.name} ({b.id}, 계정 {b.account}, 슬롯 {', '.join(b.slots)}) — 이번 주 작성"
            hist_lines += bl
            alerts += [f"[{b.id}] {a}" for a in ba]
            if cred.get(b.account):
                continue
            try:
                fleet_mod.apply_env(fleet, b)
                blog_token = blogger._access_token()
                url = net.get(
                    f"{blogger.API_BASE}/blogs/{b.blog_id}",
                    headers={"Authorization": f"Bearer {blog_token}"},
                ).json().get("url", "")
            except Exception as exc:  # noqa: BLE001
                hist_lines += [f"- Blogger 조회 실패: {str(exc)[:100]}", ""]
                continue
            if url:
                hist_lines += section_search_console(blog_token, url, days, heading=f"### 검색 유입 — {url}")
    else:
        try:
            token = blogger._access_token()
        except Exception as exc:  # noqa: BLE001
            token = ""
            alerts.append(f"Blogger 인증 실패 — {exc}")
        hist_lines, alerts_single = section_history(history, days)
        alerts += alerts_single
        if token:
            try:
                site = _blog_url(token)
            except Exception:
                site = ""

    head = [f"# 주간 보고 — {now.strftime('%Y-%m-%d')} (최근 {days}일)", ""]
    if alerts:
        head.append("> ⚠️ **확인이 필요한 항목**")
        head += [f"> - {a}" for a in alerts]
        head.append("")
    else:
        head.append("> ✅ 이상 없음. 이번 주는 할 일이 없습니다.")
        head.append("")

    body += hist_lines
    if token and not blogs:
        body += section_blogger(token)
        body += section_search_console(token, site, days)
        body += section_adsense(token, days)
    body += wp_lines
    # 애드센스는 계정 단위입니다. 함대라면 계정마다 한 번씩 봅니다.
    if blogs:
        for acc in fleet.used_accounts:
            if fleet_mod.credential_report(fleet).get(acc):
                continue
            sample = fleet.blogs_of(acc)
            if not sample:
                continue
            try:
                fleet_mod.apply_env(fleet, sample[0])
                acc_token = blogger._access_token()
            except Exception:  # noqa: BLE001
                continue
            body += section_adsense(acc_token, days, heading=f"## 광고 수익 — 계정 {acc}")
            for wp in WP_SITES:
                body += section_search_console(acc_token, wp["url"], days, heading=f"### 검색 유입 — {wp['url']}")
            break   # WordPress 사이트는 계정과 무관하므로 한 번만

    body += [
        "## 설정 요약",
        "",
        f"- 블로그 {len(blogs) or 1}개 · 블로그당 하루 작성 {cfg['run']['posts_per_run']}건 · 공개 상한 {cfg['publish'].get('max_live_per_run')}건 · "
        f"검수 기준 {cfg['review'].get('min_score')}점 · 작성 모델 {cfg['writer']['model']}",
        f"- 장수 글 주 {cfg['evergreen']['posts_per_run']}건 · 쿠팡 제휴 {'켜짐' if cfg['monetize']['coupang'].get('enabled') else '꺼짐'}"
        + ("" if env("COUPANG_ACCESS_KEY") else " (키 없음 → 비활성)"),
        "",
    ]
    return "\n".join(head + body)


def main() -> int:
    parser = argparse.ArgumentParser(description="주간 운영·수익 보고서")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", help="저장할 파일 경로")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    text = build(args.days)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"저장: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
