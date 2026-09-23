"""네트워크 없이 도는 로직 테스트.

config.yaml 을 만진 뒤 의도대로 동작하는지 30초 안에 확인할 수 있습니다.
(selftest.py 는 실제 사이트를 호출해서 느리고, 그날 트렌드에 따라 결과가 달라집니다.)

    python scripts/test_logic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import evergreen, filters, research, reviewer, state, trends, writer  # noqa: E402
from src.config import load_config  # noqa: E402
from src.main import decide  # noqa: E402
from src.monetize import coupang  # noqa: E402
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


def test_internal_links(cfg: dict) -> None:
    section("내부 링크 후보")
    hist = [
        {"status": "live", "title": "정기검사 준비물", "url": "https://b.blogspot.com/a.html", "keyword": "정기검사"},
        {"status": "draft", "title": "보류된 글", "url": "https://b.blogspot.com/d.html", "keyword": "보류"},
        {"status": "rejected", "title": "거부된 글", "url": "", "keyword": "거부"},
        {"status": "live", "title": "과태료 조회", "url": "https://b.blogspot.com/b.html", "keyword": "과태료"},
        {"status": "live", "title": "로컬 저장본", "url": "C:\\out\\x.html", "keyword": "로컬"},
    ]
    block = research.internal_links_block(hist)
    check("공개된 글만 포함", "정기검사 준비물" in block and "과태료 조회" in block)
    check("보류·거부 글 제외", "보류된 글" not in block and "거부된 글" not in block)
    check("로컬 파일 경로 제외", "로컬 저장본" not in block)
    check("최근 글이 먼저", block.index("과태료 조회") < block.index("정기검사 준비물"))
    check("지금 쓰는 글 자신은 제외",
          "과태료 조회" not in research.internal_links_block(hist, current_keyword="과태료"))
    check("공개 글 없으면 빈 문자열", research.internal_links_block([{"status": "draft"}]) == "")
    check("개수 제한", research.internal_links_block(
        [{"status": "live", "title": f"글{i}", "url": f"https://b/{i}", "keyword": str(i)} for i in range(20)],
        limit=3).count("https://") == 3)

    dup = [{"status": "live", "title": "같은 글", "url": "https://b/x", "keyword": "a"},
           {"status": "live", "title": "같은 글 재발행", "url": "https://b/x", "keyword": "b"}]
    check("같은 주소 중복 제거", research.internal_links_block(dup).count("https://b/x") == 1)


def test_no_why_searched(cfg: dict) -> None:
    section("'왜 검색되는가' 금지")
    rendered = writer.SYSTEM.format(target_length=1900, date="2026년 09월 23일", footer_rule="- 없음")
    check("작성 규칙에 금지 문구 있음", "왜 검색되는가" in rendered and "만들지 마세요" in rendered)
    check("도입부가 답부터", "독자가 찾으러 온 답을 바로" in rendered)
    check("옛 지시문 제거", "왜 지금 검색되는지부터 파악" not in writer.USER_TEMPLATE)
    check("검수도 같은 기준", "왜 지금 검색되는가" in reviewer.SYSTEM)
    check("내부 링크 규칙 있음", "이 블로그의 다른 글" in rendered and "억지로 넣지 마세요" in rendered)
    check("검수가 내부 링크를 지어낸 링크로 보지 않음", "그 목록에 없는 같은 블로그 주소" in reviewer.SYSTEM)

    # 기획 메모가 글쓰기 자료로 새지 않아야 합니다 (이게 '왜 검색되는가' 섹션의 원인이었습니다)
    c = mk_candidate("테스트 주제")
    c.note = "연말정산 시즌이라 검색량이 늘어남"
    ctx = research.to_context(cfg, c, [NewsRef(title="기사", url="https://e.com/a", source="매체")])
    check("기획 메모는 자료에 안 들어감", "검색량이 늘어남" not in ctx)


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


def test_reviewer_parse(cfg: dict) -> None:
    section("검수 응답 파싱")
    raw = (
        '```json\n{"verdict": "publish", "score": 88, "issues": [], '
        '"commercial_intent": true, "product_query": "갤럭시 S26 케이스", "evergreen": false}\n```'
    )
    rv = reviewer._parse(raw, "claude-sonnet-5")
    check("코드 펜스 섞여도 파싱", rv.verdict == "publish" and rv.score == 88)
    check("상품 검색어 추출", rv.product_query == "갤럭시 S26 케이스")
    check("승인 판정", rv.approved(80) and not rv.approved(90))

    rv2 = reviewer._parse('{"verdict": "WEIRD", "score": "abc", "issues": "하나"}', "m")
    check("이상한 값은 hold 로", rv2.verdict == "hold" and rv2.score == 0 and rv2.issues == ["하나"])

    rv3 = reviewer._parse('{"verdict": "reject", "score": 150}', "m")
    check("점수 범위 고정", rv3.score == 100)

    rv3.input_tokens, rv3.output_tokens = 10000, 500
    check("검수 비용 계산", abs(rv3.cost_usd - (10000 * 2 + 500 * 10) / 1e6) < 1e-9)


def fm_for_guard():
    """계정 상한 테스트용 소형 함대.

    default 계정에 블로그 2개(공통 상한 6), second 계정에 1개(계정별 상한 3).
    """
    from src import fleet as fm
    return fm.Fleet(
        {"max_live_per_day_account": 6},
        {"default": {}, "second": {"max_live_per_day_account": 3}},
        [fm.Blog(id="a", name="a", blog_id="1", subject="가"),
         fm.Blog(id="b", name="b", blog_id="2", subject="나"),
         fm.Blog(id="c", name="c", blog_id="3", subject="다", account="second")],
    )


def test_accounts(cfg: dict) -> None:
    section("다계정 지원")
    import os
    from src import fleet as fm
    fl = fm_for_guard()

    check("계정별 블로그 분리", [b.id for b in fl.blogs_of("default")] == ["a", "b"]
          and [b.id for b in fl.blogs_of("second")] == ["c"])
    check("사용 중인 계정 목록", fl.used_accounts == ["default", "second"], f"{fl.used_accounts}")
    check("계정별 설정이 함대 공통보다 우선", fl.account_setting("second", "max_live_per_day_account", 9) == 3)
    check("계정 설정 없으면 함대 공통", fl.account_setting("default", "max_live_per_day_account", 9) == 6)
    check("둘 다 없으면 기본값", fl.account_setting("default", "없는키", 42) == 42)

    check("default 계정은 표준 변수명", fm.env_name("BLOGGER_REFRESH_TOKEN", "default") == "BLOGGER_REFRESH_TOKEN")
    check("그 외 계정은 접미사", fm.env_name("BLOGGER_REFRESH_TOKEN", "second") == "BLOGGER_REFRESH_TOKEN_SECOND")

    # 환경변수 교체가 계정별로 되는지
    saved = {k: os.environ.get(k) for k in
             ("BLOGGER_REFRESH_TOKEN", "BLOGGER_CLIENT_ID", "BLOGGER_CLIENT_SECRET",
              "BLOGGER_REFRESH_TOKEN_SECOND", "BLOGGER_CLIENT_ID_SECOND", "BLOGGER_CLIENT_SECRET_SECOND",
              "BLOGGER_BLOG_ID", "FLEET_ACCOUNT")}
    try:
        os.environ.update({
            "BLOGGER_REFRESH_TOKEN": "tok-default", "BLOGGER_CLIENT_ID": "cid-default",
            "BLOGGER_CLIENT_SECRET": "sec-default",
            "BLOGGER_REFRESH_TOKEN_SECOND": "tok-second", "BLOGGER_CLIENT_ID_SECOND": "cid-second",
            "BLOGGER_CLIENT_SECRET_SECOND": "sec-second",
        })
        fm._default_credentials = None   # 이 테스트 값으로 원본 스냅샷을 다시 잡습니다
        fm.apply_env(fl, fl.get("c"))
        check("다른 계정 블로그 → 그 계정 토큰으로 교체",
              os.environ["BLOGGER_REFRESH_TOKEN"] == "tok-second"
              and os.environ["BLOGGER_CLIENT_ID"] == "cid-second"
              and os.environ["BLOGGER_BLOG_ID"] == "3",
              os.environ["BLOGGER_REFRESH_TOKEN"])
        check("계정 이름도 환경에 기록", os.environ.get("FLEET_ACCOUNT") == "second")

        # 토큰 캐시가 자격증명별이어야 계정을 오갈 때 섞이지 않습니다 (이 버그가 다계정의 핵심 함정)
        from src.publishers import blogger as bl
        key_second = bl._credential_key()
        fm.apply_env(fl, fl.get("a"))
        check("default 로 되돌리면 표준 토큰", os.environ["BLOGGER_REFRESH_TOKEN"] == "tok-default")
        check("자격증명이 바뀌면 캐시 키도 다름", bl._credential_key() != key_second)
        check("같은 자격증명이면 캐시 키 동일", bl._credential_key() == bl._credential_key())

        check("자격증명 있으면 missing 없음", fm.missing_credentials(fl, "second") == [])
        del os.environ["BLOGGER_REFRESH_TOKEN_SECOND"]
        check("빠진 변수는 이름으로 보고",
              fm.missing_credentials(fl, "second") == ["BLOGGER_REFRESH_TOKEN_SECOND"],
              f"{fm.missing_credentials(fl, 'second')}")
        rep = fm.credential_report(fl)
        check("계정별 보고", rep["default"] == [] and rep["second"], f"{rep}")

        # 자격증명 없는 계정으로 바꾸면 표준 변수를 지워 인증 오류로 실패하게 합니다
        # (이전 계정 토큰이 남아 엉뚱한 블로그에 쓰는 것보다 안전)
        fm.apply_env(fl, fl.get("c"))
        check("자격증명 없으면 표준 변수 제거", "BLOGGER_REFRESH_TOKEN" not in os.environ,
              os.environ.get("BLOGGER_REFRESH_TOKEN", "(없음)"))
        fm.apply_env(fl, fl.get("a"))
        check("그 뒤 default 는 정상 복원", os.environ.get("BLOGGER_REFRESH_TOKEN") == "tok-default")
    finally:
        fm._default_credentials = None
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_guard(cfg: dict) -> None:
    section("발행 안전장치 (서버 기준 상한)")
    from src import guard
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 9, 21, 10, 0, tzinfo=kst)
    g = {**cfg, "publish": {**cfg["publish"], "max_live_per_day": 2}}

    import src.publishers.blogger as bl
    orig = bl.published_since
    try:
        bl.published_since = lambda *a, **k: 0
        b = guard.daily_budget(g, "1", now)
        check("오늘 0건 → 2건 허용", b.allowed == 2 and not b.blocked)
        bl.published_since = lambda *a, **k: 2
        b = guard.daily_budget(g, "1", now)
        check("상한 도달 → 차단", b.blocked and "2건" in b.reason, b.reason)
        bl.published_since = lambda *a, **k: 5   # 이미 초과된 상태(사고 상황)
        check("이미 초과면 음수 아닌 0", guard.daily_budget(g, "1", now).allowed == 0)
        def boom(*a, **k): raise RuntimeError("네트워크 오류")
        bl.published_since = boom
        b = guard.daily_budget(g, "1", now)
        check("서버 확인 실패 → 발행 안 함 (fail-closed)", b.blocked and b.already_today == -1)

        # 계정별 상한 — 구글의 제한은 계정에 붙습니다
        fl = fm_for_guard()
        bl.published_since = lambda *a, **k: 2   # 블로그마다 2건
        ab = guard.account_budget(fl, "default", now)
        check("계정 합계로 계산", ab.already_today == 4 and ab.allowed == 2, f"{ab.already_today}/{ab.allowed}")
        check("다른 계정은 따로 계산", guard.account_budget(fl, "second", now).already_today == 2,
              f"{guard.account_budget(fl, 'second', now).already_today}")
        check("계정별 상한 설정이 우선", guard.account_budget(fl, "second", now).daily_cap == 3,
              f"{guard.account_budget(fl, 'second', now).daily_cap}")
        bl.published_since = lambda *a, **k: 3
        check("계정 상한 도달 → 차단", guard.account_budget(fl, "default", now).blocked)
        bl.published_since = boom
        check("한 블로그라도 확인 실패 → 계정 전체 보류", guard.account_budget(fl, "default", now).blocked)

        gap = {**cfg, "publish": {**cfg["publish"], "min_gap_minutes": 45}}
        bl.published_since = lambda *a, **k: 1
        ok, why = guard.min_gap_ok(gap, "1", now)
        check("최근 발행 있으면 간격 미확보", not ok and "45분" in why, why)
        bl.published_since = lambda *a, **k: 0
        check("최근 발행 없으면 통과", guard.min_gap_ok(gap, "1", now)[0])
        nogap = {**cfg, "publish": {**cfg["publish"], "min_gap_minutes": 0}}
        check("간격 0이면 항상 통과", guard.min_gap_ok(nogap, "1", now)[0])
    finally:
        bl.published_since = orig


def test_history_guard() -> None:
    section("이력 손상 감지 (사고 재발 방지)")
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "history.json"
        p.write_text('[{"keyword":"가","posted_at":"2026-09-20T10:00:00+09:00"}]', encoding="utf-8")
        check("정상 파일은 읽힘", len(state.load(p)) == 1)

        p.write_text('[{"keyword":"가"},\n<<<<<<< Updated upstream\n{"keyword":"나"}\n=======\n{"keyword":"다"}\n>>>>>>> x\n]', encoding="utf-8")
        try:
            state.load(p)
            check("깃 충돌 마커 → 예외", False, "조용히 넘어감 — 이게 사고 원인이었음")
        except state.HistoryCorrupted:
            check("깃 충돌 마커 → 예외", True)

        p.write_text('{"not": "a list"}', encoding="utf-8")
        try:
            state.load(p)
            check("목록이 아니면 예외", False)
        except state.HistoryCorrupted:
            check("목록이 아니면 예외", True)

        p.write_text("깨진 내용 {{{", encoding="utf-8")
        try:
            state.load(p)
            check("파싱 실패 → 예외", False)
        except state.HistoryCorrupted:
            check("파싱 실패 → 예외", True)

        check("파일이 없으면 빈 이력", state.load(Path(tmp) / "none.json") == [])

        # 저장은 원자적이어야 합니다 (반쪽 파일이 남으면 다음 실행이 멈춥니다)
        state.save([{"keyword": "가", "posted_at": "2026-09-21T00:00:00+09:00"}], p)
        check("저장 후 다시 읽힘", len(state.load(p)) == 1)
        check("임시 파일이 남지 않음", not (Path(tmp) / "history.json.tmp").exists())


def test_decide(cfg: dict) -> None:
    section("공개 판정")
    auto = {**cfg, "publish": {**cfg["publish"], "mode": "auto", "max_live_per_day": 2},
            "review": {**cfg["review"], "min_score": 80}}
    ok = reviewer.Review(verdict="publish", score=85)
    low = reviewer.Review(verdict="publish", score=70)
    hold = reviewer.Review(verdict="hold", score=90)
    bad = reviewer.Review(verdict="reject", score=10)

    check("통과 → 공개", decide(auto, ok, 0, False) == "live")
    check("점수 미달 → 임시저장", decide(auto, low, 0, False) == "draft")
    check("hold → 임시저장", decide(auto, hold, 0, False) == "draft")
    check("reject → 올리지 않음", decide(auto, bad, 0, False) == "reject")
    check("하루 상한 초과 → 임시저장", decide(auto, ok, 2, False) == "draft")
    check("서버 기준 여유가 0이면 임시저장", decide(auto, ok, 0, False, live_cap=0) == "draft")
    check("서버 기준 여유만큼만 공개", decide(auto, ok, 1, False, live_cap=2) == "live")
    check("--draft 강제", decide(auto, ok, 0, True) == "draft")
    check("--draft 라도 reject 는 reject", decide(auto, bad, 0, True) == "reject")

    draft_mode = {**auto, "publish": {**auto["publish"], "mode": "draft"}}
    check("draft 모드 → 전부 임시저장", decide(draft_mode, ok, 0, False) == "draft")
    legacy = {**auto, "publish": {**auto["publish"], "draft_only": True}}
    check("옛 설정 draft_only 인식", decide(legacy, ok, 0, False) == "draft")
    check("검수 꺼짐 + auto → 공개", decide(auto, None, 0, False) == "live")


def test_coupang(cfg: dict) -> None:
    section("쿠팡 상품 블록")
    products = [
        {"name": "테스트 <상품>", "price": 12900, "image": "https://img/x.jpg", "url": "https://link.coupang.com/a/1", "rocket": True},
        {"name": "둘째", "price": 0, "image": "", "url": "https://link.coupang.com/a/2", "rocket": False},
    ]
    block = coupang.render(products, "관련 상품")
    check("HTML 이스케이프", "&lt;상품&gt;" in block and "<상품>" not in block)
    check("가격 포맷", "12,900원" in block and "가격 확인" in block)
    check("sponsored 링크", 'rel="nofollow sponsored noopener"' in block)
    check("법정 고지문 포함", coupang.DISCLOSURE in block)
    check("빈 목록은 빈 문자열", coupang.render([], "x") == "")

    body = "<p>도입</p><h2>핵심</h2><p>내용</p><h2>자주 묻는 질문</h2><p>Q</p><h2>참고한 자료</h2><ul></ul>"
    out = coupang.insert(body, "<h2>관련 상품</h2>")
    check("FAQ 앞에 삽입", out.index("관련 상품") < out.index("자주 묻는 질문"))
    out2 = coupang.insert("<p>a</p><h2>참고한 자료</h2>", "BLOCK")
    check("FAQ 없으면 참고 자료 앞", out2.index("BLOCK") < out2.index("참고한 자료"))
    check("둘 다 없으면 맨 끝", coupang.insert("<p>a</p>", "BLOCK").endswith("BLOCK"))
    check("빈 블록은 그대로", coupang.insert(body, "") == body)
    check("키 없으면 미설정", coupang.configured() in (True, False))


def test_evergreen_parse(cfg: dict) -> None:
    section("장수 주제 파싱")
    raw = '[{"topic": "레버리지 ETF 수수료 계산법", "why": "상시 검색", "search": "레버리지 ETF"}, {"topic": ""}]'
    topics = evergreen._parse(raw)
    check("빈 주제 제외", len(topics) == 1 and topics[0]["search"] == "레버리지 ETF")

    history = state.record([], keyword="레버리지 ETF 수수료 계산법", title="t", mode="evergreen")
    check("이력에 mode 기록", history[0]["mode"] == "evergreen" and history[0]["status"] == "draft")
    check("이력에 비용·점수 필드", "cost_usd" in history[0] and "review_score" in history[0])

    cfg_ev = {**cfg, "dedupe": {**cfg["dedupe"]}}
    cand = trends.Candidate(keyword="레버리지 ETF 수수료 계산법")
    fresh, skipped = state.filter_seen(cfg_ev, [cand], history)
    check("장수 주제도 중복 방지", len(fresh) == 0 and len(skipped) == 1)


def test_config_shape(cfg: dict) -> None:
    section("설정 형식")
    check("review 섹션", cfg["review"]["model"].startswith("claude-") and 0 < cfg["review"]["min_score"] <= 100)
    check("publish.mode", cfg["publish"]["mode"] in ("auto", "draft"))
    # 2026-09-20 사고: 하루 8건 발행 → 계정 API 차단. 상한을 다시 올리지 못하게 테스트로 고정합니다.
    check("하루 공개 상한 2 이하", 1 <= cfg["publish"]["max_live_per_day"] <= 2, "대량 생성 정책 위험")
    check("작성 수 2 이하", 1 <= cfg["run"]["posts_per_run"] <= 2)
    check("발행 간격 설정 있음", cfg["publish"].get("min_gap_minutes", 0) >= 30)
    check("글 작성 모델은 Fable", cfg["writer"]["model"] == "claude-fable-5-1", cfg["writer"]["model"])
    check("검수·기획·관리는 Sonnet",
          cfg["review"]["model"] == cfg["planner"]["model"] == cfg["manager"]["model"] == "claude-sonnet-5")
    check("planner 섹션", cfg["planner"]["candidates"] >= 3)
    check("evergreen 섹션", cfg["evergreen"]["posts_per_run"] >= 1)
    check("monetize.coupang 섹션", isinstance(cfg["monetize"]["coupang"]["enabled"], bool))
    check("검수 모델 비용표에 있음", cfg["review"]["model"] in writer.PRICES)


def test_fleet(cfg: dict) -> None:
    section("함대: 설정·슬롯")
    import tempfile
    from datetime import datetime, timezone, timedelta
    from pathlib import Path
    from src import fleet as fm

    kst = timezone(timedelta(hours=9))
    fleet = fm.load_fleet()
    check("blogs.yaml 로딩·검증", len(fleet.blogs) >= 1 and fleet.step >= 10)
    nxt = fm.next_free_slot(fleet)
    taken = [fm.to_minutes(s) for b in fleet.blogs for s in b.slots]
    check("다음 빈 슬롯은 기존 모든 슬롯과 간격 확보", all(abs(fm.to_minutes(nxt) - t) >= fleet.step for t in taken), nxt)
    check("블로그마다 슬롯이 여러 개", all(len(b.slots) >= 1 for b in fleet.blogs))
    check("같은 블로그 슬롯끼리 몇 시간 벌어짐",
          all(fm.to_minutes(b.slots[-1]) - fm.to_minutes(b.slots[0]) >= 120 for b in fleet.blogs if len(b.slots) > 1))

    # 서로 다른 블로그의 슬롯 간격 위반 감지
    bad = fm.Fleet(fleet.settings, fleet.accounts, [
        fm.Blog(id="a", name="a", blog_id="1", slots=["06:20"], subject="가"),
        fm.Blog(id="b", name="b", blog_id="2", slots=["06:25"], subject="나"),
    ])
    try:
        fm.validate(bad)
        check("서로 다른 블로그 슬롯 5분 차이는 거부", False)
    except ValueError:
        check("서로 다른 블로그 슬롯 5분 차이는 거부", True)

    # 주제 중복 감지 — 블로그를 나눈 의미가 사라지므로 막아야 합니다
    dup = fm.Fleet(fleet.settings, fleet.accounts, [
        fm.Blog(id="a", name="a", blog_id="1", slots=["06:20"], subject="세금과 공제 기초"),
        fm.Blog(id="b", name="b", blog_id="2", slots=["09:00"], subject="공제 세금 기초"),
    ])
    try:
        fm.validate(dup)
        check("주제 중복은 거부", False)
    except ValueError:
        check("주제 중복은 거부", True)

    # planned 모드인데 subject 가 없으면 거부
    nosubj = fm.Fleet(fleet.settings, fleet.accounts, [fm.Blog(id="a", name="a", blog_id="1", slots=["06:20"])])
    try:
        fm.validate(nosubj)
        check("planned 인데 subject 없으면 거부", False)
    except ValueError:
        check("planned 인데 subject 없으면 거부", True)

    section("함대: 실행 대상 계산")
    with tempfile.TemporaryDirectory() as tmp:
        # 데이터 폴더를 임시로 바꿔 runs.json 이 실제 데이터에 안 남게 합니다
        orig = fm.DATA_DIR
        fm.DATA_DIR = Path(tmp)
        try:
            f2 = fm.Fleet({**fleet.settings, "catch_up": True}, fleet.accounts, [
                fm.Blog(id="x", name="x", blog_id="1", slots=["06:20", "15:00"], subject="가"),
                fm.Blog(id="y", name="y", blog_id="2", slots=["09:00"], subject="나"),
                fm.Blog(id="z", name="z", blog_id="3", slots=["12:00"], subject="다", enabled=False),
            ])
            now = datetime(2026, 9, 20, 16, 0, tzinfo=kst)
            due = [(b.id, s) for b, s in fm.due_slots(f2, now, "planned")]
            check("슬롯 단위로, 시각 순서대로", due == [("x", "06:20"), ("y", "09:00"), ("x", "15:00")], f"{due}")
            fm.mark_ran(f2.blogs[0], "planned", True, "ok", now, slot="06:20")
            due2 = [(b.id, s) for b, s in fm.due_slots(f2, now, "planned")]
            check("완료한 슬롯만 빠짐 (같은 블로그 다른 슬롯은 남음)",
                  due2 == [("y", "09:00"), ("x", "15:00")], f"{due2}")
            check("꺼진 블로그는 제외", all(b != "z" for b, _ in due2))
            f3 = fm.Fleet({**f2.settings, "catch_up": False}, f2.accounts, f2.blogs)
            due3 = [(b.id, s) for b, s in fm.due_slots(f3, datetime(2026, 9, 20, 9, 3, tzinfo=kst), "planned")]
            due3b = [(b.id, s) for b, s in fm.due_slots(f3, datetime(2026, 9, 20, 9, 50, tzinfo=kst), "planned")]
            check("catch-up 끄면 현재 창만", due3 == [("y", "09:00")] and due3b == [], f"{due3} {due3b}")
        finally:
            fm.DATA_DIR = orig

    section("함대: 설정 덮어쓰기·주제 필터·선점")
    blog = fm.Blog(id="eco", name="경제", blog_id="9", subject="경제", include_patterns=["금리", "환율"],
                   exclude_patterns=["로또번호"], labels=["경제"], overrides={"run": {"posts_per_run": 2}})
    merged = fm.apply_config(cfg, fleet, blog)
    check("overrides 적용", merged["run"]["posts_per_run"] == 2)
    check("fleet.defaults 적용", merged["publish"]["max_live_per_day"] == 2)
    check("블로그 라벨 적용", merged["publish"]["default_labels"] == ["경제"])
    check("exclude_patterns → block_keywords", "로또번호" in merged["filters"]["block_keywords"])
    check("원본 cfg 불변", cfg["run"]["posts_per_run"] == load_config()["run"]["posts_per_run"])

    cands = [mk_candidate("기준금리 인하 전망"), mk_candidate("축구 국가대표 명단")]
    kept, dropped = fm.filter_niche(blog, cands)
    check("include_patterns 로 주제 제한", [c.keyword for c in kept] == ["기준금리 인하 전망"] and len(dropped) == 1)

    now = datetime(2026, 9, 20, 10, 0, tzinfo=kst)
    claims = fm.claim({}, fm.Blog(id="other", name="o", blog_id="8"), "기준금리 인하", now)
    kept2, skipped = fm.filter_claimed(fleet, cfg, blog, cands, claims, now)
    check("다른 블로그가 선점한 유사 키워드 제외", len(kept2) == 1 and skipped and "other" in skipped[0][1])
    kept3, _ = fm.filter_claimed(fleet, cfg, fm.Blog(id="other", name="o", blog_id="8"), cands, claims, now)
    check("자기 선점은 제외 안 함", len(kept3) == 2)
    # 2026-09-20 관측: 같은 사건이 길이 다른 표현으로 세 블로그에 실렸음
    ufc = fm.claim({}, fm.Blog(id="a", name="a", blog_id="1"), "최두호, 핏불에게 1R TKO패", now)
    same = [mk_candidate("최두호"), mk_candidate("최두호, UFC 첫 피니시 소감"), mk_candidate("핑크뮬리 개화")]
    kept5, skipped5 = fm.filter_claimed(fleet, cfg, blog, same, ufc, now)
    check("짧은 표현 ⊂ 선점 표현 → 같은 사건", "최두호" not in [c.keyword for c in kept5])
    check("같은 인물의 다른 표현도 제외", len(kept5) == 1 and kept5[0].keyword == "핑크뮬리 개화", f"{[c.keyword for c in kept5]}")
    shared = fm.Fleet({**fleet.settings, "share_topics": True}, fleet.accounts, fleet.blogs)
    kept4, _ = fm.filter_claimed(shared, cfg, blog, cands, claims, now)
    check("share_topics: true 면 선점 무시", len(kept4) == 2)
    fm.claim(claims, blog, "옛 키워드", now - timedelta(days=40))   # 40일 전 선점 기록
    pruned = fm.claim(claims, blog, "오늘 키워드", now)                # 오늘 선점하면서 오래된 날짜 정리
    check("30일 지난 선점은 정리", all((now.date() - datetime.strptime(d, "%Y-%m-%d").date()).days <= 30 for d in pruned))

    section("함대: 관리 에이전트 규칙")
    from src import manager
    metrics = [
        {"id": "ok", "enabled": True, "paused_by": None, "posts_per_run": 2, "runs_7d": 7, "live_7d": 10, "consecutive_failures": 0},
        {"id": "dead", "enabled": True, "paused_by": None, "posts_per_run": 2, "runs_7d": 7, "live_7d": 0, "consecutive_failures": 3},
        {"id": "manual", "enabled": False, "paused_by": "manual", "posts_per_run": 2, "runs_7d": 0, "live_7d": 0, "consecutive_failures": 0},
    ]
    proposed = [
        {"blog": "ok", "action": "pause", "value": None, "reason": "그냥"},
        {"blog": "dead", "action": "pause", "value": None, "reason": "연속 실패"},
        {"blog": "manual", "action": "resume", "value": None, "reason": "켜자"},
        {"blog": "ok", "action": "set_posts_per_run", "value": 4, "reason": "상한 초과"},
        {"blog": "ok", "action": "set_posts_per_run", "value": 1, "reason": "한 칸 내림"},
        {"blog": "ghost", "action": "pause", "value": None, "reason": "?"},
        {"blog": "ok", "action": "note", "value": None, "reason": "메모"},
    ]
    actions, rejected = manager._validate_actions(proposed, metrics)
    got = {(a["blog"], a["action"], a.get("value")) for a in actions}
    check("근거 없는 pause 거부", ("ok", "pause", None) not in got)
    check("연속 실패 pause 허용", ("dead", "pause", None) in got)
    check("수동 중지 블로그 resume 거부", ("manual", "resume", None) not in got)
    check("상한(2) 초과 제안 거부, 한 칸 내리기 허용",
          ("ok", "set_posts_per_run", 4) not in got and ("ok", "set_posts_per_run", 1) in got)
    check("없는 블로그 거부", not any(a["blog"] == "ghost" for a in actions))
    check("note 는 항상 통과", ("ok", "note", None) in got)
    check("거부 사유 기록", len(rejected) == 4, f"{len(rejected)}")
    rule = manager._rule_based(metrics)
    check("규칙 기반 폴백: 연속 3회 실패 → pause", rule and rule[0]["blog"] == "dead")


def main() -> int:
    cfg = load_config()
    test_tokenize()
    test_aggregate(cfg)
    test_filters(cfg)
    test_dedupe(cfg)
    test_context(cfg)
    test_internal_links(cfg)
    test_no_why_searched(cfg)
    test_writer_parse(cfg)
    test_footer(cfg)
    test_reviewer_parse(cfg)
    test_guard(cfg)
    test_accounts(cfg)
    test_history_guard()
    test_decide(cfg)
    test_coupang(cfg)
    test_evergreen_parse(cfg)
    test_config_shape(cfg)
    test_fleet(cfg)

    print("\n" + "=" * 50)
    if failures:
        print(f"실패 {len(failures)}건: {', '.join(failures)}")
        return 1
    print("전체 통과 (네트워크 없이 실행됨)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
