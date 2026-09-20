"""Blogger 계정 교체 — 로직은 그대로 두고 대상 계정만 바꿉니다.

계정이 정책 위반으로 제한되면 이의신청을 기다리는 대신 새 계정으로 옮기는 편이 빠릅니다.
이 스크립트는 **코드가 할 수 있는 부분 전부**를 자동으로 처리합니다.
사람이 해야 하는 것은 구글 계정 만들기와 OAuth 승인 두 가지뿐입니다.

    python scripts/switch_account.py --check      # 지금 토큰이 어느 계정인지, 어떤 블로그가 보이는지
    python scripts/switch_account.py --plan       # 무엇을 바꿀지 미리 보기 (아무것도 안 바꿈)
    python scripts/switch_account.py --apply      # 실제 교체

--apply 가 하는 일
  1. 현재 fleet/blogs.yaml 과 data/blogs, data/fleet 를 타임스탬프 폴더로 보관(archive)
  2. 새 계정의 블로그 목록을 조회해 blogs.yaml 을 새로 씀 (슬롯·주제 템플릿 포함)
  3. 블로그별 이력을 빈 상태로 초기화 (새 블로그이므로 과거 이력이 의미 없음)
  4. 주제(subject)는 이전 설정에서 이름 순서대로 물려받고, 모자라면 비워 둠

주의: 이 스크립트는 **.env 의 토큰이 이미 새 계정 것**이라는 전제로 동작합니다.
토큰 교체가 먼저입니다. 순서는 --plan 출력에 안내됩니다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import net  # noqa: E402
from src.config import ROOT, env, load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402

ARCHIVE_ROOT = ROOT / "data" / "archive"

STEPS = """
계정 교체 순서 (사람이 하는 부분은 1·2·4번뿐입니다)

 1. 새 구글 계정으로 blogger.com 에서 블로그를 만듭니다.
    - 기존 계정과 **다른 계정**이어야 합니다. 제한은 계정에 붙습니다.
    - 처음에는 2~3개만 만드세요. 5개를 같은 날 만들어 동시에 글을 올린 것이 이번 차단의 한 원인입니다.

 2. 새 계정으로 Google Cloud 프로젝트를 새로 만들고 OAuth 를 설정합니다.
    - console.cloud.google.com → 새 프로젝트
    - API 라이브러리에서 'Blogger API v3' 사용 설정
      (주간 보고에 유입·수익을 표시하려면 'Google Search Console API', 'AdSense Management API' 도)
    - Google 인증 플랫폼 → 브랜딩 채우고 **앱 게시(프로덕션)** ← 안 하면 토큰이 7일 뒤 만료됩니다
    - 사용자 인증 정보 → OAuth 클라이언트 ID → **데스크톱 앱**
    - ⚠️ 기존 계정의 Cloud 프로젝트를 재사용하지 마세요. 제한된 계정과 엮입니다.

 3. 새 클라이언트 정보를 .env 에 넣습니다 (값은 화면에 찍히지 않습니다):
       BLOGGER_CLIENT_ID / BLOGGER_CLIENT_SECRET 를 새 값으로 교체

 4. 토큰을 발급받습니다. 브라우저가 열리면 **새 계정으로** 승인하세요:
       python scripts/get_blogger_token.py --full --from-env --write-env

 5. 여기서부터 자동입니다:
       python scripts/switch_account.py --check     # 새 계정이 맞는지 확인
       python scripts/switch_account.py --apply     # 함대 설정·이력 교체
       python scripts/setup_pages.py --all          # 소개·개인정보처리방침 페이지 생성

 6. GitHub Secrets 를 같은 값으로 갱신합니다 (Actions 가 쓰는 값):
       BLOGGER_CLIENT_ID / BLOGGER_CLIENT_SECRET / BLOGGER_REFRESH_TOKEN
       (.env 값을 클립보드로 복사: python scripts/copy_secret.py BLOGGER_REFRESH_TOKEN)

 7. blogs.yaml 에서 블로그별 subject(고유 주제)를 확인하고, 워크플로를 다시 켭니다:
       gh workflow enable fleet.yml && gh workflow enable manager.yml
