"""구글 계정 관리 — 한 저장소에서 여러 계정의 블로그를 함께 운영합니다.

왜 계정을 나누나
----------------
구글의 제한은 **계정 단위**로 붙습니다(2026-09-20: 한 계정의 Blogger API 쓰기 차단).
계정을 나누면 하루 발행 상한도 계정마다 따로 계산되고, 한 계정에 문제가 생겨도 나머지는
계속 돕니다. 저장소를 복사할 필요 없이 `fleet/blogs.yaml` 의 `accounts:` 에 이름을 추가하면 됩니다.

환경변수 규칙
-------------
  default 계정 : BLOGGER_CLIENT_ID      / BLOGGER_CLIENT_SECRET      / BLOGGER_REFRESH_TOKEN
  그 외 계정   : BLOGGER_CLIENT_ID_<대문자> / BLOGGER_CLIENT_SECRET_<대문자> / BLOGGER_REFRESH_TOKEN_<대문자>
예) 계정 이름이 `second` 면 BLOGGER_REFRESH_TOKEN_SECOND

명령
----
    python scripts/account_cli.py list                 # 계정별 자격증명·블로그 현황
    python scripts/account_cli.py add <이름>            # 계정 추가 (다음에 할 일을 안내)
    python scripts/account_cli.py check <이름>          # 그 계정 토큰으로 실제 블로그 조회
    python scripts/account_cli.py import <이름>         # 그 계정의 블로그를 함대에 등록
    python scripts/account_cli.py retire <이름>         # 그 계정 블로그를 전부 중지 (계정 갈아탈 때)
    python scripts/account_cli.py remove <이름>         # 계정 제거 (블로그가 없을 때만)
    python scripts/account_cli.py halt <이름> --reason "..."   # 비상정지 (403 이 나면 자동으로 걸립니다)
    python scripts/account_cli.py resume <이름>         # 비상정지 해제 — 램프업은 처음 단계부터 다시

계정을 갈아탈 때
----------------
새 계정을 add → import 한 뒤, 옛 계정을 retire 하면 됩니다. 저장소를 복사하지 않아도 되고,
옛 계정의 이력·설정은 그대로 남아 나중에 되살릴 수 있습니다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import net  # noqa: E402
from src.config import ROOT, load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402

FLEET_PATH = ROOT / "fleet" / "blogs.yaml"


def _vars(account: str) -> list[str]:
    return [fleet_mod.env_name(b, account) for b in fleet_mod.CREDENTIAL_VARS]


def _blogs_of_account(fleet: fleet_mod.Fleet, account: str) -> list[dict]:
    """그 계정 토큰으로 실제 블로그 목록을 조회합니다."""
    probe = fleet.blogs_of(account, enabled_only=False)
    if probe:
        fleet_mod.apply_env(fleet, probe[0])
    else:
        # 아직 등록된 블로그가 없으면 자격증명만 올립니다.
        for base in fleet_mod.CREDENTIAL_VARS:
            src = fleet_mod.env_name(base, account)
            if os.environ.get(src):
                os.environ[base] = os.environ[src]
    token = blogger._access_token()
    return net.get(
        f"{blogger.API_BASE}/users/self/blogs",
        headers={"Authorization": f"Bearer {token}"},
    ).json().get("items", [])


def cmd_list(_args) -> int:
    fleet = fleet_mod.load_fleet()
    accounts = list(fleet.accounts) or ["default"]
    st = fleet_mod.load_account_state()
    plan = fleet_mod.planned_slots(fleet, st=st)
    print("| 계정 | 자격증명 | 상태 | 블로그 | 오늘 켜진 슬롯 / 적어둔 슬롯 | 오늘 상한 / 최종 상한 |")
    print("|---|---|---|---:|---:|---:|")
    for acc in accounts:
        missing = fleet_mod.missing_credentials(fleet, acc)
        blogs = fleet.blogs_of(acc, enabled_only=False)
        slots = sum(len(b.slots) for b in blogs if b.enabled)
        active = sum(len(plan.get(b.id, [])) for b in blogs if b.enabled)
        ceiling = fleet.account_setting(acc, "max_live_per_day_account", 6)
        cap = fleet_mod.account_cap(fleet, acc, st=st)
        halted = fleet_mod.account_halted(acc, st)
        cred = "✅ 있음" if not missing else f"❌ {', '.join(missing)}"
        print(f"| {acc} | {cred} | {'⛔ ' + halted[:40] if halted else '정상'} | {len(blogs)} | {active} / {slots} | {cap} / {ceiling} |")
    print()
    for acc in accounts:
        blogs = fleet.blogs_of(acc, enabled_only=False)
        if blogs:
            print(f"{acc}: " + ", ".join(f"{b.id}({', '.join(b.slots)})" for b in blogs))
    return 0


def cmd_add(args) -> int:
    name = args.name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        print("계정 이름은 영소문자로 시작하는 영숫자/밑줄만 됩니다 (환경변수 이름이 되기 때문).")
        return 1
    fleet = fleet_mod.load_fleet()
    if name in fleet.accounts:
        print(f"이미 있는 계정입니다: {name}")
    else:
        text = FLEET_PATH.read_text(encoding="utf-8")
        marker = "\naccounts:\n"
        if marker not in text:
            print("blogs.yaml 에서 accounts: 블록을 찾지 못했습니다. 직접 추가하세요.")
            return 1
        today = datetime.now(KST).strftime("%Y-%m-%d")
        entry = (
            f"  {name}:\n"
            f"    since: {today}   # 램프업 기준일 (첫 글 올리는 날로 고치세요)\n"
            f"    max_live_per_day_account: {args.cap}   # 최종 상한. 오늘 상한은 램프업이 정합니다\n"
        )
        idx = text.index(marker) + len(marker)
        FLEET_PATH.write_text(text[:idx] + entry + text[idx:], encoding="utf-8")
        print(f"blogs.yaml 의 accounts 에 '{name}' 추가 (최종 상한 {args.cap}건/일, 처음 4주는 램프업으로 더 낮음)")

    v = _vars(name)
    print(f"""
