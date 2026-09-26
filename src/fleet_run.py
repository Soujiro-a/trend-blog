"""함대 실행기 — 10분마다 호출되어 '지금 돌 차례인' 블로그를 슬롯 순서대로 하나씩 돌립니다.

    python -m src.fleet_run                            # 슬롯이 지났고 오늘 아직 안 돈 블로그 실행
    python -m src.fleet_run --blog X                   # 특정 블로그만 (슬롯 무시)
    python -m src.fleet_run --all                      # 켜진 블로그 전부 (슬롯 무시, 오늘 이미 돈 것도 다시)
    python -m src.fleet_run --dry-run                  # 누가 돌 차례인지만 출력
    python -m src.fleet_run --blog X --target local    # 시험: Blogger 대신 out/ 에 저장, data/ 는 안 건드림

각 블로그는 src.main 을 --blog 옵션으로 호출한 것과 같습니다. 슬롯 하나에 글 한 건이라 글 수를 바꾸는
옵션은 두지 않습니다. 한 블로그가 실패해도 다음 블로그는 계속 돕니다.
결과는 data/fleet/last_fleet_run.md 에 남고 GitHub Actions 요약에 붙습니다.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import datetime

from . import fleet as fleet_mod
from . import guard
from . import main as single
from .config import DATA_DIR
from .state import KST

log = logging.getLogger("fleet")
REPORT_PATH = fleet_mod.FLEET_DATA / "last_fleet_run.md"


# Anthropic 계정 한도/잔액 문제는 블로그를 바꿔 다시 시도해도 똑같이 실패합니다.
# 이런 경우 남은 슬롯까지 전부 돌며 실패 기록을 남기면, 한도가 풀렸을 때 그 슬롯들이
# "오늘 이미 처리함"으로 남아 건너뛰게 됩니다. 그래서 함대 실행을 즉시 중단하고 기록도 남기지 않습니다.
_FATAL_PATTERNS = (
    "usage limit",          # You have reached your specified API usage limits
    "credit balance",       # Your credit balance is too low
    "authentication_error",
    "invalid x-api-key",
)


def _is_fatal(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _FATAL_PATTERNS)


HALT_ALERT_PATH = fleet_mod.FLEET_DATA / "halt_alert.md"

# 테스트에서 바꿔 끼울 수 있게 모듈 변수로 둡니다.
_clock = time.monotonic
_sleep = time.sleep


def _run_blog(blog: fleet_mod.Blog, mode: str, extra: list[str]) -> tuple[bool, str, int]:
    # 슬롯 하나당 글 1건입니다. 하루치를 한 번에 몰아 올리지 않기 위한 설계입니다
    # (2026-09-20: 한 블로그가 2분 30초에 3건 → 계정 API 차단).
    argv = ["--blog", blog.id, "--mode", mode, "--count", "1", *extra]
    try:
        code = single.main(argv)
    except SystemExit as exc:  # argparse 등
        code = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 — 한 블로그의 예외가 함대를 멈추면 안 됩니다
        log.exception("[%s] 실행 중 예외", blog.id)
        return False, f"예외: {exc}", 1
    summary = ""
    if blog.report_path.exists():
        for line in blog.report_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("**") or line.startswith("- ⛔"):
                summary = line.strip("*- ")
    return code == 0, summary or f"종료 코드 {code}", code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="블로그 함대 실행")
    parser.add_argument("--blog", help="이 블로그만 실행 (슬롯 무시)")
    parser.add_argument("--all", action="store_true", help="켜진 블로그 전부 실행 (슬롯 무시)")
    parser.add_argument("--dry-run", action="store_true", help="실행 대상만 표시")
    parser.add_argument("--target", choices=["blogger", "local"], help="발행 대상 덮어쓰기 (local = 시험, 기록 안 남김)")
    parser.add_argument("--mode", choices=["trend", "evergreen", "auto"], default="auto",
                        help="auto: 실시간 글 + (evergreen_weekday 인 날) 장수 글")
    args = parser.parse_args(argv)

    single._setup_logging(False)
    fleet = fleet_mod.load_fleet()
    now = datetime.now(KST)

    def blog_mode(b: fleet_mod.Blog) -> str:
        return b.content_mode if args.mode == "auto" else args.mode

    if args.blog:
        b = fleet.get(args.blog)
        targets = [(b, b.slots[0])]
    elif args.all:
        targets = [(b, b.slots[0]) for b in fleet.blogs if b.enabled]
    else:
        # 블로그마다 content_mode 가 다를 수 있어 모드별로 모아 합칩니다.
        seen: set[tuple[str, str]] = set()
        targets = []
        for b in fleet.blogs:
            for cand_b, slot in fleet_mod.due_slots(fleet, now, mode=blog_mode(b)):
                if cand_b.id == b.id and (b.id, slot) not in seen:
                    seen.add((b.id, slot))
                    targets.append((cand_b, slot))
        targets.sort(key=lambda t: fleet_mod.to_minutes(t[1]))

    report = [f"# 함대 실행 — {now.strftime('%Y-%m-%d %H:%M')} KST", ""]
    if not targets:
        msg = "처리할 슬롯 없음 (모두 오늘 완료 또는 시각 전)"
        log.info(msg)
        report.append(f"- {msg}")
        _write(report)
        return 0

    by_account: dict[str, int] = {}
    for b, _ in targets:
        by_account[b.account] = by_account.get(b.account, 0) + 1
    report.append(
        f"대상 슬롯 {len(targets)}개 (계정별: {', '.join(f'{a} {n}' for a, n in sorted(by_account.items()))})"
    )
    report.append("")
    if args.dry_run:
        for b, s in targets:
            print(f"  · {b.id}  슬롯 {s}  {b.name}  [{blog_mode(b)}]  계정 {b.account}")
        _write(report)
        return 0

    extra: list[str] = []
    if args.target:
        extra += ["--target", args.target]
    # 로컬 시험은 Blogger 에 접속하지 않고, 슬롯도 처리했다고 기록하지 않습니다(실제 슬롯이 그날 건너뛰어지지 않게).
    local = args.target == "local"

    # 자격증명 점검 — 계정별 변수가 없으면 default 계정 토큰으로 엉뚱한 블로그에 쓰게 됩니다.
    cred = {} if local else fleet_mod.credential_report(fleet)
    bad_accounts = {acc for acc, missing in cred.items() if missing}
    if bad_accounts:
        report.append("## 자격증명 없음 (해당 계정 블로그는 건너뜁니다)")
        for acc in sorted(bad_accounts):
            report.append(f"- **{acc}**: {', '.join(cred[acc])} 없음")
        report.append("")
        for acc in bad_accounts:
            log.error("계정 '%s' 자격증명 없음: %s", acc, ", ".join(cred[acc]))
        targets = [(b, s) for b, s in targets if b.account not in bad_accounts]
        if not targets:
            report.append("- ⛔ 실행 가능한 슬롯 없음")
            _write(report)
            return 1

    # 비상정지된 계정 — 모델 비용이 나가기 전에 뺍니다. (due_slots 가 이미 빼지만 --blog/--all 경로도 막습니다)
    halted = {} if local else {b.account: why for b, _ in targets if (why := fleet_mod.account_halted(b.account))}
    if halted:
        report.append("## ⛔ 비상정지된 계정 (해당 계정 블로그는 건너뜁니다)")
        for acc, why in sorted(halted.items()):
            report.append(f"- **{acc}**: {why} — 풀기: `python scripts/account_cli.py resume {acc}`")
        report.append("")
        targets = [(b, s) for b, s in targets if b.account not in halted]
        if not targets:
            _write(report)
            return 0

    # 계정별 상한 — 구글의 제한은 계정에 붙습니다. 계정을 나누면 상한도 따로 계산됩니다.
    # 2026-09-20 차단 당시 블로그별로는 상한 안팎이었지만 계정 합계가 하루 16건이었습니다.
    account_left: dict[str, int] = {}
    if not local:
        for acc in sorted({b.account for b, _ in targets}):
            ab = guard.account_budget(fleet, acc, now)
            account_left[acc] = ab.allowed
            report.append(
                f"- 계정 **{acc}**: 오늘 공개 {ab.already_today}건 / 상한 {ab.daily_cap}건 → 여유 {ab.allowed}건"
            )
            if ab.blocked:
                log.warning("계정 '%s' 상한 도달: %s", acc, ab.reason)
        report.append("")
        if all(v <= 0 for v in account_left.values()):
            report.append("- ⛔ 모든 계정이 오늘 상한에 도달했습니다.")
            _write(report)
            return 0

    # 같은 계정의 발행 사이 간격. GitHub 예약 실행은 실제로 4~5시간마다 오기 때문에(10분이 아니라),
    # 밀린 슬롯을 한 번에 따라잡으면 **같은 계정 블로그 3곳이 4분 안에** 글을 올립니다(2026-09-25 14:02~14:06).
    # 슬롯을 20분씩 벌려 둔 의미가 사라지고, 새 블로그들이 몇 분 안에 몰아 올린 2026-09-19 패턴과 같아집니다.
    # 그래서 한 실행 안에서도 같은 계정은 account_gap_minutes(+무작위 0~10분) 이상 기다렸다 올립니다.
    # 다른 계정의 슬롯은 기다리지 않고 그 사이에 처리합니다. 실행 시간 예산을 넘기면 남은 슬롯은 다음 실행으로.
    gap_s = int(fleet.settings.get("account_gap_minutes", 30)) * 60
    budget_s = int(fleet.settings.get("run_budget_minutes", 150)) * 60
    started = _clock()
    next_ok: dict[str, float] = {}   # 계정 → 이 시각(_clock) 이후에 다음 글을 올려도 됨
    # 직전 실행이 방금 올렸을 수도 있으니, 이력의 마지막 공개 시각부터 셉니다.
    wall = datetime.now(KST)
    for acc in {b.account for b, _ in targets}:
        last = max((t for b in fleet.blogs_of(acc) if (t := fleet_mod.last_live_at(b))), default=None)
        if last:
            remain = gap_s - (wall - last).total_seconds()
            if remain > 0:
                next_ok[acc] = started + remain

    failures = 0
    processed = 0
    newly_halted: dict[str, str] = {}
    pending = list(targets)
    while pending:
        t_now = _clock()
        pick = next((t for t in pending if next_ok.get(t[0].account, 0.0) <= t_now), None)
        if pick is None:
            wait = min(next_ok[b.account] for b, _ in pending) - t_now
            if t_now + wait - started > budget_s:
                for b, s in pending:
                    report.append(f"- ⏳ {b.name} ({s}) — 같은 계정 발행 간격 대기 중 실행 시간 예산 초과, 다음 실행으로")
                break
            log.info("같은 계정 발행 간격 확보를 위해 %d분 기다립니다.", round(wait / 60))
            _sleep(wait)
            continue
        pending.remove(pick)
        blog, slot = pick
        if blog.account in newly_halted:
            report.append(f"- ⏸️ {blog.name} ({slot}) — 계정 '{blog.account}' 비상정지로 건너뜀")
            continue
        left = account_left.get(blog.account)
        if left is not None and left <= 0:
            log.info("[%s] 계정 '%s' 상한 도달 — 이 슬롯은 다음 기회로 미룹니다.", blog.id, blog.account)
            report.append(f"- ⏸️ {blog.name} ({slot}) — 계정 '{blog.account}' 상한 도달로 보류")
            continue
        mode = blog_mode(blog)
        log.info("==== [%s] %s 실행 (슬롯 %s) ====", blog.id, mode, slot)
        ok, summary, code = _run_blog(blog, mode, extra)
        processed += 1
        if code == single.EXIT_ACCOUNT_HALTED:
            # 슬롯은 처리했다고 표시하지 않습니다. 사람이 원인을 보고 풀면 그날 안에 이어서 돕니다.
            newly_halted[blog.account] = summary
            report.append(f"- ⛔ **{blog.name}** ({slot}) — {summary[:300]}")
            failures += 1
            continue
        if not ok and _is_fatal(summary):
            # 한도·인증 문제. 다른 블로그도 똑같이 실패하므로 여기서 멈추고,
            # 슬롯은 처리했다고 표시하지 않아 한도가 풀리면 그대로 이어집니다.
            log.error("Claude API 한도/인증 문제로 함대 실행을 중단합니다: %s", summary[:200])
            report.append(f"- ⛔ **{blog.name}** ({slot}) — {summary[:200]}")
            report.append("")
            report.append("**Claude API 한도 또는 인증 문제입니다. 남은 슬롯은 처리하지 않았고, "
                          "기록도 남기지 않아 해결되면 그대로 이어집니다.**")
            _write(report)
            return 1
        if not local:
            fleet_mod.mark_ran(blog, mode, ok, summary, datetime.now(KST), slot=slot)
        if "공개 1건" in summary and not local:
            if blog.account in account_left:
                account_left[blog.account] -= 1
            next_ok[blog.account] = _clock() + gap_s + random.uniform(0, 600)
        icon = "✅" if ok else "❌"
        report.append(f"- {icon} **{blog.name}** ({blog.id} {slot}, {mode}, 계정 {blog.account}) — {summary}")
        if not ok:
            failures += 1

    report += ["", f"**슬롯 {processed}/{len(targets)}개 처리 · 실패 {failures}건**"]
    _write(report)
    if newly_halted:
        # 워크플로가 이 파일을 보고 즉시 GitHub 이슈를 엽니다 (밤 관리 보고까지 기다리지 않음).
        lines = [f"# ⛔ 계정 비상정지 — {now.strftime('%Y-%m-%d %H:%M')} KST", ""]
        for acc, why in newly_halted.items():
            lines += [
                f"## 계정 `{acc}`", "", f"- 원인: {why[:500]}",
                "- 이 계정의 모든 블로그가 멈췄습니다. 글 작성(모델 비용)도 하지 않습니다.",
                "- Blogger 에 로그인해 경고·정책 알림을 확인하세요. API 쓰기 제한이면 이의신청 결과를 기다려야 합니다.",
                f"- 해결된 뒤: `python scripts/account_cli.py resume {acc}` → 커밋·푸시 (램프업은 처음 단계부터 다시 시작)",
                "",
            ]
        HALT_ALERT_PATH.write_text("\n".join(lines), encoding="utf-8")
        return 1
    return 1 if failures and failures == processed else 0


def _write(lines: list[str]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
