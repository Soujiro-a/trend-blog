"""함대 블로그에 '블로그 소개'·'개인정보처리방침' 페이지를 만들어 줍니다.

애드센스 심사와 OAuth 동의 화면에 필수인 두 페이지를, pages/*.html 템플릿으로 각 블로그에 게시합니다.
이미 같은 제목의 페이지가 있으면 --update 가 없는 한 건너뜁니다.

    python scripts/setup_pages.py --all                 # 함대 전체, 없는 페이지만 생성
    python scripts/setup_pages.py --blog issuecatch1    # 한 블로그
    python scripts/setup_pages.py --all --update        # 있는 페이지도 템플릿으로 갱신
    python scripts/setup_pages.py --all --email me@x.com   # 연락 이메일 지정 (기본: .env 의 CONTACT_EMAIL)
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import net  # noqa: E402
from src.config import ROOT, env, load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402

PAGES = [
    {"title": "블로그 소개", "file": "about.html", "aliases": ["소개", "About"]},
    {"title": "개인정보처리방침", "file": "privacy-policy.html", "aliases": ["개인정보 처리방침", "Privacy Policy"]},
]


def josa(word: str, with_batchim: str, without: str) -> str:
    """받침 유무에 따라 조사를 고릅니다. '블로그은' / '관리을' 같은 어색한 문장을 막습니다.

    한글 음절은 유니코드에서 (코드 - 0xAC00) % 28 == 0 이면 받침이 없습니다.
    한글이 아닌 글자로 끝나면(영문·숫자) 받침 없는 쪽을 씁니다.
    """
    if not word:
        return without
    last = word.rstrip()[-1]
    if "가" <= last <= "힣":
        return with_batchim if (ord(last) - 0xAC00) % 28 else without
    return without


def _template(file: str, blog: fleet_mod.Blog, email: str) -> str:
    """페이지 템플릿에 이 블로그의 주제·독자·하위 축을 채웁니다.

    블로그마다 내용이 달라야 합니다. 7개 블로그가 똑같은 소개 페이지를 쓰면
    그 자체로 '한 사람이 찍어낸 사이트 묶음' 신호가 됩니다.
    """
    html = (ROOT / "pages" / file).read_text(encoding="utf-8")
    html = re.sub(r"<!--.*?-->\s*", "", html, count=1, flags=re.DOTALL)  # 맨 위 사용 설명 주석 제거

    pillars = "\n".join(f"    <li>{p}</li>" for p in blog.pillars) or "    <li>주제 관련 정보</li>"
    audience = blog.audience or "이 주제를 검색해서 들어오는 일반 독자"
    subject = blog.subject or blog.niche or "생활 정보"
    values = {
        "{{BLOG_NAME}}": blog.name,
        "{{BLOG_NAME_은는_조사}}": josa(blog.name, "은", "는"),
        "{{SUBJECT}}": subject,
        "{{SUBJECT_을를_조사}}": josa(subject, "을", "를"),
        "{{AUDIENCE}}": audience.rstrip("."),
        "{{PILLARS}}": pillars,
        "{{EMAIL}}": email,
        "{{DATE}}": datetime.now(KST).strftime("%Y년 %m월 %d일"),
    }
    for token, value in values.items():
        html = html.replace(token, value)

    left = re.findall(r"\{\{[^}]+\}\}", html)
    if left:
        raise ValueError(f"{file}: 채우지 못한 자리표시자 {set(left)}")
    return html.strip()


def _existing_pages(blog_id: str, token: str) -> dict[str, dict]:
    resp = net.get(
        f"{blogger.API_BASE}/blogs/{blog_id}/pages",
        params={"fetchBodies": "false", "view": "ADMIN"},
        headers={"Authorization": f"Bearer {token}"},
    )
    return {p.get("title", "").strip(): p for p in resp.json().get("items", [])}


def setup(blog: fleet_mod.Blog, token: str, email: str, update: bool) -> list[str]:
    out = []
    existing = _existing_pages(blog.blog_id, token)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    for page in PAGES:
        match = next((existing[t] for t in [page["title"], *page["aliases"]] if t in existing), None)
        body = {"kind": "blogger#page", "title": page["title"], "content": _template(page["file"], blog, email)}
        if match and not update:
            out.append(f"{page['title']}: 이미 있음 ({match.get('status')}) — 건너뜀")
            continue
        # Blogger 의 페이지 쓰기 한도는 낮아서(짧은 시간에 몇 번이면 429) 실패하면 기다렸다 다시 시도합니다.
        # 공용 세션의 자동 재시도는 429 를 연타해서 더 막히므로 여기서는 requests 를 직접 씁니다.
        import requests, time
        verb = "갱신" if match else "생성"
        resp = None
        for attempt in range(4):
            try:
                if match:
                    resp = requests.put(f"{blogger.API_BASE}/blogs/{blog.blog_id}/pages/{match['id']}", headers=headers, json=body, timeout=30)
                else:
                    resp = requests.post(f"{blogger.API_BASE}/blogs/{blog.blog_id}/pages/", headers=headers, json=body, timeout=30)
            except requests.RequestException as exc:
                out.append(f"{page['title']}: 요청 오류 {exc}")
                resp = None
                break
            if resp.status_code != 429:
                break
            wait = 30 * (attempt + 1)
            print(f"   {page['title']}: 429 (한도) — {wait}초 뒤 재시도")
            time.sleep(wait)
        if resp is None:
            continue
        if resp.status_code not in (200, 201):
            out.append(f"{page['title']}: {verb} 실패 ({resp.status_code}) {resp.text[:150]}")
            continue
        time.sleep(5)  # 연속 쓰기 사이 간격
        data = resp.json()
        # Blogger 는 새 페이지를 임시저장으로 만들 수 있어 게시 상태를 확인하고 필요하면 공개합니다.
        if data.get("status") != "LIVE":
            pub = net.session().post(f"{blogger.API_BASE}/blogs/{blog.blog_id}/pages/{data['id']}/publish", headers=headers, timeout=30)
            if pub.status_code == 200:
                data = pub.json()
        out.append(f"{page['title']}: {verb} → {data.get('status')} {data.get('url', '')}")
    return out


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="함대 블로그 필수 페이지 생성")
    p.add_argument("--blog", help="블로그 id")
    p.add_argument("--all", action="store_true")
    p.add_argument("--update", action="store_true", help="이미 있는 페이지도 템플릿으로 덮어씀")
    p.add_argument("--email", help="연락 이메일 (기본 .env 의 CONTACT_EMAIL)")
    args = p.parse_args()
    if not args.blog and not args.all:
        p.error("--blog 또는 --all")

    load_dotenv()
    email = args.email or env("CONTACT_EMAIL")
    if not email:
        print("연락 이메일이 필요합니다: --email 또는 .env 의 CONTACT_EMAIL")
        return 1

    fleet = fleet_mod.load_fleet()
    targets = [fleet.get(args.blog)] if args.blog else fleet.blogs
    for blog in targets:
        fleet_mod.apply_env(fleet, blog)   # 이 블로그 계정의 자격증명으로 환경변수 교체
        token = blogger._access_token()    # 캐시는 자격증명별이라 계정이 바뀌면 자동으로 새 토큰
        print(f"== {blog.name} ({blog.id}) ==")
        for line in setup(blog, token, email, args.update):
            print("  ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
