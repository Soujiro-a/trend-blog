"""함대 관리 명령줄 도구.

    python scripts/fleet_cli.py list                      # 슬롯표
    python scripts/fleet_cli.py add --name "이름" --blog-id 123 [--niche ..] [--persona ..] [--account default]
    python scripts/fleet_cli.py discover [--add]          # 계정의 Blogger 블로그 전부 조회 (--add: 미등록 블로그 자동 등록)
    python scripts/fleet_cli.py enable <id> / disable <id>
    python scripts/fleet_cli.py validate                  # 형식·슬롯 간격 검사
    python scripts/fleet_cli.py status                    # 블로그별 최근 실행/공개 현황

add/discover 는 blogs.yaml 끝에 블로그 항목을 덧붙이고 다음 빈 슬롯(10분 간격)을 배정합니다.
기존 주석은 그대로 유지됩니다.
"""

from __future__ import annotations

import argparse
import json
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


def cmd_add(args) -> int:
    fleet = fleet_mod.load_fleet()
    if any(b.blog_id == args.blog_id for b in fleet.blogs):
        print(f"이미 등록된 blog_id 입니다: {args.blog_id}")
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
    token = blogger._access_token()
    resp = net.get(f"{blogger.API_BASE}/users/self/blogs", headers={"Authorization": f"Bearer {token}"})
    items = resp.json().get("items", [])
    fleet = fleet_mod.load_fleet()
    known = {b.blog_id for b in fleet.blogs}
    print(f"계정에 블로그 {len(items)}개")
    added = 0
    for it in items:
        mark = "등록됨" if it["id"] in known else "미등록"
        print(f"  - {it['name']}  {it['url']}  id={it['id']}  [{mark}]")
        if args.add and it["id"] not in known:
            fleet = fleet_mod.load_fleet()
            entry = {
                "id": _slug(it["url"].split("//")[-1].split(".")[0], {b.id for b in fleet.blogs}),
                "name": it["name"], "blog_id": it["id"], "account": args.account,
                "slots": fleet_mod.next_free_slots(fleet, args.posts_per_day),
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
    st.setdefault("blogs", {}).setdefault(blog_id, {})["enabled"] = enabled
    st["blogs"][blog_id]["note"] = f"{'수동 켬' if enabled else '수동 끔'} {datetime.now(KST).strftime('%m-%d')}"
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
    print("| 블로그 | 슬롯 | 오늘 완료 | 7일 공개/보류/거부 | 7일 비용 | 마지막 결과 |")
    print("|---|---|---|---|---:|---|")
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    for b in sorted(fleet.blogs, key=lambda b: b.slot_minutes):
        runs = fleet_mod.load_runs(b)
        done = sum(
            1 for s in b.slots
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
        last = sorted(runs.items())[-1][1]["summary"] if runs else "-"
        print(
            f"| {b.name} ({b.id}) | {', '.join(b.slots)} | {done}/{len(b.slots)} | "
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
