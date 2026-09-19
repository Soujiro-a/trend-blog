"""실시간 이슈 블로그 자동화 진입점.

    python -m src.main                     # 수집 → 작성 → 검수 → 공개/보류 (단일 블로그, .env 의 BLOGGER_BLOG_ID)
    python -m src.main --blog picktopic    # 함대(fleet/blogs.yaml)의 특정 블로그 컨텍스트로 실행
    python -m src.main --dry-run           # 키워드 수집/필터 결과만 확인 (API 호출 없음)
    python -m src.main --target local      # Blogger 대신 out/ 폴더에 HTML 저장
    python -m src.main --mode evergreen    # 장수 해설 글 작성 (주 1회용)
    python -m src.main --draft             # 검수 결과와 무관하게 전부 임시저장

흐름
----
  키워드 후보 → (함대: 블로그 주제 필터·다른 블로그 선점 제외) → 리서치 → 작성(Fable) → 검수(Sonnet) → 판정
    publish + 점수 ≥ min_score + 하루 공개 상한 안 → 공개
    hold / 상한 초과                                 → 임시저장 (사람이 나중에 봐도 되고 안 봐도 됨)
    reject                                           → 올리지 않음
  검수관이 '구매 의도 있음'으로 본 글에는 쿠팡파트너스 상품 링크를 넣습니다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import evergreen, filters, monetize, publishers, research, reviewer, state, trends, writer
from . import fleet as fleet_mod
from .config import DATA_DIR, load_config
from .state import KST

log = logging.getLogger("trend-blog")


@dataclass
class RunContext:
    """이 실행이 어느 블로그의 어떤 파일을 쓰는지. 함대 모드가 아니면 루트 data/ 를 씁니다."""

    history_path: Path = state.HISTORY_PATH
    report_path: Path = DATA_DIR / "last_run.md"
    blog: fleet_mod.Blog | None = None
    fleet: fleet_mod.Fleet | None = None
    claims: dict | None = None

    @property
    def persona(self) -> str:
        if not self.blog:
            return ""
        parts = []
        if self.blog.niche:
            parts.append(f"이 블로그의 주제 영역: {self.blog.niche}")
        if self.blog.persona:
            parts.append(self.blog.persona)
        return "\n".join(parts)


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

    if logging.getLogger().handlers:  # 함대 실행기가 이미 설정했으면 그대로
        return
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def _write_report(ctx: RunContext, lines: list[str]) -> None:
    ctx.report_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("실행 보고서: %s", ctx.report_path)


def decide(cfg: dict, review: reviewer.Review | None, live_so_far: int, force_draft: bool) -> str:
    """검수 결과와 상한을 보고 live / draft / reject 를 정합니다."""
    pub = cfg["publish"]
    if review is not None and review.verdict == "reject":
        return "reject"
    if force_draft or pub.get("mode", "draft") != "auto" or pub.get("draft_only", False):
        return "draft"
    if review is None:
        # 검수를 끈 상태에서 auto 모드 → 그대로 공개 (사용자가 의도적으로 껐다고 봅니다)
        return "live" if live_so_far < pub.get("max_live_per_run", 3) else "draft"
    if not review.approved(cfg["review"].get("min_score", 80)):
        return "draft"
    if live_so_far >= pub.get("max_live_per_run", 3):
        return "draft"
    return "live"


def _collect_trend_candidates(cfg: dict, ctx: RunContext, report: list[str]) -> list[trends.Candidate] | None:
    """실시간 이슈 후보. 수집 부족이면 None."""
    keyword_items, headline_items = trends.collect(cfg)
    if len(keyword_items) < cfg["run"]["min_keywords"]:
        log.error(
            "수집된 키워드가 %d개뿐입니다(최소 %d개). 이번 실행은 건너뜁니다.",
            len(keyword_items), cfg["run"]["min_keywords"],
        )
        report.append(f"- ❌ 키워드 수집 부족 ({len(keyword_items)}개). 실행 중단.")
        return None

    by_source: dict[str, int] = {}
    for item in keyword_items + headline_items:
        by_source[item.source] = by_source.get(item.source, 0) + 1
    report.append("## 수집")
    report += [f"- {s}: {n}개" for s, n in sorted(by_source.items())]
    report.append("")

    candidates = trends.aggregate(cfg, keyword_items, headline_items)
    kept, rejected = filters.apply(cfg, candidates)
    history = state.load(ctx.history_path)
    fresh, skipped = state.filter_seen(cfg, kept, history)

    niche_dropped: list[tuple[str, str]] = []
    claimed: list[tuple[str, str]] = []
    if ctx.blog and ctx.fleet:
        fresh, niche_dropped = fleet_mod.filter_niche(ctx.blog, fresh)
        fresh, claimed = fleet_mod.filter_claimed(ctx.fleet, cfg, ctx.blog, fresh, ctx.claims or {})

    log.info("후보 %d개 → 필터 통과 %d개 → 신규 %d개", len(candidates), len(kept), len(fresh))

    report.append("## 후보 (상위 10)")
    for c in fresh[:10]:
        report.append(f"- **{c.keyword}** (점수 {c.score}, 소스 {len(c.sources)}개)")
    if rejected:
        report.append("")
        report.append("## 걸러낸 키워드")
        report += [f"- {r.keyword} — {r.reason}" for r in rejected[:15]]
    if skipped:
        report.append("")
        report.append("## 최근 중복으로 건너뛴 키워드")
        report += [f"- {k} (이전: {prev})" for k, prev in skipped[:10]]
    if niche_dropped or claimed:
        report.append("")
        report.append("## 함대 규칙으로 건너뛴 키워드")
        report += [f"- {k} — {why}" for k, why in (niche_dropped + claimed)[:15]]
    report.append("")
    return fresh


def _collect_evergreen_candidates(cfg: dict, ctx: RunContext, report: list[str]) -> list[trends.Candidate]:
    history = state.load(ctx.history_path)
    proposed = evergreen.propose(cfg, history)
    fresh, skipped = state.filter_seen(cfg, proposed, history)
    if ctx.blog and ctx.fleet:
        fresh, claimed = fleet_mod.filter_claimed(ctx.fleet, cfg, ctx.blog, fresh, ctx.claims or {})
        skipped += claimed
    report.append("## 장수 주제 후보")
    for c in fresh:
        why = f" — {c.headline_hits[0]}" if c.headline_hits else ""
        report.append(f"- **{c.keyword}**{why}")
    if skipped:
        report.append("")
        report.append("## 이미 다뤄서 건너뛴 주제")
        report += [f"- {k} (이전: {prev})" for k, prev in skipped]
    report.append("")
    return fresh


def _build_context(blog_id: str | None, cfg: dict) -> tuple[RunContext, dict]:
    """--blog 가 있으면 함대 컨텍스트(환경변수·경로·설정 덮어쓰기)를 만듭니다."""
    if not blog_id:
        return RunContext(), cfg
    fleet = fleet_mod.load_fleet()
    blog = fleet.get(blog_id)
    fleet_mod.apply_env(fleet, blog)
    cfg = fleet_mod.apply_config(cfg, fleet, blog)
    blog.dir.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(
        history_path=blog.history_path,
        report_path=blog.report_path,
        blog=blog,
        fleet=fleet,
        claims=fleet_mod.load_claims(),
    )
    log.info("함대 블로그 [%s] %s (blog_id %s, 슬롯 %s)", blog.id, blog.name, blog.blog_id, blog.slot)
    return ctx, cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실시간 이슈 블로그 자동 작성")
    parser.add_argument("--blog", help="fleet/blogs.yaml 의 블로그 id. 이 블로그의 설정·이력·Blogger ID 로 실행")
    parser.add_argument("--dry-run", action="store_true", help="키워드 수집·필터까지만 실행")
    parser.add_argument("--target", choices=sorted(publishers.TARGETS), help="발행 대상 덮어쓰기")
    parser.add_argument("--count", type=int, help="작성할 글 개수 덮어쓰기")
    parser.add_argument("--mode", choices=["trend", "evergreen"], default="trend", help="trend: 실시간 이슈 / evergreen: 장수 해설")
    parser.add_argument("--draft", action="store_true", help="검수 결과와 무관하게 전부 임시저장")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    cfg = load_config()
    ctx, cfg = _build_context(args.blog, cfg)

    target_name = args.target or cfg["publish"]["target"]
    mode = args.mode
    if mode == "evergreen":
        want = args.count or cfg["evergreen"]["posts_per_run"]
        labels_cfg = {**cfg, "publish": {**cfg["publish"], "default_labels": cfg["evergreen"].get("default_labels", [])}}
    else:
        want = args.count or cfg["run"]["posts_per_run"]
        labels_cfg = cfg
    now = datetime.now(KST)
    date_str = now.strftime("%Y년 %m월 %d일")
    review_on = cfg.get("review", {}).get("enabled", True)

    title_blog = f" · {ctx.blog.name}" if ctx.blog else ""
    report = [
        f"# 실행 보고 — {now.strftime('%Y-%m-%d %H:%M')} KST ({'장수 글' if mode == 'evergreen' else '실시간 이슈'}{title_blog})",
        "",
    ]

    # 1) 후보
    if mode == "evergreen":
        if args.dry_run:
            log.info("--dry-run: 장수 주제 생성은 모델 호출이 필요해 건너뜁니다.")
            _write_report(ctx, report + ["- dry-run: 장수 모드는 확인할 것이 없습니다."])
            return 0
        fresh = _collect_evergreen_candidates(cfg, ctx, report)
    else:
        fresh = _collect_trend_candidates(cfg, ctx, report)
        if fresh is None:
            _write_report(ctx, report)
            return 1

    if args.dry_run:
        log.info("--dry-run: 글 작성은 건너뜁니다.")
        for c in fresh[:want]:
            print(f"  · {c.keyword}  점수={c.score}  소스={list(c.sources)}")
        _write_report(ctx, report)
        return 0

    if not fresh:
        log.warning("쓸 만한 새 키워드가 없습니다.")
        report.append("- ⚠️ 새 키워드 없음. 작성 건너뜀.")
        _write_report(ctx, report)
        return 0

    # 2) 리서치 → 작성 → 검수 → 발행
    publisher = publishers.get(target_name)
    history = state.load(ctx.history_path)
    written = 0
    live = 0
    errors = 0  # 진짜 실패(API 오류 등). 필터·모델 판단으로 건너뛴 건 여기 안 셉니다.
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
            article = writer.write(cfg, candidate, context, refs, date_str, mode=mode, persona=ctx.persona)
            cost = article.cost_usd

            rv: reviewer.Review | None = None
            if review_on:
                rv = reviewer.review(cfg, article, context)
                cost += rv.cost_usd

        except writer.SkippedByModel as exc:
            log.info("[%s] 모델이 작성을 건너뜀: %s", candidate.keyword, exc)
            report.append(f"- ⏭️ {candidate.keyword} — 모델 판단으로 제외")
            continue
        except Exception as exc:
            log.exception("[%s] 작성 실패: %s", candidate.keyword, exc)
            report.append(f"- ❌ {candidate.keyword} — 작성 실패: {exc}")
            errors += 1
            continue

        # 함대: 글을 쓴 순간 키워드를 선점해 다른 블로그가 같은 주제를 쓰지 않게 합니다 (결과와 무관)
        if ctx.blog and ctx.claims is not None:
            fleet_mod.claim(ctx.claims, ctx.blog, candidate.keyword, now)
            fleet_mod.save_claims(ctx.claims)

        decision = decide(cfg, rv, live, args.draft)
        score_txt = f"검수 {rv.score}점" if rv else "검수 없음"
        issues_txt = f" — {'; '.join(rv.issues[:2])}" if rv and rv.issues else ""

        if decision == "reject":
            total_cost += cost
            history = state.record(
                history, keyword=candidate.keyword, title=article.title,
                status="rejected", mode=mode, cost_usd=cost, review_score=rv.score if rv else None,
            )
            state.save(history, ctx.history_path)
            written += 1  # 비용은 썼으니 하루 생산량에는 포함
            report.append(f"- 🚫 **{article.title}** — 검수 거부({score_txt}){issues_txt}")
            continue

        extras: list[str] = []
        if decision == "live" and rv is not None:
            extras = monetize.apply(cfg, article, rv)

        try:
            post = publisher.publish(labels_cfg, article, live=(decision == "live"))
        except Exception as exc:
            log.exception("[%s] 발행 실패: %s", candidate.keyword, exc)
            report.append(f"- ❌ {article.title} — 발행 실패: {exc}")
            errors += 1
            continue

        history = state.record(
            history,
            keyword=candidate.keyword,
            title=article.title,
            post_id=str(post.get("id", "")),
            url=post.get("url", ""),
            status=decision,
            mode=mode,
            cost_usd=cost,
            review_score=rv.score if rv else None,
            extras=extras,
        )
        state.save(history, ctx.history_path)

        written += 1
        total_cost += cost
        if decision == "live":
            live += 1
        log.info(
            "[%s] %s — 토큰 %d/%d, 약 $%.3f",
            article.title, "공개" if decision == "live" else "임시저장",
            article.input_tokens, article.output_tokens, cost,
        )
        icon = "✅" if decision == "live" else "📝"
        state_txt = "공개" if decision == "live" else "임시저장"
        extra_txt = f" · 제휴: {', '.join(extras)}" if extras else ""
        report.append(
            f"- {icon} **{article.title}** ({state_txt}, {score_txt}){issues_txt}  \n"
            f"  키워드: {candidate.keyword} · 태그: {', '.join(article.labels)} · "
            f"비용 약 ${cost:.3f}{extra_txt}"
            + (f"  \n  {post.get('url')}" if decision == "live" and post.get("url") else "")
        )

    summary = f"**공개 {live}건 / 임시저장 {written - live}건 / 예상 비용 약 ${total_cost:.2f}**"
    if errors:
        summary += f" · 실패 {errors}건"
    report += ["", summary]
    _write_report(ctx, report)

    log.info("끝. 공개 %d건, 작성 %d건, 실패 %d건, 예상 비용 약 $%.2f", live, written, errors, total_cost)

    # 종료 코드는 "사람이 봐야 하는 문제인가"만 알립니다.
    # 후보가 전부 필터에 걸려 한 건도 못 쓴 것은 정상 동작이므로 성공으로 끝냅니다.
    if written == 0 and errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
