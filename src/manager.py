"""함대 관리 에이전트 — 블로그 100개를 사람 대신 돌봅니다.

하루 한 번(모든 슬롯이 끝난 뒤) 실행되어:

1. 블로그별 지표를 모읍니다 — 최근 7일 공개/보류/거부 수, 비용, 검수 평균, 연속 실패,
   Blogger 실제 공개 글 수, (권한이 있으면) Search Console 클릭.
2. 함대 전체 이상을 찾습니다 — 같은 날 여러 블로그가 비슷한 제목을 낸 경우(중복 콘텐츠),
   비용 급증, 실행이 안 된 블로그.
3. Claude(Sonnet 5)에게 지표와 운영 규칙을 주고 **정해진 행동 목록 안에서만** 결정을 받습니다:
      pause / resume / note
   코드가 규칙으로 다시 검증한 뒤 data/fleet/manager_state.json 에 적용합니다.
   **발행량은 올리지 못합니다.** 증량은 블로그·계정 나이로 정해지는 램프업(src/fleet.py)만 합니다.
   (2026-09-25: 관리 모델이 만 4일 된 블로그를 "꾸준하다"며 하루 2건으로 올리려 했습니다.
    계정 차단을 부른 것이 바로 새 블로그의 빠른 증량이었으므로, 이 판단은 모델에게 맡기지 않습니다.)
   (blogs.yaml 은 건드리지 않습니다. 사람이 직접 끈 블로그는 다시 켜지 않습니다.)
4. 보고서를 data/fleet/manager_report.md 에 씁니다. 조치나 경고가 있으면 GitHub 이슈가 됩니다.

모델 호출이 실패하면 규칙 기반 폴백(연속 3회 실패 → 일시 중지)만 적용합니다.

    python -m src.manager            # 실행
    python -m src.manager --dry-run  # 결정만 보고 적용 안 함
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

import anthropic

from . import fleet as fleet_mod
from . import llm, net, state
from .config import env, load_config
from .publishers import blogger
from .state import KST
from .trends.base import similarity

log = logging.getLogger("manager")
REPORT_PATH = fleet_mod.FLEET_DATA / "manager_report.md"

# 운영 규칙 — 모델이 무엇을 제안하든 코드가 이 범위 안에서만 적용합니다.
RULES = {
    "pause_after_consecutive_failures": 3,
    "pause_if_zero_live_days": 7,          # 7일간 공개 0건이고 실행은 5회 이상이면 중지 검토
    "max_changes_per_day": 5,
    "cost_alert_per_blog_week_usd": 12.0,  # 하루 2건×7일×$0.40 ≈ $5.6 가 정상 (글 1건 $0.35~0.40, 2026-09 실측)
    "duplicate_title_similarity": 0.6,
}

SYSTEM = """당신은 여러 개의 한국어 이슈 블로그를 운영하는 자동화 시스템의 관리자입니다. \
사람이 매일 보지 않으므로 당신의 판단이 곧 운영입니다. 다만 할 수 있는 행동은 아래 세 가지뿐이고, \
코드가 규칙으로 다시 검증하므로 규칙 밖의 제안은 버려집니다.

## 행동
- pause: 블로그 실행 중지. 연속 실패가 {pause_fail}회 이상이거나, {zero_days}일간 공개 0건인데 실행은 계속 돈 경우.
- resume: 이전에 관리자가 중지한 블로그를 다시 켬. 원인이 해소됐다고 볼 근거가 있을 때만.
- note: 사람이 다음 주간 보고에서 봐야 할 메모 (행동은 하지 않음).

발행량(하루 글 수)은 당신이 바꿀 수 없습니다. 블로그·계정 나이에 따른 램프업 규칙이 정하며, \
지표의 `slots_today`(오늘 켜진 슬롯)와 `ramp`(계정 단계)가 그 결과입니다. 증량을 제안하지 마세요. \
보류·거부가 많은 블로그가 있으면 note 로 알리세요.
`account_halted` 가 있는 계정은 Blogger 가 쓰기를 거부해 멈춘 상태입니다. 사람이 풀어야 하므로 note 로 알리기만 하세요.

