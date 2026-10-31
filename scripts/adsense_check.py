"""애드센스 심사 준비 점검 — 블로그마다 심사에서 걸릴 만한 것을 찾아 목록으로 보여줍니다.

    python scripts/adsense_check.py              # 켜진 블로그 전부
    python scripts/adsense_check.py --blog gaganam1
    python scripts/adsense_check.py --no-index    # 색인 비율(URL 검사 API) 빼고 빠르게

항목은 애드센스 거절 메시지 분류대로 묶습니다: [콘텐츠부족] [가치부족] [정책] [탐색] [연결] [언어].
트래픽 출처·중복 계정처럼 코드로 볼 수 없는 사유는 '사람이 할 일' 끝에 적습니다.

API 로 확인할 수 있는 것(글 수, 페이지, 설명, 라벨)과 방문자가 보는 화면(메뉴 링크, 시간대,
소개→개인정보처리방침 링크)을 함께 봅니다. Blogger 설정 화면에서만 바꿀 수 있는 항목은
'사람이 할 일' 로 따로 모아 어디를 누르면 되는지 적습니다(Blogger API 로는 설정을 못 바꿉니다).

기준은 구글이 공개한 숫자가 아니라 흔히 통과하는 블로그들의 공통점입니다. 특히 글 수는
'이만큼이면 통과'가 아니라 '이보다 적으면 콘텐츠 부족으로 거절되기 쉽다'는 뜻입니다.
"""

from __future__ import annotations

import argparse
import html as html_mod
import re
import sys
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import net, state  # noqa: E402
from src.config import load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402

MIN_POSTS = 25          # 이보다 적으면 '콘텐츠 부족'으로 거절되기 쉽습니다
MIN_DAYS = 28           # 첫 글 뒤 최소 이만큼은 운영한 뒤 신청
KOREA_OFFSET = "+09:00"


def _public(url: str) -> str:
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": net.USER_AGENT})
        return r.text if r.status_code == 200 else ""
    except requests.RequestException:
        return ""


# ---------------------------------------------------------------- 거절 사유별 판단 (네트워크 없이 시험 가능)
# 애드센스 거절 메시지의 공식 분류(support.google.com/adsense/answer/81904, 12176698, 정책 10502938)마다
# 미리 볼 수 있는 신호를 둡니다. 2026-10-29 gaganam1 이 구조 점검 9/9 를 통과하고도 '가치가 별로 없는 콘텐츠'로
# 거절돼(색인 0/42, 같은 제도 쪼개기) 콘텐츠 신호를 더했습니다. 숫자는 구글 공개 기준이 아니라 경험적 경계입니다.

MIN_AVG_CHARS = 1500      # 본문 평균 글자 수 (공백 제외)
SHORT_CHARS = 1000        # 이보다 짧은 글은 '콘텐츠 부족'으로 보이기 쉽습니다
MIN_INDEX_RATIO = 0.5     # 색인 비율. gaganam1 거절 당시 0%
MAX_CLUSTER = 3           # 제목이 거의 같은 글 묶음의 최대 편수 (같은 제도를 상황만 바꿔 쪼갠 신호)
CLUSTER_SIMILAR = 0.5     # 묶음 판단 유사도. 0.6 은 쪼갠 글을 놓쳤고, 핵심어 비율은 블로그 주제어(환불·국민연금)에 묻혀 못 씀(10-31 실측)
SIMILAR = 0.6             # 함대 블로그 사이 겹침 — config dedupe.similarity_threshold 와 같은 값
MIN_SOURCED = 0.8         # '참고한 자료'(공식 출처) 가 있는 글 비율
MAX_AFFILIATE = 0.2       # 제휴 링크가 있는 글 비율 ('제휴 위주' 품질 문제)
MIN_HANGUL = 0.5          # 본문 글자 중 한글 비율 (지원 언어)
INDEX_SAMPLE = 20         # 색인 검사할 글 수 (공개 7일 넘은 글 중 최근 순)


def text_of(content: str) -> str:
    """HTML → 공백 정리한 글자."""
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", content or "", flags=re.S | re.I)
    t = html_mod.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()


def char_count(content: str) -> int:
    return len(re.sub(r"\s", "", text_of(content)))


def hangul_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum("가" <= c <= "힣" for c in letters) / len(letters) if letters else 0.0


def broken_internal(posts: list[dict], host: str) -> list[tuple[str, str]]:
    """공개 글 안의 같은 블로그 글 링크 중 공개 목록에 없는 것(임시저장·삭제 → 방문자에게 404). [(글, 깨진 링크)]"""
    live = {urllib.parse.urlparse(p["url"]).path for p in posts}
    out = []
    for p in posts:
        for link in re.findall(r'href="(https?://[^"]+)"', p.get("content", "")):
            u = urllib.parse.urlparse(link)
            if u.netloc.lower() == host and re.fullmatch(r"/\d{4}/\d{2}/[^/]+\.html", u.path) and u.path not in live:
                out.append((p["url"], link))
    return out


