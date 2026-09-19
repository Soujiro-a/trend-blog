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
from . import main as single
from .config import DATA_DIR
from .state import KST

log = logging.getLogger("fleet")
REPORT_PATH = fleet_mod.FLEET_DATA / "last_fleet_run.md"


def _run_blog(blog: fleet_mod.Blog, mode: str, extra: list[str]) -> tuple[bool, str]:
    argv = ["--blog", blog.id, "--mode", mode, *extra]
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

    if args.blog:
        targets = [fleet.get(args.blog)]
    elif args.all:
        targets = [b for b in fleet.blogs if b.enabled]
    else:
        targets = fleet_mod.due_blogs(fleet, now, mode="trend")

    modes = ["trend"]
    if args.mode == "evergreen":
        modes = ["evergreen"]
    elif args.mode == "auto" and now.weekday() == int(fleet.settings.get("evergreen_weekday", -1)):
        modes.append("evergreen")

    report = [f"# 함대 실행 — {now.strftime('%Y-%m-%d %H:%M')} KST", ""]
    if not targets:
        msg = "실행할 블로그 없음 (모두 오늘 완료 또는 슬롯 전)"
        log.info(msg)
        report.append(f"- {msg}")
        _write(report)
        return 0

    report.append(f"대상 {len(targets)}개: " + ", ".join(f"{b.id}({b.slot})" for b in targets))
    report.append("")
    if args.dry_run:
        for b in targets:
            print(f"  · {b.id}  슬롯 {b.slot}  {b.name}")
        _write(report)
        return 0

    extra: list[str] = []
    if args.target:
        extra += ["--target", args.target]
    if args.count:
        extra += ["--count", str(args.count)]

    failures = 0
    for blog in targets:
        for mode in modes:
            if args.blog is None and not args.all and fleet_mod.ran_today(blog, mode, now):
                continue
            log.info("==== [%s] %s 실행 (슬롯 %s) ====", blog.id, mode, blog.slot)
            ok, summary = _run_blog(blog, mode, extra)
            fleet_mod.mark_ran(blog, mode, ok, summary, now)
            icon = "✅" if ok else "❌"
            report.append(f"- {icon} **{blog.name}** ({blog.id}, {mode}) — {summary}")
            if not ok:
                failures += 1

    report += ["", f"**완료 {len(targets)}개 블로그 · 실패 {failures}건**"]
    _write(report)
    return 1 if failures and failures == len(targets) else 0


def _write(lines: list[str]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
