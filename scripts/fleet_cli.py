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
        f"    slot: \"{entry['slot']}\"",
        "    enabled: true",
        f"    niche: {json.dumps(entry.get('niche', ''), ensure_ascii=False)}",
        f"    persona: {json.dumps(entry.get('persona', ''), ensure_ascii=False)}",
        "    include_patterns: []",
        "    exclude_patterns: []",
        f"    labels: {json.dumps(entry.get('labels', ['실시간이슈']), ensure_ascii=False)}",
        "    overrides: {}",
    ]
    path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")


def cmd_list(_args) -> int:
    fleet = fleet_mod.load_fleet()
    print(fleet_mod.schedule_table(fleet))
    print(f"\n{len(fleet.blogs)}개 블로그 · 켜짐 {sum(b.enabled for b in fleet.blogs)}개 · 다음 빈 슬롯 {fleet_mod.next_free_slot(fleet)}")
    return 0


def cmd_add(args) -> int:
    fleet = fleet_mod.load_fleet()
    if any(b.blog_id == args.blog_id for b in fleet.blogs):
        print(f"이미 등록된 blog_id 입니다: {args.blog_id}")
        return 1
    entry = {
        "id": args.id or _slug(args.name, {b.id for b in fleet.blogs}),
        "name": args.name,
        "blog_id": args.blog_id,
        "account": args.account,
        "slot": args.slot or fleet_mod.next_free_slot(fleet),
        "niche": args.niche or "",
        "persona": args.persona or "",
    }
    _append_blog(entry)
    fleet_mod.load_fleet()  # 검증
    print(f"등록: {entry['id']} ({entry['name']}) 슬롯 {entry['slot']} KST")
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
                "slot": fleet_mod.next_free_slot(fleet),
            }
            _append_blog(entry)
            added += 1
            print(f"      → 등록: {entry['id']} 슬롯 {entry['slot']}")
    if args.add:
        fleet_mod.load_fleet()
        print(f"\n{added}개 등록됨. persona/niche 는 blogs.yaml 에서 블로그마다 다르게 채워 주세요.")
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
    print(f"OK — 블로그 {len(fleet.blogs)}개, 슬롯 간격 ≥ {fleet.step}분, 계정 {', '.join(fleet.accounts)}")
    return 0


def cmd_status(_args) -> int:
    fleet = fleet_mod.load_fleet()
    print("| 블로그 | 슬롯 | 오늘 실행 | 7일 공개/보류/거부 | 7일 비용 | 마지막 결과 |")
    print("|---|---|---|---|---:|---|")
    for b in sorted(fleet.blogs, key=lambda b: b.slot_minutes):
        runs = fleet_mod.load_runs(b)
        today = runs.get(f"trend:{datetime.now(KST).strftime('%Y-%m-%d')}")
        hist = state.recent(state.load(b.history_path), 7)
        live = sum(h.get("status") == "live" for h in hist)
        draft = sum(h.get("status") == "draft" for h in hist)
        rej = sum(h.get("status") == "rejected" for h in hist)
        cost = sum(float(h.get("cost_usd") or 0) for h in hist)
        last = sorted(runs.items())[-1][1]["summary"] if runs else "-"
        print(f"| {b.name} ({b.id}) | {b.slot} | {'✅' if today and today['ok'] else ('❌' if today else '-')} | {live}/{draft}/{rej} | ${cost:.2f} | {last[:60]} |")
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
    a.add_argument("--slot")
    a.add_argument("--account", default="default")
    a.add_argument("--niche")
    a.add_argument("--persona")
    a.set_defaults(fn=cmd_add)
    d = sub.add_parser("discover")
    d.add_argument("--add", action="store_true")
    d.add_argument("--account", default="default")
    d.set_defaults(fn=cmd_discover)
    e = sub.add_parser("enable"); e.add_argument("blog"); e.set_defaults(fn=lambda a: _toggle(a.blog, True))
    x = sub.add_parser("disable"); x.add_argument("blog"); x.set_defaults(fn=lambda a: _toggle(a.blog, False))
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
