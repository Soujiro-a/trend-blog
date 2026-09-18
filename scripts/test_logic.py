"""네트워크 없이 도는 로직 테스트.

config.yaml 을 만진 뒤 의도대로 동작하는지 30초 안에 확인할 수 있습니다.
(selftest.py 는 실제 사이트를 호출해서 느리고, 그날 트렌드에 따라 결과가 달라집니다.)

    python scripts/test_logic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import filters, research, state, trends, writer  # noqa: E402
from src.config import load_config  # noqa: E402
from src.trends import Candidate, Variant  # noqa: E402
from src.trends.base import NewsRef, TrendItem, similarity, tokenize  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  [OK]   {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def mk_candidate(rep: str, variants: list[str] | None = None) -> Candidate:
    variants = variants or [rep]
    return Candidate(
        keyword=rep, seen=[Variant(v, "signal_bz", 1) for v in variants]
    )


def test_tokenize() -> None:
    section("토큰화 · 유사도")
    check("조사 제거", "의" not in tokenize("대통령의 발언"))
    check("한 글자 토큰 제외", tokenize("A 축구") == {"축구"}, f"{tokenize('A 축구')}")
    check("같은 말은 유사도 1.0", similarity("북한 선수단", "선수단 북한") == 1.0)
    check("무관한 말은 0.0", similarity("축구", "물가") == 0.0)
    check(
        "부분 일치는 중간값",
        0 < similarity("북한 선수단 입촌", "북한 선수단") < 1.0,
    )


def test_aggregate(cfg: dict) -> None:
    section("집계 · 대표 키워드 선정")
    items = [
        TrendItem("을지연습 사후강평", "signal_bz", 1),
        TrendItem("2026년 을지연습 사후강평회의", "nate", 2),
        TrendItem("물가 상승률", "google_trends", 3),
    ]
    cands = trends.aggregate(cfg, items, [])

    check("합성어가 달라도 같은 이슈로 묶임", len(cands) == 2, f"{len(cands)}개")
    top = cands[0]
    check("여러 소스에 뜬 쪽이 1위", len(top.sources) == 2, f"소스 {top.sources}")
    check("다중 소스 가산점 적용", top.score > cands[1].score)

    section("대표 키워드 선정 규칙")
    # 규칙: 가중치 높은 소스의 상위 순위 표현을 쓰고, 그 값이 같을 때만 더 긴 쪽.
    # 무조건 짧은 쪽을 고르면 '수능 수학 문제 유출' 이 '수학' 이 되는 버그가 납니다.
    regression = trends.aggregate(
        cfg,
        [
            TrendItem("수학", "google_trends", 5),
            TrendItem("수능 수학 문제 유출", "signal_bz", 1),
        ],
        [],
    )
    check(
        "짧은 토막이 대표가 되지 않음",
        regression[0].keyword == "수능 수학 문제 유출",
        f"실제 '{regression[0].keyword}'",
    )

    tie = trends.aggregate(
        cfg,
        [
            TrendItem("물가 상승률", "signal_bz", 1),
            TrendItem("물가 상승률 발표", "signal_bz", 1),
        ],
        [],
    )
    check(
        "소스·순위가 같으면 구체적인 쪽",
        tie[0].keyword == "물가 상승률 발표",
        f"실제 '{tie[0].keyword}'",
    )

    section("헤드라인 보강")
    headlines = [TrendItem("물가 상승률 3개월째 둔화", "naver_news", 1)]
    with_hl = trends.aggregate(cfg, items, headlines)
    plain = {c.keyword: c.score for c in cands}
    boosted = next(c for c in with_hl if "물가" in c.keyword)
    check(
        "뉴스로 뒷받침되면 점수 상승",
        boosted.score > plain.get(boosted.keyword, 0),
        f"{plain.get(boosted.keyword)} → {boosted.score}",
    )
    check("근거 헤드라인 기록됨", len(boosted.headline_hits) == 1)


def test_filters(cfg: dict) -> None:
    section("필터")
    cases = {
        "축구": (mk_candidate("축구"), False),
        "정구": (mk_candidate("정구"), False),
        "오현규": (mk_candidate("오현규"), True),
        "축구 팀": (mk_candidate("축구 팀"), True),
        "축구(대안 있음)": (mk_candidate("축구", ["축구", "축구 국가대표 명단"]), True),
    }
    kept, rejected = filters.apply(cfg, [c for c, _ in cases.values()])
    kept_ids = {id(c) for c in kept}
    for label, (cand, should_keep) in cases.items():
        check(
            f"{label} → {'통과' if should_keep else '제외'}",
            (id(cand) in kept_ids) == should_keep,
        )
    sub = cases["축구(대안 있음)"][0]
    check(
        "짧은 키워드는 구체적 표현으로 교체",
        sub.keyword == "축구 국가대표 명단",
        f"실제 '{sub.keyword}'",
    )

    section("민감 주제 차단")
    sensitive = [
        mk_candidate("배우 김철수 응급실 이송"),
        mk_candidate("정치인 뇌물 의혹"),
    ]
    kept2, rejected2 = filters.apply(cfg, sensitive)
    check("민감 키워드 전부 차단", len(kept2) == 0, f"{len(kept2)}개 통과")
    check("차단 사유 기록됨", all("민감" in r.reason for r in rejected2))

    news_based = mk_candidate("어떤 축구선수 이름")
    news_based.news = [NewsRef(title="○○ 선수 교통사고로 사망")]
    kept3, _ = filters.apply(cfg, [news_based])
    check("연결된 기사 제목으로도 차단", len(kept3) == 0)


def test_dedupe(cfg: dict) -> None:
    section("중복 방지")
    history = state.record([], keyword="북한 선수단 입촌", title="제목")
    cands = [
        mk_candidate("북한 선수단 입촌"),
        mk_candidate("물가 상승률 발표"),
    ]
    fresh, skipped = state.filter_seen(cfg, cands, history)
    check("최근에 쓴 주제는 제외", len(fresh) == 1 and fresh[0].keyword == "물가 상승률 발표")
    check("건너뛴 이유 기록", len(skipped) == 1)

    old = [{"keyword": "북한 선수단 입촌", "posted_at": "2020-01-01T00:00:00+09:00"}]
    fresh2, _ = state.filter_seen(cfg, cands, old)
    check("기간이 지난 이력은 무시", len(fresh2) == 2, f"{len(fresh2)}개")


def test_context(cfg: dict) -> None:
    section("리서치 컨텍스트")
    cand = mk_candidate("테스트 키워드")
    refs = [
        NewsRef(title="실제 링크 기사", url="https://example.com/a", source="동아일보"),
        NewsRef(
            title="구글뉴스 기사",
            url="https://news.google.com/rss/articles/CBMiXXX",
            source="연합뉴스",
        ),
    ]
    ctx = research.to_context(cfg, cand, refs)
    check("실제 URL은 링크 가능으로 표시", "링크(걸어도 됨): https://example.com/a" in ctx)
    check("구글뉴스 URL은 링크 불가로 표시", "링크 없음" in ctx)
    check("구글뉴스 주소는 노출 안 됨", "news.google.com" not in ctx)
    check("더 읽기 링크 포함", "search.naver.com" in ctx)

    # 글자수 제한에 걸려도 '더 읽기' 링크는 살아남아야 합니다.
    tight = {**cfg, "research": {**cfg["research"], "max_context_chars": 400}}
    many = [NewsRef(title=f"기사 제목 {i}" * 10, source="매체") for i in range(30)]
    ctx2 = research.to_context(tight, cand, many)
    check("잘려도 더 읽기 링크 유지", "search.naver.com" in ctx2)
    check("잘림 표시 있음", "생략" in ctx2)


def test_writer_parse(cfg: dict) -> None:
    section("모델 응답 파싱")
    raw = (
        "<<<TITLE>>>\n제목입니다\n"
        "<<<DESCRIPTION>>>\n요약입니다\n"
        "<<<LABELS>>>\n가, 나, 다\n"
        "<<<BODY>>>\n<p>본문</p>"
    )
    a = writer._parse(raw, "키워드", "claude-opus-5")
    check("제목", a.title == "제목입니다")
    check("태그", a.labels == ["가", "나", "다"], f"{a.labels}")
    check("본문", a.body_html == "<p>본문</p>")

    try:
        writer._parse("SKIP: 사생활 주제", "키워드", "claude-opus-5")
        check("SKIP 처리", False, "예외가 안 났습니다")
    except writer.SkippedByModel:
        check("SKIP 처리", True)

    try:
        writer._parse("형식에 맞지 않는 응답", "키워드", "claude-opus-5")
        check("형식 오류 감지", False, "예외가 안 났습니다")
    except ValueError:
        check("형식 오류 감지", True)

    a.input_tokens, a.output_tokens = 4000, 3000
    check("비용 계산", abs(a.cost_usd - (4000 * 5 + 3000 * 25) / 1e6) < 1e-9)


def test_footer(cfg: dict) -> None:
    section("안내 문구 설정")
    note = (cfg["writer"].get("footer_note") or "").strip()
    rendered = writer.SYSTEM.format(
        target_length=1900,
        date="2026년 09월 18일",
        footer_rule=f"- 맨 끝: {note.format(date='2026년 09월 18일')}" if note else "- 없음",
    )
    check("프롬프트 조립 성공", len(rendered) > 500)
    check("더 읽기 자리표시자 유지", "{더_읽기_주소}" in rendered)
    check("AI 고지 문구 없음", "AI" not in rendered)


def main() -> int:
    cfg = load_config()
    test_tokenize()
    test_aggregate(cfg)
    test_filters(cfg)
    test_dedupe(cfg)
    test_context(cfg)
    test_writer_parse(cfg)
    test_footer(cfg)

    print("\n" + "=" * 50)
    if failures:
        print(f"실패 {len(failures)}건: {', '.join(failures)}")
        return 1
    print("전체 통과 (네트워크 없이 실행됨)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
