"""새 블로그의 검색 엔진 등록이 빠짐없이 됐는지 확인합니다 — docs/new-blog-checklist.md 의 마지막 확인 단계.

블로그 페이지와 Search Console API 로 확인할 수 있는 것만 봅니다(읽기 전용, 아무것도 바꾸지 않음).
네이버·다음·Bing 에 사이트맵을 냈는지는 각 사이트에 로그인해야 보여서 여기서 확인하지 못합니다.
그 셋은 체크리스트에 표시하며 진행하세요.

    python scripts/registration_check.py              # 켜진 블로그 전부
    python scripts/registration_check.py --blog gaganam1
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import net  # noqa: E402
from src.config import load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402

GSC = "https://searchconsole.googleapis.com/webmasters/v3/sites"


def _gsc(path: str, token: str) -> dict | None:
    resp = net.session().get(f"{GSC}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=20)
    return resp.json() if resp.status_code == 200 else None


def check(blog, token: str, gsc_sites: list[str] | None) -> list[tuple[bool, str]]:
    home_url = blog.home_url
    home = net.session().get(home_url, timeout=20).text
    robots = net.session().get(home_url + "robots.txt", timeout=20).text
    rows: list[tuple[bool, str]] = []

    # Search Console — 속성과 사이트맵
    prop = next((p for p in (f"sc-domain:{blog.domain}", home_url) if gsc_sites and p in gsc_sites), None)
    if gsc_sites is None:
        rows.append((False, "Search Console 조회 실패 (토큰에 webmasters 권한이 있는지 확인)"))
    elif not prop:
        rows.append((False, "Search Console 속성 없음"))
    else:
        sm = (_gsc(f"/{urllib.parse.quote(prop, safe='')}/sitemaps", token) or {}).get("sitemap", [])
        main = next((s for s in sm if s.get("path", "").endswith("/sitemap.xml")), None)
        if main:
            got = sum(int(c.get("submitted", 0)) for c in main.get("contents", []))
            rows.append((True, f"Search Console 사이트맵 제출됨 (읽음 {main.get('lastDownloaded', '-')[:10]}, 주소 {got}개)"))
        else:
            rows.append((False, f"Search Console 속성({prop})은 있으나 sitemap.xml 미제출"))

    # 네이버 — 테마 <head> 의 소유확인 태그
    rows.append(("naver-site-verification" in home, "네이버 소유확인 메타태그 (테마 <head>)"))
    # 다음 — 맞춤 robots.txt 의 인증 줄. 기본 규칙이 같이 남아 있어야 합니다.
    rows.append(("#DaumWebMasterTool:" in robots, "다음 인증 줄 (맞춤 robots.txt)"))
    rows.append(("Sitemap:" in robots and "Disallow: /search" in robots, "robots.txt 기본 규칙·Sitemap 줄 유지"))
    # 블로그 첫 화면 검색 설명 (설정 → 메타 태그)
    m = re.search(r"<meta\s+content='([^']*)'\s+name='description'", home)
    rows.append((bool(m and m.group(1).strip()), "첫 화면 검색 설명 (설정 → 메타 태그 → 검색 설명)"))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--blog")
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    fleet = fleet_mod.load_fleet()

    sites_by_account: dict[str, list[str] | None] = {}
    missing = 0
    for b in fleet.blogs:
        if (args.blog and b.id != args.blog) or (not args.blog and not b.enabled):
            continue
        fleet_mod.apply_env(fleet, b)
        token = blogger._access_token()
        if b.account not in sites_by_account:
            data = _gsc("", token)
            sites_by_account[b.account] = [s.get("siteUrl", "") for s in data.get("siteEntry", [])] if data else None
        rows = check(b, token, sites_by_account[b.account])
        bad = sum(not ok for ok, _ in rows)
        missing += bad
        print(f"\n## {b.id} ({b.account}) — {len(rows) - bad}/{len(rows)}")
        for ok, text in rows:
            print(f"  {'✅' if ok else '❌'} {text}")
    print("\n네이버·다음·Bing 사이트맵 제출은 로그인해야 보여서 여기서 확인하지 않습니다 (docs/new-blog-checklist.md).")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