다음에 할 일

 1. 새 구글 계정으로 blogger.com 에서 블로그를 만듭니다 (처음엔 2개. 나머지는 몇 주 뒤에 하나씩).

 2. **그 계정으로** Google Cloud 프로젝트를 새로 만듭니다.
    - Blogger API v3 사용 설정 (주간 보고용으로 Search Console·AdSense API 도)
    - Google 인증 플랫폼 → 브랜딩 채우고 **앱 게시(프로덕션)**  ← 안 하면 토큰이 7일 뒤 만료
    - 사용자 인증 정보 → OAuth 클라이언트 ID → **데스크톱 앱**
    ⚠️ 기존 계정의 Cloud 프로젝트를 재사용하지 마세요. 제한된 계정과 엮입니다.

 3. .env 에 클라이언트 정보를 넣습니다 (이 이름 그대로):
       {v[1]}=...
       {v[2]}=...

 4. 토큰 발급 — 브라우저에서 **{name} 계정으로** 승인:
       python scripts/get_blogger_token.py --full --from-env --write-env --account {name}

 5. 확인하고 블로그를 함대에 등록:
       python scripts/account_cli.py check {name}
       python scripts/account_cli.py import {name}

 6. blogs.yaml 에서 새 블로그마다 subject(고유 주제)를 채웁니다. 기존 블로그와 겹치면 안 됩니다.
       python scripts/fleet_cli.py validate

 7. GitHub Secrets 에 같은 이름으로 3개 등록:
       {v[0]} / {v[1]} / {v[2]}

 8. 워크플로 두 곳(.github/workflows/fleet.yml, manager.yml)의 env 에 같은 세 줄을 추가합니다:
       {v[0]}: ${{{{ secrets.{v[0]} }}}}
       {v[1]}: ${{{{ secrets.{v[1]} }}}}
       {v[2]}: ${{{{ secrets.{v[2]} }}}}
    (저장소가 공개라 시크릿을 한꺼번에 넘기지 않고 필요한 것만 적어 둡니다)
