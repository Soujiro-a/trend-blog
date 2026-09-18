"""실시간 이슈 블로그 자동화 진입점.

    python -m src.main                 # 설정대로 수집 → 작성 → 임시저장
    python -m src.main --dry-run       # 키워드 수집/필터 결과만 확인 (API 호출 없음)
    python -m src.main --target local  # Blogger 대신 out/ 폴더에 HTML 저장
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import filters, publishers, research, state, trends, writer
from .config import DATA_DIR, load_config
from .state import KST

log = logging.getLogger("trend-blog")

REPORT_PATH = DATA_DIR / "last_run.md"


def _setup_logging(verbose: bool) -> None:
    # 한국어 윈도우 콘솔은 기본 인코딩이 cp949 라, 로그에 섞인 기호에서
    # UnicodeEncodeError 가 납니다. 출력 스트림을 UTF-8 로 맞춰둡니다.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def _write_report(lines: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("실행 보고서: %s", REPORT_PATH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실시간 이슈 블로그 자동 작성")
    parser.add_argument("--dry-run", action="store_true", help="키워드 수집·필터까지만 실행")
    parser.add_argument("--target", choices=sorted(publishers.TARGETS), help="발행 대상 덮어쓰기")
    parser.add_argument("--count", type=int, help="작성할 글 개수 덮어쓰기")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    cfg = load_config()

    target_name = args.target or cfg["publish"]["target"]
    want = args.count or cfg["run"]["posts_per_run"]
    now = datetime.now(KST)
    date_str = now.strftime("%Y년 %m월 %d일")

    report = [
        f"# 실행 보고 — {now.strftime('%Y-%m-%d %H:%M')} KST",
        "",
    ]

    # 1) 수집
    keyword_items, headline_items = trends.collect(cfg)
    if len(keyword_items) < cfg["run"]["min_keywords"]:
        log.error(
            "수집된 키워드가 %d개뿐입니다(최소 %d개). 이번 실행은 건너뜁니다.",
            len(keyword_items),
            cfg["run"]["min_keywords"],
        )
        report.append(f"- ❌ 키워드 수집 부족 ({len(keyword_items)}개). 실행 중단.")
        _write_report(report)
        return 1

    by_source: dict[str, int] = {}
    for item in keyword_items + headline_items:
        by_source[item.source] = by_source.get(item.source, 0) + 1
    report.append("## 수집")
    report += [f"- {s}: {n}개" for s, n in sorted(by_source.items())]
    report.append("")

    # 2) 통합 · 필터 · 중복 제거
    candidates = trends.aggregate(cfg, keyword_items, headline_items)
    kept, rejected = filters.apply(cfg, candidates)
    history = state.load()
    fresh, skipped = state.filter_seen(cfg, kept, history)

    log.info(
        "후보 %d개 → 필터 통과 %d개 → 신규 %d개",
        len(candidates), len(kept), len(fresh),
    )

    report.append("## 후보 (상위 10)")
    for c in fresh[:10]:
        report.append(
            f"- **{c.keyword}** (점수 {c.score}, 소스 {len(c.sources)}개)"
        )
    if rejected:
        report.append("")
        report.append("## 걸러낸 키워드")
        report += [f"- {r.keyword} — {r.reason}" for r in rejected[:15]]
    if skipped:
        report.append("")
        report.append("## 최근 중복으로 건너뛴 키워드")
        report += [f"- {k} (이전: {prev})" for k, prev in skipped[:10]]
    report.append("")

    if args.dry_run:
        log.info("--dry-run: 글 작성은 건너뜁니다.")
        for c in fresh[:want]:
            print(f"  · {c.keyword}  점수={c.score}  소스={list(c.sources)}")
        _write_report(report)
        return 0

    if not fresh:
        log.warning("쓸 만한 새 키워드가 없습니다.")
        report.append("- ⚠️ 새 키워드 없음. 작성 건너뜀.")
        _write_report(report)
        return 0

    # 3) 리서치 → 작성 → 임시저장
    publisher = publishers.get(target_name)
    written = 0
    total_cost = 0.0
    report.append("## 작성 결과")

    for candidate in fresh:
        if written >= want:
            break

        log.info("---- [%s] 작성 시작 (점수 %s)", candidate.keyword, candidate.score)
        try:
            refs = research.gather(cfg, candidate)
            if len(refs) < 2:
                log.info("[%s] 참고 기사가 %d건뿐이라 건너뜁니다.", candidate.keyword, len(refs))
                report.append(f"- ⏭️ {candidate.keyword} — 참고 기사 부족({len(refs)}건)")
                continue

            context = research.to_context(cfg, candidate, refs)
            article = writer.write(cfg, candidate, context, refs, date_str)

        except writer.SkippedByModel as exc:
            log.info("[%s] 모델이 작성을 건너뜀: %s", candidate.keyword, exc)
            report.append(f"- ⏭️ {candidate.keyword} — 모델 판단으로 제외")
            continue
        except Exception as exc:
            log.exception("[%s] 작성 실패: %s", candidate.keyword, exc)
            report.append(f"- ❌ {candidate.keyword} — 작성 실패: {exc}")
            continue

        try:
            post = publisher.publish(cfg, article)
        except Exception as exc:
            log.exception("[%s] 발행 실패: %s", candidate.keyword, exc)
            report.append(f"- ❌ {article.title} — 발행 실패: {exc}")
            continue

        history = state.record(
            history,
            keyword=candidate.keyword,
            title=article.title,
            post_id=str(post.get("id", "")),
            url=post.get("url", ""),
            status="draft" if cfg["publish"].get("draft_only", True) else "live",
        )
        state.save(history)

        written += 1
        total_cost += article.cost_usd
        log.info(
            "[%s] 완료 — 토큰 %d/%d, 약 $%.3f",
            article.title, article.input_tokens, article.output_tokens, article.cost_usd,
        )
        report.append(
            f"- ✅ **{article.title}**  \n"
            f"  키워드: {candidate.keyword} · 태그: {', '.join(article.labels)} · "
            f"비용 약 ${article.cost_usd:.3f}"
        )

    report += [
        "",
        f"**임시저장 {written}건 / 예상 비용 약 ${total_cost:.2f}**",
    ]
    _write_report(report)

    log.info("끝. 임시저장 %d건, 예상 비용 약 $%.2f", written, total_cost)
    return 0 if written else 2


if __name__ == "__main__":
    raise SystemExit(main())