## 판단 원칙
- 하루 최대 {max_changes}건만 바꿉니다. 확신이 없으면 note 로 남기고 바꾸지 않습니다.
- 사람이 수동으로 끈 블로그(by=manual)는 건드리지 않습니다.
- 여러 블로그가 같은 날 비슷한 제목을 냈다면 그것은 함대 전체의 위험(중복 콘텍츠)입니다. \
해당 블로그들의 subject(고유 주제)를 다르게 설정하라고 note 로 권고하세요. 특히 **같은 계정** 안에서 \
겹치면 더 위험합니다 — 구글의 제한은 계정 단위로 붙습니다.
- 지표에 `history_error` 가 있는 블로그는 이력 파일이 깨져 실행이 멈춘 상태입니다. 조치 대신 note 로 \
사람이 복구해야 한다고 알리세요.
- 비용이 기준을 넘는 블로그는 note 로 알리세요.

## 출력
JSON 하나만. 설명이나 코드 펜스 없이.
{{"summary": "한 줄 총평",
  "actions": [{{"blog": "id", "action": "pause|resume|note", "value": null, "reason": "이유"}}]}}"""


def _blog_metrics(fleet: fleet_mod.Fleet, blog: fleet_mod.Blog, use_api: bool, now: datetime) -> dict:
    try:
        hist = state.recent(state.load(blog.history_path), 7)
    except state.HistoryCorrupted as exc:
        # 이력이 깨진 블로그는 실행이 멈춰 있습니다. 지표 대신 그 사실을 올려 보냅니다.
        return {
            "id": blog.id, "name": blog.name, "slot": blog.slot, "account": blog.account,
            "enabled": blog.enabled, "paused_by": None, "slots_today": [],
            "runs_7d": 0, "runs_failed_7d": 0, "consecutive_failures": 99,
            "live_7d": 0, "draft_7d": 0, "rejected_7d": 0, "avg_score_7d": None,
            "cost_7d": 0.0, "titles_today": [], "blogger_live_total": None,
            "blog_url": None, "gsc_clicks_7d": None,
            "history_error": str(exc)[:160],
        }
    by = Counter(h.get("status") for h in hist)
    scores = [h["review_score"] for h in hist if h.get("review_score") is not None]
    runs = fleet_mod.load_runs(blog)
    # 실행 기록 키는 "<모드>:<날짜>[:<슬롯>]" 입니다 (planned:2026-09-22:08:00, trend:2026-09-20 …).
    # 예전엔 "trend:" 로 시작하는 것만 셌는데, 블로그들이 planned 모드로 바뀐 뒤로 실행이 0회로 잡혀
    # "7일간 실행 기록 없음" 오경보가 났고(2026-09-24), 더 나쁘게는 연속 실패 3회 → 자동 중지
    # 규칙도 조용히 꺼져 있었습니다. 모드와 상관없이 전부 셉니다.
    all_runs = sorted(runs.items(), key=lambda kv: kv[1].get("at", ""))
    consecutive_fail = 0
    for _, r in reversed(all_runs):
        if r.get("ok"):
            break
        consecutive_fail += 1
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    recent_runs = [v for k, v in all_runs if k.split(":")[1] >= cutoff]
    ms = fleet_mod.load_manager_state().get("blogs", {}).get(blog.id, {})

    m = {
        "id": blog.id, "name": blog.name, "slot": blog.slot, "account": blog.account,
        "enabled": blog.enabled,
        "paused_by": ms.get("by") if ms.get("enabled") is False else None,
        # 오늘 실제로 켜진 슬롯 (램프업·계정 상한·비상정지 반영). 하루 글 수 = 이 개수.
        "slots_today": fleet_mod.planned_slots(fleet, now).get(blog.id, []),
        "blog_age_days": fleet_mod.blog_age_days(fleet, blog, now),
        "runs_7d": len(recent_runs), "runs_failed_7d": sum(not r.get("ok") for r in recent_runs),
        "consecutive_failures": consecutive_fail,
        "live_7d": by.get("live", 0), "draft_7d": by.get("draft", 0), "rejected_7d": by.get("rejected", 0),
        "avg_score_7d": round(sum(scores) / len(scores), 1) if scores else None,
        "cost_7d": round(sum(float(h.get("cost_usd") or 0) for h in hist), 2),
        "titles_today": [h.get("title", "") for h in hist if h.get("posted_at", "").startswith(now.strftime("%Y-%m-%d"))],
        "blogger_live_total": None, "blog_url": None, "gsc_clicks_7d": None,
    }
    if use_api:
        try:
            # 계정별 자격증명으로 바꾼 뒤 토큰을 받습니다. 토큰 캐시가 자격증명별이라
            # 여러 계정을 오가도 서로의 토큰을 쓰지 않습니다.
            fleet_mod.apply_env(fleet, blog)
            token = blogger._access_token()
            info = net.get(
                f"{blogger.API_BASE}/blogs/{blog.blog_id}",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
            m["blogger_live_total"] = info.get("posts", {}).get("totalItems")
            m["blog_url"] = info.get("url")
            if m["blog_url"]:
                m["gsc_clicks_7d"] = _gsc_clicks(token, m["blog_url"], 7)
        except Exception as exc:  # noqa: BLE001
            m["blogger_error"] = str(exc)[:120]
    return m


def _gsc_clicks(token: str, site: str, days: int) -> int | None:
    try:
        from scripts.weekly_report import _gsc_property, _gsc_query, _gsc_sites  # type: ignore
    except Exception:
        return None
    registered = _gsc_sites(token)
    if not registered:
        return None
    prop = _gsc_property(site, registered)
    if not prop:
        return None
    end = datetime.now(KST).date() - timedelta(days=2)
    try:
        resp = _gsc_query(token, prop, end - timedelta(days=days), end, row_limit=1000)
        if resp.status_code != 200:
            return None
        return int(sum(r["clicks"] for r in resp.json().get("rows", [])))
    except Exception:
        return None


def _find_duplicates(metrics: list[dict]) -> list[str]:
    """같은 날 서로 다른 블로그의 제목이 비슷하면 경고."""
    out = []
    titles = [(m["id"], t) for m in metrics for t in m["titles_today"]]
    for i, (b1, t1) in enumerate(titles):
        for b2, t2 in titles[i + 1:]:
            if b1 != b2 and similarity(t1, t2) >= RULES["duplicate_title_similarity"]:
                out.append(f"{b1} ↔ {b2}: 「{t1[:40]}」 ≈ 「{t2[:40]}」")
    return out


def _rule_based(metrics: list[dict]) -> list[dict]:
    """모델 없이도 반드시 적용할 최소 규칙."""
    actions = []
    for m in metrics:
        if m["enabled"] and m["consecutive_failures"] >= RULES["pause_after_consecutive_failures"]:
            actions.append({"blog": m["id"], "action": "pause", "value": None,
                            "reason": f"연속 {m['consecutive_failures']}회 실패 (규칙)"})
    return actions


def account_overview(fleet, now: datetime) -> list[dict]:
    """계정별 램프업 단계와 비상정지 상태. 보고서와 관리 모델 입력에 씁니다."""
    st = fleet_mod.load_account_state()
    plan = fleet_mod.planned_slots(fleet, now, st)
    out = []
    for acc in fleet.accounts:
        blogs = fleet.blogs_of(acc)
        if not blogs:
            continue
        start = fleet_mod._ramp_start(fleet, acc, st)
        out.append({
            "account": acc,
            "account_halted": fleet_mod.account_halted(acc, st),
            "ramp": {
                "since": start.isoformat() if start else None,
                "age_days": (now.date() - start).days if start else None,
                "cap_today": fleet_mod.account_cap(fleet, acc, now, st),
                "ceiling": int(fleet.account_setting(acc, "max_live_per_day_account", 6)),
            },
            "blogs": len(blogs),
            "slots_today": sum(len(plan.get(b.id, [])) for b in blogs),
            "slots_configured": sum(len(b.slots) for b in blogs),
        })
    return out


def _ask_model(cfg: dict, metrics: list[dict], duplicates: list[str], alerts: list[str],
               accounts: list[dict] | None = None) -> dict:
    client = llm.client()
    system = SYSTEM.format(
        pause_fail=RULES["pause_after_consecutive_failures"], zero_days=RULES["pause_if_zero_live_days"],
        max_changes=RULES["max_changes_per_day"],
    )
    user = (
        "## 계정별 램프업·비상정지\n" + json.dumps(accounts or [], ensure_ascii=False, indent=1)
        + "\n\n## 블로그별 지표 (최근 7일)\n" + json.dumps(metrics, ensure_ascii=False, indent=1)
        + "\n\n## 중복 의심\n" + ("\n".join(f"- {d}" for d in duplicates) or "- 없음")
        + "\n\n## 코드가 찾은 경고\n" + ("\n".join(f"- {a}" for a in alerts) or "- 없음")
        + "\n\n위 규칙 안에서 오늘 할 행동을 JSON 으로 출력하세요."
    )
    resp = client.messages.create(
        model=cfg.get("manager", {}).get("model", "claude-sonnet-5"),
        max_tokens=6000,
        system=system,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": user}],
    )
    raw = llm.text_of(resp, "관리 판단")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"관리 모델 응답에 JSON 없음: {raw[:200]!r}")
    data = json.loads(m.group(0))
    data["_usage"] = (resp.usage.input_tokens, resp.usage.output_tokens)
    return data


def _validate_actions(actions: list[dict], metrics: list[dict]) -> tuple[list[dict], list[str]]:
    """모델 제안을 규칙으로 걸러 실제 적용할 행동만 남깁니다."""
    by_id = {m["id"]: m for m in metrics}
    ok, rejected = [], []
    changes = 0
    for a in actions:
        blog = a.get("blog")
        act = a.get("action")
        m = by_id.get(blog)
        if act == "note":
            ok.append(a)
            continue
        if m is None:
            rejected.append(f"{blog}: 없는 블로그")
            continue
        if changes >= RULES["max_changes_per_day"]:
            rejected.append(f"{blog}/{act}: 하루 변경 한도 초과")
            continue
        if m.get("paused_by") == "manual":
            rejected.append(f"{blog}/{act}: 사람이 수동으로 끈 블로그")
            continue
        if act == "pause":
            zero_live = m["live_7d"] == 0 and m["runs_7d"] >= 5
            if not (m["consecutive_failures"] >= RULES["pause_after_consecutive_failures"] or zero_live):
                rejected.append(f"{blog}/pause: 근거 부족 (연속실패 {m['consecutive_failures']}, 7일 공개 {m['live_7d']})")
                continue
        elif act == "resume":
            if m["enabled"] or m.get("paused_by") != "manager":
                rejected.append(f"{blog}/resume: 관리자가 중지한 블로그가 아님")
                continue
        else:
            rejected.append(f"{blog}: 알 수 없는 행동 {act}")
            continue
        ok.append(a)
        changes += 1
    return ok, rejected


def _apply(actions: list[dict], now: datetime) -> None:
    st = fleet_mod.load_manager_state()
    blogs = st.setdefault("blogs", {})
    stamp = now.strftime("%Y-%m-%d")
    for a in actions:
        if a["action"] == "note":
            continue
        entry = blogs.setdefault(a["blog"], {})
        if a["action"] == "pause":
            entry.update({"enabled": False, "by": "manager", "note": f"관리자 중지 {stamp}: {a['reason'][:80]}"})
        elif a["action"] == "resume":
            entry.update({"enabled": True, "by": "manager", "note": f"관리자 재개 {stamp}"})
    st["updated_at"] = now.isoformat(timespec="seconds")
    fleet_mod.save_manager_state(st)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="함대 관리 에이전트")
    parser.add_argument("--dry-run", action="store_true", help="결정만 보고 적용하지 않음")
    parser.add_argument("--no-model", action="store_true", help="규칙 기반만 (모델 호출 안 함)")
    args = parser.parse_args(argv)

    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")

    cfg = load_config()
    fleet = fleet_mod.load_fleet()
    now = datetime.now(KST)

    # 계정별로 자격증명이 있는지 먼저 봅니다. 없는 계정 블로그는 사이트 지표를 건너뜁니다.
    cred = fleet_mod.credential_report(fleet)
    usable = {acc for acc, missing in cred.items() if not missing}
    metrics = [_blog_metrics(fleet, b, b.account in usable, now) for b in fleet.blogs]

    duplicates = _find_duplicates(metrics)
    alerts: list[str] = []
    for acc, missing in cred.items():
        if missing:
            alerts.append(f"계정 '{acc}' 자격증명 없음: {', '.join(missing)} — 이 계정 블로그는 실행되지 않습니다")
    accounts = account_overview(fleet, now)
    for a in accounts:
        if a["account_halted"]:
            alerts.append(
                f"계정 '{a['account']}' 비상정지 중: {a['account_halted']} — "
                f"원인 확인 후 python scripts/account_cli.py resume {a['account']}"
            )
    for m in metrics:
        if m.get("history_error"):
            alerts.append(f"{m['id']}: 이력 파일 손상 — {m['history_error']}")
        # 램프업 대기 중(켜진 슬롯 없음)이거나 막 시작한 블로그는 실행 기록이 없는 게 정상입니다.
        if (m["enabled"] and m["runs_7d"] == 0 and not m.get("history_error")
                and m.get("slots_today") and m.get("blog_age_days", 99) >= 2):
            alerts.append(f"{m['id']}: 7일간 실행 기록 없음 (슬롯 {m['slot']}) — 함대 실행기가 멈췄는지 확인")
        if m["cost_7d"] > RULES["cost_alert_per_blog_week_usd"]:
            alerts.append(f"{m['id']}: 7일 비용 ${m['cost_7d']} — 기준 ${RULES['cost_alert_per_blog_week_usd']} 초과")
        if m["enabled"] and m["consecutive_failures"] >= 2:
            alerts.append(f"{m['id']}: 연속 실패 {m['consecutive_failures']}회")
    alerts += [f"중복 의심: {d}" for d in duplicates]

    proposed: list[dict] = _rule_based(metrics)
    summary = "규칙 기반 판단만 적용"
    usage = None
    if not args.no_model and env("ANTHROPIC_API_KEY"):
        try:
            data = _ask_model(cfg, metrics, duplicates, alerts, accounts)
            summary = str(data.get("summary", ""))[:300]
            usage = data.get("_usage")
            # 규칙 행동과 모델 행동을 합치되 같은 블로그/행동은 하나만
            seen = {(a["blog"], a["action"]) for a in proposed}
            for a in data.get("actions", []) or []:
                key = (a.get("blog"), a.get("action"))
                if key not in seen:
                    proposed.append(a)
                    seen.add(key)
        except Exception as exc:  # noqa: BLE001
            log.exception("관리 모델 호출 실패 — 규칙 기반만 적용")
            alerts.append(f"관리 모델 호출 실패: {exc}")

    actions, rejected = _validate_actions(proposed, metrics)
    if not args.dry_run and actions:
        _apply(actions, now)

    # 보고서
    lines = [f"# 함대 관리 보고 — {now.strftime('%Y-%m-%d %H:%M')} KST", "", f"> {summary}", ""]
    if alerts:
        lines.append("## ⚠️ 경고")
        lines += [f"- {a}" for a in alerts]
        lines.append("")
    lines.append("## 조치" + (" (dry-run, 미적용)" if args.dry_run else ""))
    real = [a for a in actions if a["action"] != "note"]
    notes = [a for a in actions if a["action"] == "note"]
    lines += [f"- **{a['blog']}** {a['action']}{'=' + str(a['value']) if a.get('value') is not None else ''} — {a['reason']}" for a in real] or ["- 없음"]
    if notes:
        lines.append("")
        lines.append("## 메모")
        lines += [f"- {a['blog']}: {a['reason']}" for a in notes]
    if rejected:
        lines.append("")
        lines.append("## 규칙에 걸려 버린 제안")
        lines += [f"- {r}" for r in rejected]
    lines += ["", "## 계정별 발행량 (램프업)", "",
              "| 계정 | 상태 | 램프업 기준일 | 경과 | 오늘 상한 / 최종 | 켜진 슬롯 / 적어둔 슬롯 | 블로그 |",
              "|---|---|---|---:|---:|---:|---:|"]
    for a in accounts:
        r = a["ramp"]
        lines.append(
            f"| {a['account']} | {'⛔ 비상정지' if a['account_halted'] else '정상'} | {r['since'] or '미지정'} | "
            f"{r['age_days'] if r['age_days'] is not None else '-'}일 | {r['cap_today']} / {r['ceiling']} | "
            f"{a['slots_today']} / {a['slots_configured']} | {a['blogs']} |"
        )
    lines += ["", "## 블로그별 지표 (7일)", "",
              "| 계정 | 블로그 | 슬롯 | 상태 | 실행/실패 | 공개/보류/거부 | 검수평균 | 비용 | Blogger 총 글 | GSC 클릭 |",
              "|---|---|---|---|---|---|---:|---:|---:|---:|"]
    for m in sorted(metrics, key=lambda x: (x.get("account", ""), x["slot"])):
        st = "켜짐" if m["enabled"] else f"꺼짐({m.get('paused_by') or '?'})"
        if m.get("history_error"):
            st = "⚠️ 이력 손상"
        lines.append(
            f"| {m.get('account', '?')} | {m['name']} ({m['id']}) | {m['slot']} | {st} | "
            f"{m['runs_7d']}/{m['runs_failed_7d']} | "
            f"{m['live_7d']}/{m['draft_7d']}/{m['rejected_7d']} | {m['avg_score_7d'] or '-'} | ${m['cost_7d']} | "
            f"{m['blogger_live_total'] if m['blogger_live_total'] is not None else '-'} | {m['gsc_clicks_7d'] if m['gsc_clicks_7d'] is not None else '-'} |"
        )
    total_cost = sum(m["cost_7d"] for m in metrics)
    per_account: dict[str, int] = {}
    for m in metrics:
        per_account[m.get("account", "?")] = per_account.get(m.get("account", "?"), 0) + m["live_7d"]
    lines += [
        "",
        f"**계정 {len(per_account)}개 · 블로그 {len(metrics)}개 · 7일 총 비용 약 ${total_cost:.2f} · "
        f"7일 공개 {sum(m['live_7d'] for m in metrics)}건** "
        f"(계정별: {', '.join(f'{a} {n}건' for a, n in sorted(per_account.items()))})",
    ]
    if usage:
        lines.append(f"<sub>관리 모델 토큰 {usage[0]}/{usage[1]}</sub>")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

    # 종료 코드 2 = 사람이 볼 것이 있음 (워크플로가 이슈를 만듭니다)
    return 2 if (alerts or real) else 0


if __name__ == "__main__":
    raise SystemExit(main())