""")
    return 0


def cmd_check(args) -> int:
    fleet = fleet_mod.load_fleet()
    name = args.name
    missing = fleet_mod.missing_credentials(fleet, name)
    if missing:
        print(f"계정 '{name}' 자격증명 없음: {', '.join(missing)}")
        print(f"→ python scripts/account_cli.py add {name}  로 순서를 확인하세요.")
        return 1
    try:
        blogs = _blogs_of_account(fleet, name)
    except Exception as exc:  # noqa: BLE001
        print(f"조회 실패: {str(exc)[:300]}")
        return 1
    known = {b.blog_id for b in fleet.blogs}
    print(f"계정 '{name}' 토큰 정상 · 블로그 {len(blogs)}개")
    for b in blogs:
        print(f"  - {b['name']}  {b['url']}  id={b['id']}  [{'등록됨' if b['id'] in known else '미등록'}]")
    wrong = [b for b in fleet.blogs_of(name, enabled_only=False) if b.blog_id not in {x['id'] for x in blogs}]
    if wrong:
        print(f"\n⚠️ blogs.yaml 에서 이 계정으로 돼 있지만 실제로는 없는 블로그: {', '.join(b.id for b in wrong)}")
        print("   account 값이 잘못됐거나 다른 계정의 블로그입니다.")
        return 1
    return 0


def _slug(url: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "", url.split("//")[-1].split(".")[0].lower()) or "blog"
    slug, n = base, 2
    while slug in existing:
        slug, n = f"{base}{n}", n + 1
    return slug


def cmd_import(args) -> int:
    fleet = fleet_mod.load_fleet()
    name = args.name
    if fleet_mod.missing_credentials(fleet, name):
        print(f"계정 '{name}' 자격증명이 없습니다. add 로 순서를 확인하세요.")
        return 1
    blogs = _blogs_of_account(fleet, name)
    known = {b.blog_id for b in fleet.blogs}
    new = [b for b in blogs if b["id"] not in known]
    if not new:
        print("등록할 새 블로그가 없습니다.")
        return 0

    ids = {b.id for b in fleet.blogs}
    added = []
    # 슬롯은 여기서 한 번에 계산합니다. 중간에 load_fleet 를 다시 부르면
    # 방금 쓴 subject 없는 항목 때문에 검증에서 막힙니다.
    taken: list[str] = []
    for b in new:
        slug = _slug(b["url"], ids)
        ids.add(slug)
        slots = fleet_mod.next_free_slots(fleet, args.posts_per_day, extra_taken=taken)
        taken += slots
        lines = [
            f"  - id: {slug}",
            f"    name: {json.dumps(b['name'], ensure_ascii=False)}",
            f"    blog_id: \"{b['id']}\"",
            f"    account: {name}",
            f"    slots: {json.dumps(slots, ensure_ascii=False)}",
            "    enabled: true",
            "    content_mode: planned",
            '    subject: ""          # ← 반드시 채우세요 (다른 블로그와 겹치면 validate 가 막습니다)',
            "    pillars: []",
            '    audience: ""',
            '    persona: ""',
            "    include_patterns: []",
            "    exclude_patterns: []",
            '    labels: ["생활정보"]',
            "    overrides: {}",
        ]
        text = FLEET_PATH.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        FLEET_PATH.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")
        added.append((slug, b["name"], slots))
        (ROOT / "data" / "blogs" / slug).mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "blogs" / slug / "history.json").write_text("[]", encoding="utf-8")
        (ROOT / "data" / "blogs" / slug / "runs.json").write_text("{}", encoding="utf-8")

    for slug, nm, slots in added:
        print(f"등록: {slug} ({nm}) 계정 {name} 슬롯 {', '.join(slots)}")
    print(f"\n{len(added)}개 등록됨. **blogs.yaml 에서 subject 를 채운 뒤** validate 를 돌리세요:")
    print("  python scripts/fleet_cli.py validate")
    return 0


def cmd_halt(args) -> int:
    fleet_mod.load_fleet().accounts  # 설정 검증
    fleet_mod.halt_account(args.name, args.reason or "수동 비상정지")
    print(f"계정 '{args.name}' 비상정지. 이 계정의 블로그는 글을 쓰지도 올리지도 않습니다.")
    print("커밋·푸시해야 GitHub Actions 에도 적용됩니다: data/fleet/account_state.json")
    return 0


def cmd_resume(args) -> int:
    why = fleet_mod.account_halted(args.name)
    if not why:
        print(f"계정 '{args.name}' 은 멈춰 있지 않습니다.")
        return 0
    print(f"비상정지 사유: {why}")
    if not args.yes:
        print("\nBlogger 에서 원인(정책 알림, API 제한)이 풀린 것을 확인했다면 --yes 를 붙여 다시 실행하세요.")
        return 1
    fleet_mod.resume_account(args.name)
    fleet = fleet_mod.load_fleet()
    print(f"계정 '{args.name}' 재개. 램프업은 오늘부터 다시 시작합니다 — 오늘 상한 {fleet_mod.account_cap(fleet, args.name)}건.")
    print("커밋·푸시해야 GitHub Actions 에도 적용됩니다: data/fleet/account_state.json")
    return 0


def cmd_retire(args) -> int:
    """계정의 블로그를 전부 중지합니다. 계정을 갈아탈 때 옛 계정을 멈추는 용도.

    blogs.yaml 은 건드리지 않고 관리 상태 파일에만 기록하므로, 나중에 enable 로 되살릴 수 있습니다.
    """
    fleet = fleet_mod.load_fleet()
    blogs = fleet.blogs_of(args.name, enabled_only=False)
    if not blogs:
        print(f"계정 '{args.name}' 에 블로그가 없습니다.")
        return 1
    st = fleet_mod.load_manager_state()
    entries = st.setdefault("blogs", {})
    for b in blogs:
        entries.setdefault(b.id, {}).update(
            {"enabled": False, "by": "manual", "note": f"계정 '{args.name}' 중지"}
        )
    fleet_mod.save_manager_state(st)
    print(f"계정 '{args.name}' 의 블로그 {len(blogs)}개를 중지했습니다: {', '.join(b.id for b in blogs)}")
    print("되살리려면: python scripts/fleet_cli.py enable <블로그id>")
    print(f"완전히 정리하려면 blogs.yaml 에서 해당 항목을 지운 뒤 account_cli.py remove {args.name}")
    return 0


def cmd_remove(args) -> int:
    fleet = fleet_mod.load_fleet()
    name = args.name
    if name == "default":
        print("default 계정은 제거할 수 없습니다.")
        return 1
    blogs = fleet.blogs_of(name, enabled_only=False)
    if blogs:
        print(f"이 계정에 블로그가 {len(blogs)}개 있습니다: {', '.join(b.id for b in blogs)}")
        print("먼저 blogs.yaml 에서 해당 블로그를 지우거나 다른 계정으로 옮기세요.")
        return 1
    text = FLEET_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^  {re.escape(name)}:\n(?:    .*\n)*", re.MULTILINE)
    new_text, n = pattern.subn("", text)
    if not n:
        print(f"accounts 에서 '{name}' 을 찾지 못했습니다.")
        return 1
    FLEET_PATH.write_text(new_text, encoding="utf-8")
    print(f"계정 '{name}' 제거. .env 와 GitHub Secrets 의 {', '.join(_vars(name))} 도 지우세요.")
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="구글 계정 관리 (여러 계정의 블로그를 한 저장소에서)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = sub.add_parser("add"); a.add_argument("name"); a.add_argument("--cap", type=int, default=8, help="최종 하루 상한 (램프업이 4→6→8 로 천천히 올림)")
    a.set_defaults(fn=cmd_add)
    c = sub.add_parser("check"); c.add_argument("name"); c.set_defaults(fn=cmd_check)
    i = sub.add_parser("import"); i.add_argument("name"); i.add_argument("--posts-per-day", type=int, default=2)
    i.set_defaults(fn=cmd_import)
    h = sub.add_parser("halt"); h.add_argument("name"); h.add_argument("--reason", default=""); h.set_defaults(fn=cmd_halt)
    u = sub.add_parser("resume"); u.add_argument("name"); u.add_argument("--yes", action="store_true"); u.set_defaults(fn=cmd_resume)
    t = sub.add_parser("retire"); t.add_argument("name"); t.set_defaults(fn=cmd_retire)
    r = sub.add_parser("remove"); r.add_argument("name"); r.set_defaults(fn=cmd_remove)
    args = p.parse_args()
    load_dotenv()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
