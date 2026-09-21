"""함대 실행기 — 10분마다 호출되어 '지금 돌 차례인' 블로그를 슬롯 순서대로 하나씩 돌립니다.

    python -m src.fleet_run              # 슬롯이 지났고 오늘 아직 안 돈 블로그 실행
    python -m src.fleet_run --blog X     # 특정 블로그만 (슬롯 무시)
    python -m src.fleet_run --all        # 켜진 블로그 전부 (슬롯 무시, 오늘 이미 돈 것도 다시)
    python -m src.fleet_run --dry-run    # 누가 돌 차례인지만 출력

각 블로그는 src.main 을 --blog 옵션으로 호출한 것과 같습니다. 한 블로그가 실패해도 다음
블로그는 계속 돕니다. 결과는 data/fleet/last_fleet_run.md 에 남고 GitHub Actions 요약에 붙습니다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import fleet as fleet_mod
from . import guard
from . import main as single
from .config import DATA_DIR
from .state import KST

log = logging.getLogger("fleet")
REPORT_PATH = fleet_mod.FLEET_DATA / "last_fleet_run.md"


def _run_blog(blog: fleet_mod.Blog, mode: str, extra: list[str]) -> tuple[bool, str]:
    # 슬롯 하나당 글 1건입니다. 하루치를 한 번에 몰아 올리지 않기 위한 설계입니다
    # (2026-09-20: 한 블로그가 2분 30초에 3건 → 계정 API 차단).
    argv = ["--blog", blog.id, "--mode", mode, "--count", "1", *extra]
    try:
        code = single.main(argv)
    except SystemExit as exc:  # argparse 등
        code = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 — 한 블로그의 예외가 함대를 멈추면 안 됩니다
        log.exception("[%s] 실행 중 예외", blog.id)
        return False, f"예외: {exc}"
    summary = ""
    if blog.report_path.exists():
        for line in blog.report_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("**"):
                summary = line.strip("* ")
    return code == 0, summary or f"종료 코드 {code}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="블로그 함대 실행")
    parser.add_argument("--blog", help="이 블로그만 실행 (슬롯 무시)")
    parser.add_argument("--all", action="store_true", help="켜진 블로그 전부 실행 (슬롯 무시)")
    parser.add_argument("--dry-run", action="store_true", help="실행 대상만 표시")
    parser.add_argument("--target", choices=["blogger", "local"], help="발행 대상 덮어쓰기 (테스트용)")
    parser.add_argument("--count", type=int, help="글 개수 덮어쓰기")
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

    # 자격증명 점검 — 계정별 변수가 없으면 default 계정 토큰으로 엉뚱한 블로그에 쓰게 됩니다.
    cred = fleet_mod.credential_report(fleet)
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

    # 계정별 상한 — 구글의 제한은 계정에 붙습니다. 계정을 나누면 상한도 따로 계산됩니다.
    # 2026-09-20 차단 당시 블로그별로는 상한 안팎이었지만 계정 합계가 하루 16건이었습니다.
    account_left: dict[str, int] = {}
    if args.target != "local":
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

    failures = 0
    for blog, slot in targets:
        left = account_left.get(blog.account)
        if left is not None and left <= 0:
            log.info("[%s] 계정 '%s' 상한 도달 — 이 슬롯은 다음 기회로 미룹니다.", blog.id, blog.account)
            report.append(f"- ⏸️ {blog.name} ({slot}) — 계정 '{blog.account}' 상한 도달로 보류")
            continue
        mode = blog_mode(blog)
        log.info("==== [%s] %s 실행 (슬롯 %s) ====", blog.id, mode, slot)
        ok, summary = _run_blog(blog, mode, extra)
        fleet_mod.mark_ran(blog, mode, ok, summary, now, slot=slot)
        if blog.account in account_left and "공개 1건" in summary:
            account_left[blog.account] -= 1
        icon = "✅" if ok else "❌"
        report.append(f"- {icon} **{blog.name}** ({blog.id} {slot}, {mode}, 계정 {blog.account}) — {summary}")
        if not ok:
            failures += 1

    report += ["", f"**슬롯 {len(targets)}개 처리 · 실패 {failures}건**"]
    _write(report)
    return 1 if failures and failures == len(targets) else 0


def _write(lines: list[str]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
