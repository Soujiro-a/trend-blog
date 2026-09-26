"""함대 관리 명령줄 도구.

    python scripts/fleet_cli.py list                      # 슬롯표 (괄호 = 램프업 대기)
    python scripts/fleet_cli.py add --name "이름" --blog-id 123 --subject "고유 주제" --account <계정>
    python scripts/fleet_cli.py discover --account <계정> [--add]   # 그 계정의 Blogger 블로그 조회 (--add: 미등록 블로그 등록)
    python scripts/fleet_cli.py enable <id> / disable <id>
    python scripts/fleet_cli.py validate                  # 형식·슬롯 간격·주제 중복 검사
    python scripts/fleet_cli.py status                    # 블로그별 최근 실행/공개 현황

add/discover 는 blogs.yaml 끝에 블로그 항목을 덧붙이고 다음 빈 슬롯(slot_step_minutes 간격)을 배정합니다.
기존 주석은 그대로 유지됩니다. 비상정지된 계정(account_state.json)에는 블로그를 추가하지 않습니다.
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
from src import net, state  # noqa: E402
from src.config import load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402


def _slug(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^0-9a-z]+", "-", name.lower()).strip("-") or "blog"
    if not re.search(r"[a-z]", base):
        base = "blog"
    slug, n = base, 2
    while slug in existing:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _append_blog(entry: dict) -> None:
    """blogs.yaml 끝에 항목을 텍스트로 덧붙입니다 (yaml.dump 는 주석을 날리므로)."""
    path = fleet_mod.FLEET_PATH
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    lines = [
        f"  - id: {entry['id']}",
        f"    name: {json.dumps(entry['name'], ensure_ascii=False)}",
        f"    blog_id: \"{entry['blog_id']}\"",
        f"    account: {entry.get('account', 'default')}",
        f"    slots: {json.dumps(entry['slots'], ensure_ascii=False)}",
        "    enabled: true",
        f"    content_mode: {entry.get('content_mode', 'planned')}",
        f"    subject: {json.dumps(entry.get('subject', ''), ensure_ascii=False)}",
        f"    pillars: {json.dumps(entry.get('pillars', []), ensure_ascii=False)}",
        f"    audience: {json.dumps(entry.get('audience', ''), ensure_ascii=False)}",
        f"    persona: {json.dumps(entry.get('persona', ''), ensure_ascii=False)}",
        "    include_patterns: []",
        "    exclude_patterns: []",
        f"    labels: {json.dumps(entry.get('labels', ['생활정보']), ensure_ascii=False)}",
        "    overrides: {}",
    ]
    path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")


def cmd_list(_args) -> int:
    fleet = fleet_mod.load_fleet()
    print(fleet_mod.schedule_table(fleet))
    slots = sum(len(v) for v in fleet_mod.planned_slots(fleet).values())
    print(
        f"\n{len(fleet.blogs)}개 블로그 · 켜짐 {sum(b.enabled for b in fleet.blogs)}개 · "
        f"오늘 하루 {slots}건 · 다음 빈 슬롯 {fleet_mod.next_free_slot(fleet)}"
    )
    print("(괄호) 슬롯은 램프업 대기 — 블로그·계정 나이가 차면 자동으로 켜집니다.")
    for w in fleet_mod.ramp_warnings(fleet):
        print(f"  ⚠️ {w}")
    return 0


def _account_problem(fleet: fleet_mod.Fleet, account: str) -> str:
    """이 계정에 블로그를 붙이면 안 되는 이유. 괜찮으면 빈 문자열.

    blogs.yaml 에 먼저 써 버리면 검증 실패 뒤 사람이 손으로 지워야 하므로, 쓰기 전에 확인합니다.
    """
    if account not in fleet.accounts:
        return (f"계정 '{account}' 이(가) blogs.yaml 의 accounts 에 없습니다 "
                f"(있는 것: {', '.join(fleet.accounts)}). 새 계정은 python scripts/account_cli.py add <이름>")
    why = fleet_mod.account_halted(account)
    if why:
        return f"계정 '{account}' 은 비상정지 상태입니다({why[:80]}). 이 계정의 블로그는 돌지 않습니다 — --account 로 다른 계정을 지정하세요"
    return ""


def cmd_add(args) -> int:
    fleet = fleet_mod.load_fleet()
    if any(b.blog_id == args.blog_id for b in fleet.blogs):
        print(f"이미 등록된 blog_id 입니다: {args.blog_id}")
        return 1
    problem = _account_problem(fleet, args.account)
    if problem:
        print(problem)
        return 1
    posts = args.posts_per_day or int(
        (fleet.settings.get("defaults", {}).get("run", {}) or {}).get("posts_per_run", 1)
    ) + 1
    entry = {
        "id": args.id or _slug(args.name, {b.id for b in fleet.blogs}),
        "name": args.name,
        "blog_id": args.blog_id,
        "account": args.account,
        "slots": args.slots.split(",") if args.slots else fleet_mod.next_free_slots(fleet, posts),
        "subject": args.subject or "",
        "audience": args.audience or "",
        "persona": args.persona or "",
    }
    _append_blog(entry)
    try:
        fleet_mod.load_fleet()  # 검증 (subject 누락·중복이면 여기서 걸립니다)
    except ValueError as exc:
        print(f"등록했지만 검증 실패: {exc}\n→ fleet/blogs.yaml 에서 해당 항목을 고치세요.")
        return 1
    print(f"등록: {entry['id']} ({entry['name']}) 슬롯 {', '.join(entry['slots'])} KST")
    if not entry["subject"]:
        print("  ⚠️ subject(고유 주제)가 비어 있습니다. planned 모드로 돌리려면 blogs.yaml 에서 채우세요.")
    return 0


def cmd_discover(args) -> int:
    load_dotenv()
    fleet = fleet_mod.load_fleet()
    if args.add:
        problem = _account_problem(fleet, args.account)
        if problem:
            print(problem)
            return 1
    missing = fleet_mod.missing_credentials(fleet, args.account)
    if missing:
        print(f"계정 '{args.account}' 자격증명 없음: {', '.join(missing)}")
        return 1
    # 그 계정의 자격증명으로 조회합니다. 예전엔 --account 와 상관없이 표준 변수(default 계정)로 조회해서,
    # default 계정의 블로그를 다른 계정 이름으로 등록할 수 있었습니다.
    for base, value in fleet_mod.account_credentials(args.account).items():
        os.environ[base] = value
    token = blogger._access_token()
    resp = net.get(f"{blogger.API_BASE}/users/self/blogs", headers={"Authorization": f"Bearer {token}"})
    items = resp.json().get("items", [])
    known = {b.blog_id for b in fleet.blogs}
    ids = {b.id for b in fleet.blogs}
    taken: list[str] = []
    print(f"계정 '{args.account}' 에 블로그 {len(items)}개")
    added = 0
    for it in items:
        mark = "등록됨" if it["id"] in known else "미등록"
        print(f"  - {it['name']}  {it['url']}  id={it['id']}  [{mark}]")
        if args.add and it["id"] not in known:
            # 슬롯은 처음 읽은 함대로 계산합니다. 도중에 load_fleet 를 다시 부르면 방금 덧붙인
            # subject 없는 항목이 검증에 걸려 두 번째 블로그부터 등록이 멈춥니다.
            slots = fleet_mod.next_free_slots(fleet, args.posts_per_day, extra_taken=taken)
            taken += slots
            slug = _slug(it["url"].split("//")[-1].split(".")[0], ids)
            ids.add(slug)
            entry = {
                "id": slug,
                "name": it["name"], "blog_id": it["id"], "account": args.account,
                "slots": slots,
                "content_mode": "planned",
            }
            _append_blog(entry)
            added += 1
            print(f"      → 등록: {entry['id']} 슬롯 {', '.join(entry['slots'])}")
    if args.add and added:
        print(
            f"\n{added}개 등록됨. **blogs.yaml 에서 블로그마다 subject(고유 주제)를 채워야** 실행됩니다.\n"
            "  주제가 비어 있거나 다른 블로그와 겹치면 validate 가 막습니다."
        )
    return 0


def _toggle(blog_id: str, enabled: bool) -> int:
    fleet = fleet_mod.load_fleet()
    fleet.get(blog_id)
    st = fleet_mod.load_manager_state()
    # by=manual 이라야 관리 에이전트(src/manager.py)가 이 결정을 되돌리지 않습니다. 예전엔 by 를 안 적어서,
    # 관리자가 멈췄던 블로그를 사람이 다시 끄면 by=manager 가 남아 다음 날 밤 자동으로 켜질 수 있었습니다.
    # 켤 때도 적습니다 — 켜진 동안은 관리자 판단에 쓰이지 않고(연속 실패면 여전히 중지됨), 누가 켰는지만 남깁니다.
    st.setdefault("blogs", {}).setdefault(blog_id, {}).update({
        "enabled": enabled,
        "by": "manual",
        "note": f"{'수동 켬' if enabled else '수동 끔'} {datetime.now(KST).strftime('%m-%d')}",
    })
    fleet_mod.save_manager_state(st)
    print(f"{blog_id}: {'켜짐' if enabled else '꺼짐'} (data/fleet/manager_state.json)")
    return 0


def cmd_validate(_args) -> int:
    fleet = fleet_mod.load_fleet()
    slots = sum(len(v) for v in fleet_mod.planned_slots(fleet).values())
    print(
        f"OK — 블로그 {len(fleet.blogs)}개, 오늘 하루 {slots}건(램프업 적용), 슬롯 간격 ≥ {fleet.step}분, "
        f"주제 중복 없음, 계정 {', '.join(fleet.accounts)}"
    )
    for w in fleet_mod.ramp_warnings(fleet):
        print(f"  ⚠️ {w}")
    return 0


def cmd_status(_args) -> int:
    fleet = fleet_mod.load_fleet()
    # 오늘 완료는 '오늘 켜진 슬롯'(램프업·계정 상한 반영) 기준으로 셉니다. 적어 둔 슬롯 기준이면
    # 램프업 대기 중인 두 번째 슬롯 때문에 늘 "1/2" 로 보여 덜 돈 것처럼 보입니다.
    plan = fleet_mod.planned_slots(fleet)
    print("| 블로그 | 슬롯 | 오늘 완료 / 켜진 슬롯 | 7일 공개/보류/거부 | 7일 비용 | 마지막 결과 |")
    print("|---|---|---|---|---:|---|")
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    for b in sorted(fleet.blogs, key=lambda b: (not b.enabled, b.account, b.slot_minutes)):
        runs = fleet_mod.load_runs(b)
        active = plan.get(b.id, []) if b.enabled else []
        done = sum(
            1 for s in active
            if any(f"{m}:{today_str}:{s}" in runs for m in ("planned", "trend", "evergreen"))
        )
        try:
            hist = state.recent(state.load(b.history_path), 7)
        except state.HistoryCorrupted as exc:
            print(f"| {b.name} ({b.id}) | {', '.join(b.slots)} | ⚠️ | **이력 손상** | - | {str(exc)[:60]} |")
            continue
        live = sum(h.get("status") == "live" for h in hist)
        draft = sum(h.get("status") == "draft" for h in hist)
        rej = sum(h.get("status") == "rejected" for h in hist)
        cost = sum(float(h.get("cost_usd") or 0) for h in hist)
        # 시각 순으로 마지막 실행 (키 순으로 고르면 모드 이름 순서 때문에 옛 기록이 나옵니다)
        last = max(runs.values(), key=lambda r: r.get("at", "")).get("summary", "-") if runs else "-"
        today = f"{done}/{len(active)}" if b.enabled else "꺼짐"
        print(
            f"| {b.name} ({b.id}) | {', '.join(b.slots)} | {today} | "
            f"{live}/{draft}/{rej} | ${cost:.2f} | {last[:60]} |"
        )
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="블로그 함대 관리")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = sub.add_parser("add")
    a.add_argument("--name", required=True)
    a.add_argument("--blog-id", required=True)
    a.add_argument("--id")
    a.add_argument("--slots", help="쉼표로 구분한 시각들 (예: 06:20,15:00). 비우면 자동 배정")
    a.add_argument("--posts-per-day", type=int, help="자동 배정할 슬롯 수 (기본 2)")
    a.add_argument("--account", default="default")
    a.add_argument("--subject", help="이 블로그만의 고유 주제 (planned 모드 필수)")
    a.add_argument("--audience")
    a.add_argument("--persona")
    a.set_defaults(fn=cmd_add)
    d = sub.add_parser("discover")
    d.add_argument("--add", action="store_true")
    d.add_argument("--account", default="default")
    d.add_argument("--posts-per-day", type=int, default=2)
    d.set_defaults(fn=cmd_discover)
    e = sub.add_parser("enable"); e.add_argument("blog"); e.set_defaults(fn=lambda a: _toggle(a.blog, True))
    x = sub.add_parser("disable"); x.add_argument("blog"); x.set_defaults(fn=lambda a: _toggle(a.blog, False))
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
