"""구글 Search Console '색인 생성 요청' 대기열 — 예약 작업(Claude 데스크톱 앱)이 씁니다.

색인 요청은 API 가 없어(URL 검사 API 는 읽기 전용) Search Console 화면에서 버튼을 눌러야 합니다.
이 스크립트는 **무엇을 누를지**만 정합니다. 누르는 일은 앱의 예약 작업이 브라우저 창에서 합니다.

대기열 순서: 블로그 첫 화면 → 공개 글(오래된 순). 쌓인 글이 먼저 끝나고, 그 뒤로는 새로 올라온 글이
자연히 다음 차례가 됩니다. 이미 색인된 주소(URL 검사 API 로 확인)와 최근 30일 안에 요청한 주소는 뺍니다.
요청 한도는 구글 계정 전체에 하루 약 10건입니다(2026-09-29 확인).

**요청은 모바일 주소(…?m=1)로 합니다.** 구글은 주로 휴대폰 크롤러로 긁는데, Blogger 는 휴대폰에 원래 주소를
?m=1 로 302 리디렉션합니다. 원래 주소로 요청하면 크롤이 '리디렉션 오류'로 끝나 요청 한 건이 버려졌습니다
(2026-09-30 확인: 요청한 글 16건 모두 REDIRECT_ERROR). ?m=1 페이지는 바로 200 이고 원래 주소를 canonical 로
가리키므로, 구글이 그 관계를 처리하면 원래 주소가 색인됩니다. 색인 여부는 두 주소 중 하나라도 등록됐는지로 봅니다.

    python scripts/index_queue.py next [--limit 10]     # 지금 요청할 주소 (JSON 한 줄씩)
    python scripts/index_queue.py mark <URL> requested  # 요청 완료 기록 (requested | quota | failed)
    python scripts/index_queue.py status                # 블로그별 색인·요청 현황

기록은 out/gsc/index_requests.json (깃에 올리지 않는 로컬 파일, 이 PC 에서만 요청하므로).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import net  # noqa: E402
from src.config import OUT_DIR, load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402

LOG_PATH = OUT_DIR / "gsc" / "index_requests.json"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
REREQUEST_DAYS = 30


def load_log() -> list[dict]:
    try:
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_log(entries: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def recently_requested(entries: list[dict], now: datetime) -> set[str]:
    cutoff = now - timedelta(days=REREQUEST_DAYS)
    return {
        e["url"] for e in entries
        if e.get("result") == "requested" and datetime.fromisoformat(e["at"]) >= cutoff
    }


def _headers() -> dict:
    return {"Authorization": f"Bearer {blogger._access_token()}"}


def _live_urls(blog_id: str) -> list[tuple[str, str]]:
    """(공개 시각, 주소) 목록."""
    out, page = [], None
    while True:
        params = {"status": "live", "maxResults": 100, "fetchBodies": "false", "view": "ADMIN"}
        if page:
            params["pageToken"] = page
        payload = net.get(f"{blogger.API_BASE}/blogs/{blog_id}/posts", params=params, headers=_headers()).json()
        out.extend((p["published"], p["url"]) for p in payload.get("items", []))
        page = payload.get("nextPageToken")
        if not page:
            return out


def mobile_url(url: str) -> str:
    """Blogger 모바일 주소. 휴대폰 크롤러가 리디렉션 없이 바로 받는 주소입니다."""
    return url + ("&" if "?" in url else "?") + "m=1"


def inspect(url: str, site: str) -> dict:
    """URL 검사 결과 {verdict, coverage, crawled, fetch}. 실패하면 빈 dict."""
    try:
        resp = net.once().post(
            INSPECT_URL, headers=_headers(), timeout=60,
            json={"inspectionUrl": url, "siteUrl": site, "languageCode": "ko"},
        )
    except Exception:  # noqa: BLE001 — 한 주소의 검사 실패로 전체를 멈추지 않습니다
        return {}
    if resp.status_code != 200:
        return {}
    r = resp.json().get("inspectionResult", {}).get("indexStatusResult", {})
    return {"verdict": r.get("verdict", ""), "coverage": r.get("coverageState", ""),
            "crawled": r.get("lastCrawlTime", ""), "fetch": r.get("pageFetchState", "")}


def is_indexed(item: dict) -> tuple[bool, dict]:
    """(원래 주소나 모바일 주소가 색인됐는지, 모바일 주소 검사 결과)."""
    if inspect(item["url"], item["site"]).get("verdict") == "PASS":
        return True, {}
    m = inspect(item["request_url"], item["site"])
    return m.get("verdict") == "PASS", m


def queue(fleet: fleet_mod.Fleet) -> list[dict]:
    """요청 후보 전체 (첫 화면 먼저, 그다음 글은 오래된 순). 색인 여부는 아직 보지 않습니다."""
    homes, posts = [], []
    st = fleet_mod.load_account_state()
    for b in fleet.blogs:
        if not b.enabled or fleet_mod.account_halted(b.account, st):
            continue
        fleet_mod.apply_env(fleet, b)
        site = f"sc-domain:{b.id}.blogspot.com"
        home = f"https://{b.id}.blogspot.com/"
        homes.append({"blog": b.id, "url": home, "request_url": mobile_url(home), "site": site, "published": ""})
        posts.extend({"blog": b.id, "url": u, "request_url": mobile_url(u), "site": site, "published": t}
                     for t, u in _live_urls(b.blog_id))
    posts.sort(key=lambda x: datetime.fromisoformat(x["published"]))
    return homes + posts


def cmd_next(args, fleet) -> int:
    now = datetime.now(KST)
    done = recently_requested(load_log(), now)
    picked = []
    for item in queue(fleet):
        # 원래 주소로 요청했던 기록은 세지 않습니다 — 그 요청은 리디렉션 오류로 끝났습니다.
        if item["request_url"] in done:
            continue
        fleet_mod.apply_env(fleet, fleet.get(item["blog"]))
        ok, m = is_indexed(item)
        if ok:
            continue
        if m.get("fetch") == "SUCCESSFUL":     # 모바일 주소를 이미 잘 긁어 감 — 구글이 처리 중이라 요청할 필요 없음
            continue
        # 예약 작업은 url 을 입력창에 넣고 mark 합니다. 그래서 url 을 모바일 주소로 바꿔 내보냅니다.
        picked.append({**item, "url": item["request_url"], "canonical": item["url"], "coverage": m.get("coverage", "")})
        if len(picked) >= args.limit:
            break
    for p in picked:
        print(json.dumps(p, ensure_ascii=False))
    if not picked:
        print("# 요청할 주소가 없습니다 (모두 색인됐거나 최근 30일 안에 요청함).")
    return 0


def cmd_mark(args, fleet) -> int:
    entries = load_log()
    entries.append({"at": datetime.now(KST).isoformat(timespec="seconds"), "url": args.url, "result": args.result})
    save_log(entries)
    print(f"기록: {args.result} {args.url}")
    return 0


def cmd_status(args, fleet) -> int:
    now = datetime.now(KST)
    done = recently_requested(load_log(), now)
    rows: dict[str, list[int]] = {}
    for item in queue(fleet):
        fleet_mod.apply_env(fleet, fleet.get(item["blog"]))
        ok, m = is_indexed(item)
        r = rows.setdefault(item["blog"], [0, 0, 0, 0])   # 전체, 색인됨, 요청함·처리 중(미색인), 대기
        r[0] += 1
        if ok:
            r[1] += 1
        elif item["request_url"] in done or m.get("fetch") == "SUCCESSFUL":
            r[2] += 1
        else:
            r[3] += 1
    print("블로그          전체  색인됨  요청·처리중  대기")
    for blog, (total, indexed, requested, waiting) in rows.items():
        print(f"{blog:14} {total:5} {indexed:7} {requested:12} {waiting:5}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("next")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("mark")
    p.add_argument("url")
    p.add_argument("result", choices=["requested", "quota", "failed"])
    sub.add_parser("status")
    args = ap.parse_args(argv)

    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    fleet = fleet_mod.load_fleet()
    return {"next": cmd_next, "mark": cmd_mark, "status": cmd_status}[args.cmd](args, fleet)


if __name__ == "__main__":
    raise SystemExit(main())
