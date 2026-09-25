"""애드센스 심사 준비 점검 — 블로그마다 심사에서 걸릴 만한 것을 찾아 목록으로 보여줍니다.

    python scripts/adsense_check.py              # 켜진 블로그 전부
    python scripts/adsense_check.py --blog gaganam1

API 로 확인할 수 있는 것(글 수, 페이지, 설명, 라벨)과 방문자가 보는 화면(메뉴 링크, 시간대,
소개→개인정보처리방침 링크)을 함께 봅니다. Blogger 설정 화면에서만 바꿀 수 있는 항목은
'사람이 할 일' 로 따로 모아 어디를 누르면 되는지 적습니다(Blogger API 로는 설정을 못 바꿉니다).

기준은 구글이 공개한 숫자가 아니라 흔히 통과하는 블로그들의 공통점입니다. 특히 글 수는
'이만큼이면 통과'가 아니라 '이보다 적으면 콘텐츠 부족으로 거절되기 쉽다'는 뜻입니다.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import net  # noqa: E402
from src.config import load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402

MIN_POSTS = 25          # 이보다 적으면 '콘텐츠 부족'으로 거절되기 쉽습니다
MIN_DAYS = 28           # 첫 글 뒤 최소 이만큼은 운영한 뒤 신청
KOREA_OFFSET = "+09:00"


def _public(url: str) -> str:
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": net.USER_AGENT})
        return r.text if r.status_code == 200 else ""
    except requests.RequestException:
        return ""


def check(fleet: fleet_mod.Fleet, blog: fleet_mod.Blog) -> tuple[list[tuple[bool, str]], list[str]]:
    """(점검 결과 [(통과?, 설명)], 사람이 할 일 [..])"""
    fleet_mod.apply_env(fleet, blog)
    h = {"Authorization": f"Bearer {blogger._access_token()}"}
    base = f"{blogger.API_BASE}/blogs/{blog.blog_id}"
    info = net.get(base, headers=h).json()
    posts = net.get(f"{base}/posts", headers=h, params={
        "status": "live", "maxResults": 500, "fetchBodies": "false", "view": "ADMIN",
    }).json().get("items", [])
    pages = net.get(f"{base}/pages", headers=h, params={"fetchBodies": "false", "view": "ADMIN"}).json().get("items", [])
    url = info.get("url", "").rstrip("/")
    home = _public(url + "/")

    res: list[tuple[bool, str]] = []
    todo: list[str] = []

    # 1) 콘텐츠 양과 운영 기간
    n = len(posts)
    age = fleet_mod.blog_age_days(fleet, blog)
    res.append((n >= MIN_POSTS, f"공개 글 {n}개 (신청 권장 {MIN_POSTS}개 이상)"))
    res.append((age >= MIN_DAYS, f"운영 {age}일째 (신청 권장 {MIN_DAYS}일 이상)"))

    # 2) 필수 페이지와 연결
    live_pages = {p["title"]: p for p in pages if p.get("status") == "LIVE"}
    about = next((live_pages[t] for t in ("블로그 소개", "소개", "About") if t in live_pages), None)
    privacy = next((live_pages[t] for t in ("개인정보처리방침", "개인정보 처리방침", "Privacy Policy") if t in live_pages), None)
    res.append((bool(about), "블로그 소개 페이지"))
    res.append((bool(privacy), "개인정보처리방침 페이지"))
    if about and privacy:
        about_html = _public(about["url"])
        path = re.sub(r"^https?://[^/]+", "", privacy["url"])
        res.append((path in about_html, "소개 페이지 → 개인정보처리방침 링크"))
    if not (about or privacy):
        todo.append("페이지 만들기: python scripts/setup_pages.py --blog " + blog.id)

    in_menu = [p for p in (about, privacy) if p and re.sub(r"^https?://[^/]+", "", p["url"]) in home]
    ok_menu = bool(about and privacy) and len(in_menu) == 2
    res.append((ok_menu, "첫 화면에서 소개·개인정보처리방침으로 가는 메뉴"))
    if not ok_menu:
        todo.append(
            "메뉴에 페이지 넣기: Blogger → 레이아웃 → (상단 또는 사이드바) 가젯 추가 → '페이지' → "
            "'블로그 소개'·'개인정보처리방침' 체크 → 저장"
        )

    # 3) 블로그 설정 (API 로 못 바꾸는 것들)
    desc = (info.get("description") or "").strip()
    res.append((bool(desc), "블로그 설명" + (f": {desc[:40]}" if desc else " 없음 (검색 결과·공유 미리보기가 비어 보입니다)")))
    if not desc:
        todo.append(f"블로그 설명: Blogger → 설정 → 기본 → 설명 → \"{_suggest_description(blog)}\"")

    offsets = {p["published"][-6:] for p in posts if p.get("published")}
    tz_ok = not offsets or offsets == {KOREA_OFFSET}
    res.append((tz_ok, "시간대 서울" + ("" if tz_ok else f" 아님 ({', '.join(sorted(offsets))}) — 글 날짜가 하루씩 어긋나 보입니다")))
    if not tz_ok:
        todo.append("시간대: Blogger → 설정 → 서식 → 시간대 → '(GMT+09:00) 서울'")

    if home and "ContactForm" not in home:
        todo.append("(선택) 문의 양식: 레이아웃 → 사이드바 가젯 추가 → '문의 양식' — 소개 페이지 이메일과 함께 연락 경로가 둘이 됩니다")

    # 4) 라벨 — 글 하나짜리 라벨이 잔뜩이면 분류가 엉성해 보입니다
    counts = Counter(label for p in posts for label in (p.get("labels") or []))
    singles = sum(1 for c in counts.values() if c == 1)
    tidy = not counts or (len(counts) <= max(8, n // 2) and singles <= max(3, len(counts) // 3))
    res.append((tidy, f"라벨 {len(counts)}개 (글 1개짜리 {singles}개)"))
    if not tidy:
        todo.append(f"라벨 정리: python scripts/adsense_check.py --blog {blog.id} --fix-labels")

    return res, todo


def _suggest_description(blog: fleet_mod.Blog) -> str:
    pillars = ", ".join(p.split("과 ")[0].split("와 ")[0] for p in blog.pillars[:3])
    return f"{blog.subject}에 대해 {pillars} 등을 순서대로 정리하는 블로그입니다."


def fix_labels(fleet: fleet_mod.Fleet, blog: fleet_mod.Blog, apply: bool, overrides: dict[str, str] | None = None) -> None:
    """이미 올라간 글의 라벨을 '블로그 고정 라벨 + 하위 축 하나'로 바꿉니다.

    하위 축은 제목·기존 라벨과 가장 가까운 pillar 로 고릅니다. 틀리면 --set "제목 앞부분=하위 축" 으로 고칩니다.
    --apply 없이는 바꿀 내용만 보여줍니다.
    쓰기 요청이라 글 사이에 20초씩 쉽니다(짧은 시간에 수정이 몰리지 않게).
    """
    import time

    from src.planner import match_pillar
    from src.trends.base import similarity

    fleet_mod.apply_env(fleet, blog)
    h = {"Authorization": f"Bearer {blogger._access_token()}"}
    base = f"{blogger.API_BASE}/blogs/{blog.blog_id}"
    posts = net.get(f"{base}/posts", headers=h, params={
        "status": "live", "maxResults": 500, "fetchBodies": "false", "view": "ADMIN",
    }).json().get("items", [])
    for i, p in enumerate(posts):
        old = p.get("labels") or []
        hay = " ".join([p["title"], *old])
        pillar = max(blog.pillars, key=lambda x: similarity(x, hay)) if blog.pillars else ""
        pillar = match_pillar(pillar, blog.pillars)
        for prefix, forced in (overrides or {}).items():
            if p["title"].startswith(prefix):
                pillar = match_pillar(forced, blog.pillars) or pillar
        new = list(dict.fromkeys([*blog.labels, *([pillar] if pillar else [])]))
        if set(new) == set(old):
            continue
        print(f"- {p['title'][:50]}\n    {old}\n  → {new}")
        if apply:
            if i:
                time.sleep(20)
            r = net.once().patch(f"{base}/posts/{p['id']}", headers=h, json={"labels": new}, timeout=30)
            print("    ", "✅ 변경" if r.status_code == 200 else f"❌ {r.status_code} {r.text[:120]}")
            if r.status_code == 403:
                fleet_mod.halt_account(blog.account, f"라벨 수정 403: {r.text[:200]}")
                return


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="애드센스 심사 준비 점검")
    p.add_argument("--blog")
    p.add_argument("--fix-labels", action="store_true", help="라벨을 고정 라벨 + 하위 축으로 정리 (미리보기)")
    p.add_argument("--apply", action="store_true", help="--fix-labels 를 실제로 적용")
    p.add_argument("--set", action="append", default=[], metavar="제목앞부분=하위축",
                   help="--fix-labels 가 고른 하위 축이 틀린 글을 바로잡습니다 (여러 번 가능)")
    args = p.parse_args()

    load_dotenv()
    fleet = fleet_mod.load_fleet()
    targets = [fleet.get(args.blog)] if args.blog else [b for b in fleet.blogs if b.enabled]

    if args.fix_labels:
        for b in targets:
            print(f"== {b.name} ({b.id}) ==")
            overrides = dict(x.split("=", 1) for x in args.set if "=" in x)
            fix_labels(fleet, b, args.apply, overrides)
        return 0

    all_todo: dict[str, list[str]] = {}
    for b in targets:
        res, todo = check(fleet, b)
        passed = sum(ok for ok, _ in res)
        print(f"\n## {b.name} ({b.id}) — {passed}/{len(res)}")
        for ok, text in res:
            print(f"  {'✅' if ok else '❌'} {text}")
        if todo:
            all_todo[b.name] = todo
    if all_todo:
        print("\n## 사람이 할 일 (Blogger 설정 화면)")
        for name, todo in all_todo.items():
            print(f"\n{name}")
            for t in todo:
                print(f"  - {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