def biggest_cluster(titles: list[str], threshold: float = CLUSTER_SIMILAR) -> list[str]:
    """제목이 서로 비슷한 글끼리 이은 묶음 중 가장 큰 것 (한 편뿐이면 빈 목록)."""
    from src.trends.base import similarity

    parent = list(range(len(titles)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            if similarity(titles[i], titles[j]) >= threshold:
                parent[root(i)] = root(j)
    groups = Counter(root(i) for i in range(len(titles)))
    if not groups:
        return []
    top, size = groups.most_common(1)[0]
    return [titles[i] for i in range(len(titles)) if root(i) == top] if size > 1 else []


def cross_blog_overlap(titles: list[str], others: list[str], threshold: float = SIMILAR) -> list[tuple[str, str]]:
    """다른 함대 블로그 글과 제목이 겹치는 글. 한 운영자의 여러 사이트가 같은 글을 내면 스팸 정책(대량 생성)에 걸립니다."""
    from src.trends.base import similarity

    return [(t, o) for t in titles for o in others if similarity(t, o) >= threshold][:20]


def robots_blocks(robots: str) -> bool:
    """robots.txt 가 일반 크롤러나 애드센스 크롤러(Mediapartners-Google)에게 사이트 전체를 막는지."""
    agent, blocked = "", False
    for line in robots.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            agent = v.lower()
        elif k == "disallow" and v == "/" and agent in ("*", "mediapartners-google"):
            blocked = True
    return blocked


def verdict(res: list[tuple[bool, str]]) -> str:
    bad = [t for ok, t in res if not ok]
    if not bad:
        return "✅ 신청해도 됩니다"
    kinds = list(dict.fromkeys(b.split("]", 1)[0].lstrip("[") for b in bad))
    return f"⏸️ 신청 보류 — {len(bad)}개 항목 ({', '.join(kinds)})"


def _all_posts(base: str, h: dict) -> list[dict]:
    out, page = [], None
    while True:
        params = {"status": "live", "maxResults": 100, "fetchBodies": "true", "view": "ADMIN"}
        if page:
            params["pageToken"] = page
        data = net.get(f"{base}/posts", headers=h, params=params).json()
        out += data.get("items", [])
        page = data.get("nextPageToken")
        if not page:
            return out


def _index_ratio(blog: fleet_mod.Blog, posts: list[dict], now: datetime) -> tuple[int, int]:
    from scripts import index_queue as iq

    old = sorted((p for p in posts if now - datetime.fromisoformat(p["published"]) > timedelta(days=7)),
                 key=lambda p: p["published"], reverse=True)[:INDEX_SAMPLE]
    site = f"sc-domain:{blog.domain}"
    hit = sum(iq.inspect(p["url"], site).get("verdict") == "PASS"
              or iq.inspect(iq.mobile_url(p["url"]), site).get("verdict") == "PASS" for p in old)
    return hit, len(old)


def check(fleet: fleet_mod.Fleet, blog: fleet_mod.Blog, index: bool = True) -> tuple[list[tuple[bool, str]], list[str]]:
    """(점검 결과 [(통과?, '[분류] 설명')], 사람이 할 일 [..]). 분류는 애드센스 거절 메시지 이름입니다."""
    fleet_mod.apply_env(fleet, blog)
    h = {"Authorization": f"Bearer {blogger._access_token()}"}
    base = f"{blogger.API_BASE}/blogs/{blog.blog_id}"
    info = net.get(base, headers=h).json()
    posts = _all_posts(base, h)
    pages = net.get(f"{base}/pages", headers=h, params={"fetchBodies": "false", "view": "ADMIN"}).json().get("items", [])
    url = info.get("url", "").rstrip("/")
    host = urllib.parse.urlparse(url).netloc.lower()
    home = _public(url + "/")
    now = datetime.now(KST)

    res: list[tuple[bool, str]] = []
    todo: list[str] = []

    # 1) 콘텐츠 부족 (Insufficient content) — 글 수·기간·분량
    n = len(posts)
    age = fleet_mod.blog_age_days(fleet, blog)
    res.append((n >= MIN_POSTS, f"[콘텐츠부족] 공개 글 {n}개 (신청 권장 {MIN_POSTS}개 이상)"))
    res.append((age >= MIN_DAYS, f"[콘텐츠부족] 운영 {age}일째 (신청 권장 {MIN_DAYS}일 이상)"))
    sizes = [char_count(p.get("content", "")) for p in posts]
    avg = sum(sizes) // len(sizes) if sizes else 0
    short = [p["url"] for p, s in zip(posts, sizes) if s < SHORT_CHARS]
    res.append((avg >= MIN_AVG_CHARS and not short,
                f"[콘텐츠부족] 본문 평균 {avg:,}자, {SHORT_CHARS:,}자 미만 {len(short)}편" + (f" (예: {short[0]})" if short else "")))

    # 2) 가치가 별로 없는 콘텐츠 (Content quality) — 색인 비율·쪼개기·함대 안 겹침·정리 대기·출처·제휴
    if index:
        hit, total = _index_ratio(blog, posts, now)
        ratio = hit / total if total else 0.0
        res.append((total > 0 and ratio >= MIN_INDEX_RATIO,
                    f"[가치부족] 색인 {hit}/{total} ({ratio:.0%}, 공개 7일 넘은 최근 글, 권장 {MIN_INDEX_RATIO:.0%} 이상)"))
    titles = [p["title"] for p in posts]
    cluster = biggest_cluster(titles)
    res.append((len(cluster) <= MAX_CLUSTER,
                f"[가치부족] 제목이 거의 같은 가장 큰 묶음 {len(cluster)}편 (권장 {MAX_CLUSTER}편 이하)"
                + (f": {cluster[0][:30]} …" if cluster else "")))
    others = [x.get("title", "") for b in fleet.blogs if b.id != blog.id
              for x in state.load(b.history_path) if x.get("status") == "live" and x.get("title")]
    overlap = cross_blog_overlap(titles, others)
    res.append((not overlap, f"[가치부족] 다른 함대 블로그와 제목 겹침 {len(overlap)}건"
                + (f" (예: {overlap[0][0][:25]} ↔ {overlap[0][1][:25]})" if overlap else "")))
    from scripts import rewrite_post as rw

    q = rw.load_queue()
    done = {rw.key_of(it["url"]) for it in q if it.get("done_at")}
    queued = [it for it in q if rw.host_of(it["url"]) == host and it["action"] in ("rewrite", "revert")
              and not it.get("done_at") and not it.get("failed_at")]
    failed = [it for it in q if rw.host_of(it["url"]) == host and it.get("failed_at") and rw.key_of(it["url"]) not in done
              and not any(rw.key_of(o["url"]) == rw.key_of(it["url"]) for o in queued)]
    res.append((not queued and not failed, f"[가치부족] 정리 대기열 남은 {len(queued)}건, 실패 뒤 미해결 {len(failed)}건"))
    sourced = sum("참고한 자료" in p.get("content", "") for p in posts)
    res.append((not n or sourced / n >= MIN_SOURCED, f"[가치부족] 출처(참고한 자료) 있는 글 {sourced}/{n}"))
    affiliate = [p for p in posts if re.search(r"coupang\.com|link\.coupang|ali\.ski|smartstore\.naver", p.get("content", ""))]
    res.append((not n or len(affiliate) / n <= MAX_AFFILIATE, f"[가치부족] 제휴 링크 있는 글 {len(affiliate)}/{n}"))

    # 3) 정책 위반·허위 표시 (Content policy / Misrepresentation) — 운영자 소개·연락처·개인정보처리방침
    live_pages = {p["title"]: p for p in pages if p.get("status") == "LIVE"}
    about = next((live_pages[t] for t in ("블로그 소개", "소개", "About") if t in live_pages), None)
    privacy = next((live_pages[t] for t in ("개인정보처리방침", "개인정보 처리방침", "Privacy Policy") if t in live_pages), None)
    res.append((bool(about), "[정책] 블로그 소개 페이지"))
    res.append((bool(privacy), "[정책] 개인정보처리방침 페이지"))
    about_html = _public(about["url"]) if about else ""
    privacy_html = _public(privacy["url"]) if privacy else ""
    if about and privacy:
        path = re.sub(r"^https?://[^/]+", "", privacy["url"])
        res.append((path in about_html, "[정책] 소개 페이지 → 개인정보처리방침 링크"))
        res.append(("mailto:" in about_html or re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text_of(about_html)) is not None,
                    "[정책] 소개 페이지에 연락처(이메일)"))
        res.append(("쿠키" in privacy_html and "Google" in privacy_html,
                    "[정책] 개인정보처리방침에 Google 광고 쿠키 고지 (애드센스 필수 고지)"))
    if not (about or privacy):
        todo.append("페이지 만들기: python scripts/setup_pages.py --blog " + blog.id)
    if affiliate:
        no_disclosure = [p["url"] for p in affiliate if "수수료" not in p.get("content", "")]
        res.append((not no_disclosure, f"[정책] 제휴 링크 글의 대가 표시 누락 {len(no_disclosure)}편"))

    # 4) 사이트 탐색 문제 (Site navigation) — 메뉴·깨진 내부 링크·라벨·설명·시간대
    in_menu = [p for p in (about, privacy) if p and re.sub(r"^https?://[^/]+", "", p["url"]) in home]
    ok_menu = bool(about and privacy) and len(in_menu) == 2
    res.append((ok_menu, "[탐색] 첫 화면에서 소개·개인정보처리방침으로 가는 메뉴"))
    if not ok_menu:
        todo.append(
            "메뉴에 페이지 넣기: Blogger → 레이아웃 → (상단 또는 사이드바) 가젯 추가 → '페이지' → "
            "'블로그 소개'·'개인정보처리방침' 체크 → 저장"
        )
    broken = broken_internal(posts, host)
    res.append((not broken, f"[탐색] 공개 글 속 깨진 내부 링크 {len(broken)}개"
                + (f" (예: {broken[0][0]} → {broken[0][1]})" if broken else "")))
    counts = Counter(label for p in posts for label in (p.get("labels") or []))
    singles = sum(1 for c in counts.values() if c == 1)
    tidy = not counts or (len(counts) <= max(8, n // 2) and singles <= max(3, len(counts) // 3))
    res.append((tidy, f"[탐색] 라벨 {len(counts)}개 (글 1개짜리 {singles}개)"))
    if not tidy:
        todo.append(f"라벨 정리: python scripts/adsense_check.py --blog {blog.id} --fix-labels")
    desc = (info.get("description") or "").strip()
    res.append((bool(desc), "[탐색] 블로그 설명" + (f": {desc[:40]}" if desc else " 없음 (검색 결과·공유 미리보기가 비어 보입니다)")))
    if not desc:
        todo.append(f"블로그 설명: Blogger → 설정 → 기본 → 설명 → \"{_suggest_description(blog)}\"")
    offsets = {p["published"][-6:] for p in posts if p.get("published")}
    tz_ok = not offsets or offsets == {KOREA_OFFSET}
    res.append((tz_ok, "[탐색] 시간대 서울" + ("" if tz_ok else f" 아님 ({', '.join(sorted(offsets))}) — 글 날짜가 하루씩 어긋나 보입니다")))
    if not tz_ok:
        todo.append("시간대: Blogger → 설정 → 서식 → 시간대 → '(GMT+09:00) 서울'")
    if home and "ContactForm" not in home:
        todo.append("(선택) 문의 양식: 레이아웃 → 사이드바 가젯 추가 → '문의 양식' — 소개 페이지 이메일과 함께 연락 경로가 둘이 됩니다")

    # 5) 사이트 연결 불가 (Site unreachable) — 첫 화면·robots.txt·noindex·https
    res.append((bool(home) and "blogin.g" not in home, "[연결] 첫 화면이 바로 열림 (비공개·콘텐츠 경고 화면 아님)"))
    res.append((not robots_blocks(_public(url + "/robots.txt")), "[연결] robots.txt 가 크롤러를 막지 않음"))
    noindex = re.search(r'<meta[^>]+name=["\']robots["\'][^>]+noindex', home, re.I) is not None
    res.append((not noindex, "[연결] 첫 화면에 noindex 없음"))
    if noindex:
        todo.append("noindex 끄기: Blogger → 설정 → 크롤러 및 색인 생성 → 맞춤 robots 헤더 태그 확인")
    try:
        r = requests.get(url.replace("https://", "http://", 1) + "/", timeout=30, allow_redirects=False,
                         headers={"User-Agent": net.USER_AGENT})
        https_ok = r.status_code in (301, 302, 307, 308) and r.headers.get("location", "").startswith("https://")
    except requests.RequestException:
        https_ok = False
    res.append((https_ok, "[연결] http → https 리디렉션"))
    if not https_ok:
        todo.append("HTTPS: Blogger → 설정 → HTTPS 리디렉션 켜기")

    # 6) 지원하지 않는 언어
    ko = hangul_ratio(" ".join(text_of(p.get("content", "")) for p in posts[:10]))
    res.append((ko >= MIN_HANGUL, f"[언어] 본문 한글 비율 {ko:.0%}"))

    # 코드로 볼 수 없는 거절 사유 — 신청 전에 사람이 확인
    todo.append("트래픽 출처: 유료 클릭·트래픽 교환·자동 방문 프로그램을 쓰지 않기 (거절 사유 '트래픽 소스 문제')")
    todo.append("중복 계정: 새 애드센스 계정을 만들지 말고 승인 계정(가가남) 하나로만 신청 (사람당 1개, 소유지로 계정 애드센스는 닫힘)")
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
    p.add_argument("--no-index", action="store_true", help="색인 비율 검사(URL 검사 API, 블로그당 1~2분)를 건너뜀")
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
        res, todo = check(fleet, b, index=not args.no_index)
        passed = sum(ok for ok, _ in res)
        print(f"\n## {b.name} ({b.id}) — {passed}/{len(res)} · {verdict(res)}")
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