"""


def _account_info() -> tuple[str, list[dict]]:
    """토큰이 가리키는 계정 표시용 정보와 블로그 목록.

    이메일은 userinfo 범위가 있어야 읽을 수 있는데 이 프로젝트 토큰에는 없습니다.
    없으면 블로그 주소로 대신 알아볼 수 있으므로 실패해도 계속 진행합니다.
    """
    token = blogger._access_token()
    email = "(이메일 범위 없음 — 아래 블로그 목록으로 계정을 확인하세요)"
    try:
        me = net.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        email = me.get("email") or email
    except Exception:  # noqa: BLE001
        pass
    blogs = net.get(
        f"{blogger.API_BASE}/users/self/blogs",
        headers={"Authorization": f"Bearer {token}"},
    ).json().get("items", [])
    return email, blogs


def cmd_check() -> int:
    email, blogs = _account_info()
    print(f"현재 토큰의 계정: {email}")
    print(f"이 계정의 블로그 {len(blogs)}개")
    fleet = fleet_mod.load_fleet()
    known = {b.blog_id for b in fleet.blogs}
    for b in blogs:
        print(f"  - {b['name']}  {b['url']}  id={b['id']}  [{'등록됨' if b['id'] in known else '미등록'}]")
    stale = [b for b in fleet.blogs if b.blog_id not in {x["id"] for x in blogs}]
    if stale:
        print(f"\n⚠️ blogs.yaml 에 있지만 이 계정에 없는 블로그 {len(stale)}개 (이전 계정 것):")
        for b in stale:
            print(f"  - {b.id} ({b.name})")
        print("→ --apply 로 교체하면 정리됩니다.")
    return 0


def _archive(stamp: str) -> Path:
    dest = ARCHIVE_ROOT / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for rel in ("fleet/blogs.yaml", "data/blogs", "data/fleet"):
        src = ROOT / rel
        if not src.exists():
            continue
        target = dest / rel.replace("/", "_")
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)
    return dest


def _render_yaml(fleet: fleet_mod.Fleet, entries: list[dict]) -> str:
    head = (ROOT / "fleet" / "blogs.yaml").read_text(encoding="utf-8").split("\nblogs:")[0]
    out = [head, "\nblogs:"]
    for e in entries:
        out.append(f"  - id: {e['id']}")
        out.append(f"    name: {json.dumps(e['name'], ensure_ascii=False)}")
        out.append(f"    blog_id: \"{e['blog_id']}\"")
        out.append(f"    account: {e['account']}")
        out.append(f"    slots: {json.dumps(e['slots'], ensure_ascii=False)}")
        out.append("    enabled: true")
        out.append("    content_mode: planned")
        out.append(f"    subject: {json.dumps(e['subject'], ensure_ascii=False)}")
        out.append(f"    pillars: {json.dumps(e['pillars'], ensure_ascii=False)}")
        out.append(f"    audience: {json.dumps(e['audience'], ensure_ascii=False)}")
        out.append(f"    persona: {json.dumps(e['persona'], ensure_ascii=False)}")
        out.append("    include_patterns: []")
        out.append("    exclude_patterns: []")
        out.append(f"    labels: {json.dumps(e['labels'], ensure_ascii=False)}")
        out.append("    overrides: {}")
    return "\n".join(out) + "\n"


def _plan(posts_per_day: int) -> tuple[list[dict], list[fleet_mod.Blog], str]:
    email, blogs = _account_info()
    old = fleet_mod.load_fleet()
    # 이전 설정의 주제를 순서대로 물려줍니다. 새 블로그가 더 많으면 나머지는 비워 둡니다.
    donors = [b for b in old.blogs if b.subject]
    empty = fleet_mod.Fleet(old.settings, old.accounts, [])
    entries = []
    for i, b in enumerate(blogs):
        d = donors[i] if i < len(donors) else None
        entries.append(
            {
                "id": b["url"].split("//")[-1].split(".")[0].replace("-", "") or f"blog{i+1}",
                "name": b["name"],
                "blog_id": b["id"],
                "account": "default",
                "slots": fleet_mod.next_free_slots(empty, posts_per_day, extra_taken=[]),
                "subject": d.subject if d else "",
                "pillars": d.pillars if d else [],
                "audience": d.audience if d else "",
                "persona": d.persona if d else "",
                "labels": d.labels if d else ["생활정보"],
            }
        )
        empty.blogs.append(
            fleet_mod.Blog(id=entries[-1]["id"], name=b["name"], blog_id=b["id"], slots=entries[-1]["slots"])
        )
    return entries, old.blogs, email


def cmd_plan(posts_per_day: int) -> int:
    entries, old_blogs, email = _plan(posts_per_day)
    print(f"새 계정: {email}\n")
    print("교체 후 함대:")
    for e in entries:
        subj = e["subject"] or "⚠️ 주제 없음 — blogs.yaml 에서 채워야 함"
        print(f"  - {e['id']:<14} {e['name']:<12} 슬롯 {', '.join(e['slots'])}  주제: {subj}")
    print(f"\n보관될 이전 블로그 {len(old_blogs)}개: " + ", ".join(b.id for b in old_blogs))
    print("\n--apply 를 붙이면 실제로 바꿉니다.")
    return 0


def cmd_apply(posts_per_day: int) -> int:
    entries, old_blogs, email = _plan(posts_per_day)
    if not entries:
        print("새 계정에 블로그가 없습니다. blogger.com 에서 먼저 만드세요.")
        return 1

    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    dest = _archive(stamp)
    print(f"이전 설정·이력 보관: {dest.relative_to(ROOT)}")

    fleet_path = ROOT / "fleet" / "blogs.yaml"
    fleet_path.write_text(_render_yaml(fleet_mod.load_fleet(), entries), encoding="utf-8")
    print(f"blogs.yaml 새로 씀: 블로그 {len(entries)}개")

    # 블로그별 상태 초기화 — 새 블로그라 과거 이력이 의미 없습니다.
    blogs_dir = ROOT / "data" / "blogs"
    if blogs_dir.exists():
        shutil.rmtree(blogs_dir)
    for e in entries:
        d = blogs_dir / e["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "history.json").write_text("[]", encoding="utf-8")
        (d / "runs.json").write_text("{}", encoding="utf-8")
    for f in ("claims.json", "manager_state.json"):
        p = ROOT / "data" / "fleet" / f
        if p.exists():
            p.write_text("{}" if f.endswith("state.json") else "{}", encoding="utf-8")
    print("블로그별 이력·실행 기록 초기화 완료")

    try:
        fleet_mod.load_fleet()
        print("검증 OK")
    except ValueError as exc:
        print(f"\n⚠️ 검증 실패: {exc}")
        print("→ fleet/blogs.yaml 에서 subject(고유 주제)를 블로그마다 다르게 채우세요.")

    missing = [e["id"] for e in entries if not e["subject"]]
    print("\n다음 할 일:")
    if missing:
        print(f"  1. blogs.yaml 에서 subject 채우기: {', '.join(missing)}")
    print("  2. python scripts/setup_pages.py --all")
    print("  3. GitHub Secrets 갱신 (BLOGGER_CLIENT_ID/SECRET/REFRESH_TOKEN)")
    print("  4. gh workflow enable fleet.yml && gh workflow enable manager.yml")
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Blogger 계정 교체")
    p.add_argument("--check", action="store_true", help="현재 토큰의 계정과 블로그 확인")
    p.add_argument("--plan", action="store_true", help="교체 계획 미리 보기")
    p.add_argument("--apply", action="store_true", help="실제 교체")
    p.add_argument("--posts-per-day", type=int, default=2, help="블로그당 하루 글 수 = 슬롯 수 (기본 2)")
    args = p.parse_args()

    load_dotenv()
    if not env("BLOGGER_REFRESH_TOKEN"):
        print("BLOGGER_REFRESH_TOKEN 이 없습니다.")
        print(STEPS)
        return 1

    if args.check:
        return cmd_check()
    if args.plan:
        return cmd_plan(args.posts_per_day)
    if args.apply:
        return cmd_apply(args.posts_per_day)
    print(STEPS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
