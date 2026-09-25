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


def test_llm_robustness(cfg: dict) -> None:
    section("모델 응답 잘림 대응")
    from src import llm, planner
    from dataclasses import dataclass, field

    @dataclass
    class _U:
        output_tokens: int = 2000

    @dataclass
    class _B:
        text: str
        type: str = "text"

    @dataclass
    class _R:
        content: list
        stop_reason: str
        usage: _U = field(default_factory=_U)

    ok = _R([_B('{"a":1}')], "end_turn")
    check("정상 응답은 그대로", llm.text_of(ok, "t") == '{"a":1}')
    cut = _R([_B('[{"topic":"가"},{"topic":')], "max_tokens")
    try:
        llm.text_of(cut, "t")
        check("잘린 응답은 기본적으로 예외", False)
    except llm.Truncated:
        check("잘린 응답은 기본적으로 예외", True)
    check("허용하면 잘린 텍스트 반환", llm.text_of(cut, "t", allow_truncated=True).startswith("[{"))

    # 2026-09-23 실제 사고 응답 형태: 배열이 두 번째 항목 중간에서 끊김
    raw = ('[\n  {"topic": "택배 파손·배송사고 보상 받는 방법과 신고 절차", "pillar": "배송 사고", '
           '"why": "많이 검색", "search": "택배 파손 배상 절차"},\n  {"topic": "구매 후 하자 발견 시 환불과 교환 중')
    topics = planner._parse(raw)
    check("잘린 배열에서 완성된 글감만 건짐", len(topics) == 1 and topics[0]["topic"].startswith("택배 파손"),
          f"{topics}")
    check("정상 배열은 전부", len(planner._parse('[{"topic":"가"},{"topic":"나"}]')) == 2)
    try:
        planner._parse("글감을 못 드리겠습니다")
        check("아무것도 없으면 예외", False)
    except ValueError:
        check("아무것도 없으면 예외", True)

    check("재시도 횟수 넉넉히", llm.client().max_retries >= 5)
    check("기획 max_tokens 여유", cfg["planner"]["max_tokens"] >= 6000)
    check("검수 max_tokens 여유", cfg["review"]["max_tokens"] >= 4000)
    check("작성 max_tokens 여유", cfg["writer"]["max_tokens"] >= 24000)

    section("관리 에이전트 실행 집계 (모드 무관)")
    import tempfile
    from pathlib import Path
    from datetime import datetime, timezone, timedelta
    from src import fleet as fm, manager
    kst = timezone(timedelta(hours=9))
    with tempfile.TemporaryDirectory() as tmp:
        orig = fm.DATA_DIR
        fm.DATA_DIR = Path(tmp)
        try:
            fl = fm.Fleet({"max_live_per_day_account": 6}, {"default": {}},
                          [fm.Blog(id="p", name="p", blog_id="1", subject="가")])
            b = fl.blogs[0]
            now = datetime(2026, 9, 24, 10, 0, tzinfo=kst)
            for d, ok in ((21, True), (22, True), (23, False)):
                fm.mark_ran(b, "planned", ok, "x", datetime(2026, 9, d, 8, 0, tzinfo=kst), slot="08:00")
            m = manager._blog_metrics(fl, b, False, now)
            check("planned 모드 실행도 집계 (오경보 원인)", m["runs_7d"] == 3, f"{m['runs_7d']}")
            check("실패 수 집계", m["runs_failed_7d"] == 1)
            check("연속 실패 계산", m["consecutive_failures"] == 1)
            fm.mark_ran(b, "planned", False, "x", datetime(2026, 9, 24, 8, 0, tzinfo=kst), slot="08:00")
            fm.mark_ran(b, "planned", False, "x", datetime(2026, 9, 24, 12, 0, tzinfo=kst), slot="12:00")
            m2 = manager._blog_metrics(fl, b, False, now)
            check("연속 실패 3회 → 자동 중지 규칙이 다시 작동",
                  m2["consecutive_failures"] == 3 and manager._rule_based([{**m2, "enabled": True}]),
                  f"{m2['consecutive_failures']}")
        finally:
            fm.DATA_DIR = orig


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


def test_pages() -> None:
    section("소개·개인정보처리방침 페이지 템플릿")
    from scripts import setup_pages
    from src.fleet import Blog

    base = dict(id="t", name="가가남블로그", blog_id="1", subject="자동차 운전과 차량 관리", pillars=["면허"])
    plain = Blog(**base)
    about = setup_pages._template("about.html", plain, "a@b.c", "https://t.blogspot.com/p/blog-page_21.html")
    check("개인정보처리방침 링크가 실제 주소", 'href="/p/blog-page_21.html"' in about, about[-300:])
    check("page 비우면 기본 문구", "관할 기관에 문의하세요" in about)
    check("주소 모르면 옛 경로", 'href="/p/privacy-policy.html"' in setup_pages._template("about.html", plain, "a@b.c"))

    custom = Blog(**base, page={"goal": "살림 목표 문구", "gap": "살림 빈틈 문구", "caution": "살림 주의 문구"})
    about = setup_pages._template("about.html", custom, "a@b.c")
    check("주제별 문구로 교체", all(s in about for s in ("살림 목표 문구", "살림 빈틈 문구", "살림 주의 문구")))
    check("기본 문구는 빠짐", "관할 기관에 문의하세요" not in about and "법률·세무·의료 자문" not in about)

    privacy = setup_pages._template("privacy-policy.html", plain, "a@b.c")
    check("'본 블로그'는 (조사)", '(이하 "본 블로그")는' in privacy)
    check("개인정보처리방침을 먼저 만듦", setup_pages.PAGES[0]["file"] == "privacy-policy.html")


def test_scale_safety() -> None:
    section("증량·확장 안전장치 (램프업 · 비상정지 · 계정 발행 간격)")
    import json
    import tempfile
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from src import fleet as fm
    from src import fleet_run, planner
    from src import main as single
    from src.guard import Budget

    kst = timezone(timedelta(hours=9))
    at = lambda d: datetime.strptime(d, "%Y-%m-%d").replace(hour=12, tzinfo=kst)  # noqa: E731
    settings = {"blog_ramp": [[0, 1], [28, 2]], "account_ramp": [[0, 4], [28, 6], [56, 8]], "slot_step_minutes": 20}
    accounts = {"acc": {"since": "2026-09-21", "max_live_per_day_account": 8}}
    blogs = [
        fm.Blog(id="old1", name="o1", blog_id="1", subject="가", account="acc", since="2026-09-21", slots=["08:00", "14:00"]),
        fm.Blog(id="old2", name="o2", blog_id="2", subject="나", account="acc", since="2026-09-21", slots=["11:00", "17:00"]),
        fm.Blog(id="new1", name="n1", blog_id="3", subject="다", account="acc", since="2026-09-25", slots=["09:40", "15:40"]),
        fm.Blog(id="new2", name="n2", blog_id="4", subject="라", account="acc", since="2026-09-25", slots=["12:40", "18:40"]),
    ]
    fl = fm.Fleet(settings, accounts, blogs)
    empty: dict = {}
    total = lambda plan: sum(len(v) for v in plan.values())  # noqa: E731

    p = fm.planned_slots(fl, at("2026-09-25"), empty)
    check("첫 4주: 블로그당 1건, 계정 4건", total(p) == 4 and all(len(v) == 1 for v in p.values()), f"{p}")
    p = fm.planned_slots(fl, at("2026-10-19"), empty)
    check("4주 지난 블로그만 2건, 계정 상한 6", total(p) == 6 and len(p["old1"]) == 2 and len(p["new1"]) == 1, f"{p}")
    p = fm.planned_slots(fl, at("2026-11-16"), empty)
    check("8주 뒤 전부 2건 (계정 8)", total(p) == 8, f"{p}")

    capped = fm.Fleet(settings, {"acc": {"since": "2026-01-01", "max_live_per_day_account": 5}}, blogs)
    p = fm.planned_slots(capped, at("2026-11-16"), empty)
    check("최종 상한이 램프업보다 우선", total(p) == 5, f"{p}")
    check("상한 초과분은 가장 어린 블로그 슬롯부터 꺼짐",
          len(p["new1"]) == 1 and len(p["new2"]) == 1 and len(p["old1"]) + len(p["old2"]) == 3, f"{p}")

    crowded = fm.Fleet(settings, accounts, blogs + [
        fm.Blog(id="new3", name="n3", blog_id="5", subject="마", account="acc", since="2026-09-26", slots=["19:40"]),
    ])
    p = fm.planned_slots(crowded, at("2026-09-27"), empty)
    check("블로그를 붙여도 계정 총량은 그대로, 새 블로그는 대기", total(p) == 4 and p["new3"] == [], f"{p}")

    future = fm.Fleet(settings, accounts, [fm.Blog(id="f", name="f", blog_id="9", subject="바", account="acc", since="2026-10-02", slots=["08:00"])])
    check("since 가 미래면 그날까지 안 돎", fm.planned_slots(future, at("2026-09-27"), empty)["f"] == [])
    nosince = fm.Fleet(settings, accounts, [fm.Blog(id="q", name="q", blog_id="8", subject="사", account="acc", slots=["08:00", "14:00"])])
    check("since 없으면 가장 낮은 단계", len(fm.planned_slots(nosince, at("2027-01-01"), empty)["q"]) == 1)

    # 비상정지
    with tempfile.TemporaryDirectory() as tmp:
        orig_path = fm.ACCOUNT_STATE_PATH
        fm.ACCOUNT_STATE_PATH = Path(tmp) / "account_state.json"
        try:
            check("처음엔 멈춘 계정 없음", fm.account_halted("acc") == "")
            fm.halt_account("acc", "발행 403", at("2026-10-20"))
            check("403 → 계정 비상정지", "403" in fm.account_halted("acc"))
            check("비상정지 계정은 슬롯 0개", total(fm.planned_slots(fl, at("2026-10-20"))) == 0)
            check("다른 계정은 영향 없음", fm.account_halted("other") == "")
            fm.resume_account("acc", at("2026-10-22"))
            check("해제 후 램프업은 처음 단계부터", fm.account_cap(fl, "acc", at("2026-10-22")) == 4
                  and total(fm.planned_slots(fl, at("2026-10-22"))) == 4)
            check("해제 4주 뒤 다시 한 단계", fm.account_cap(fl, "acc", at("2026-11-19")) == 6)
            fm.ACCOUNT_STATE_PATH.write_text("{깨짐", encoding="utf-8")
            check("상태 파일이 깨지면 전부 멈춘 것으로 봄 (fail-closed)", fm.account_halted("acc") != "")
        finally:
            fm.ACCOUNT_STATE_PATH = orig_path

    try:
        fm.validate(fm.Fleet({**settings, "max_blogs_per_account": 3}, accounts, blogs))
        check("계정당 블로그 수 초과 거부", False)
    except ValueError as exc:
        check("계정당 블로그 수 초과 거부", "새 계정" in str(exc), str(exc))

    check("하위 축 이름 그대로", planner.match_pillar("해외직구 통관과 관세", ["청약철회 기준", "해외직구 통관과 관세"]) == "해외직구 통관과 관세")
    check("띄어쓰기만 다른 하위 축", planner.match_pillar("해외직구통관과 관세", ["청약철회 기준", "해외직구 통관과 관세"]) == "해외직구 통관과 관세")
    check("엉뚱한 하위 축은 버림", planner.match_pillar("우주 여행", ["청약철회 기준", "해외직구 통관과 관세"]) == "")

    from src import net
    check("발행 요청은 재시도 없는 세션 (중복 게시 방지)", not net.once().adapters["https://"].max_retries.total)

    # 같은 계정 발행 간격 — 밀린 슬롯 3개가 4분 안에 올라가던 문제 (2026-09-25)
    clock = [0.0]
    slept: list[float] = []
    ran: list[tuple[str, float]] = []
    two = fm.Fleet({**settings, "account_gap_minutes": 30, "run_budget_minutes": 150},
                   {"acc": accounts["acc"], "oth": {"since": "2026-09-21"}},
                   blogs[:3] + [fm.Blog(id="x1", name="x1", blog_id="7", subject="아", account="oth", since="2026-09-21", slots=["10:00"])])
    due = [(two.get("old1"), "08:00"), (two.get("new1"), "09:40"), (two.get("x1"), "10:00"), (two.get("old2"), "11:00")]

    def fake_run(blog, mode, extra):
        ran.append((blog.id, clock[0]))
        clock[0] += 180
        return True, "공개 1건 / 임시저장 0건", 0

    saved = {k: getattr(fm, k) for k in ("load_fleet", "due_slots", "credential_report", "mark_ran", "last_live_at", "account_halted")}
    saved_run = (fleet_run._run_blog, fleet_run._clock, fleet_run._sleep, fleet_run._write, fleet_run.guard.account_budget)
    try:
        fm.load_fleet = lambda *a, **k: two
        fm.due_slots = lambda f, now=None, mode="planned": due
        fm.credential_report = lambda f: {}
        fm.mark_ran = lambda *a, **k: None
        fm.last_live_at = lambda b: None
        fm.account_halted = lambda *a, **k: ""
        fleet_run._run_blog = fake_run
        fleet_run._clock = lambda: clock[0]
        fleet_run._sleep = lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s))
        fleet_run._write = lambda lines: None
        fleet_run.guard.account_budget = lambda f, acc, now=None: Budget(9, 0, 9)
        fleet_run.main([])
        order = [r[0] for r in ran]
        check("다른 계정 슬롯은 기다리지 않고 사이에 처리", order[:2] == ["old1", "x1"], f"{order}")
        acc_times = [t for bid, t in ran if bid in ("old1", "new1", "old2")]
        gaps = [b - a for a, b in zip(acc_times, acc_times[1:])]
        check("같은 계정 발행 사이 30분 이상", len(acc_times) == 3 and all(g >= 30 * 60 for g in gaps), f"{gaps}")

        # 예산을 넘기면 남은 슬롯은 다음 실행으로 (처리했다고 기록하지 않음)
        ran.clear(); clock[0] = 0.0
        two.settings["run_budget_minutes"] = 20
        fleet_run.main([])
        check("실행 시간 예산 넘으면 남은 슬롯은 넘김", [r[0] for r in ran] == ["old1", "x1"], f"{[r[0] for r in ran]}")
        two.settings["run_budget_minutes"] = 150

        # 403 → 같은 계정의 남은 슬롯은 건너뜀
        ran.clear(); clock[0] = 0.0
        fleet_run._run_blog = lambda b, m, e: (ran.append((b.id, 0)), (False, "계정 비상정지", single.EXIT_ACCOUNT_HALTED))[1]
        orig_alert = fleet_run.HALT_ALERT_PATH
        with tempfile.TemporaryDirectory() as tmp:
            fleet_run.HALT_ALERT_PATH = Path(tmp) / "halt.md"
            fleet_run.main([])
            check("비상정지 뒤 같은 계정 남은 슬롯은 실행 안 함", [r[0] for r in ran] == ["old1", "x1"], f"{ran}")
            check("비상정지 알림 파일 생성", fleet_run.HALT_ALERT_PATH.exists())
        fleet_run.HALT_ALERT_PATH = orig_alert
    finally:
        for k, v in saved.items():
            setattr(fm, k, v)
        (fleet_run._run_blog, fleet_run._clock, fleet_run._sleep, fleet_run._write, fleet_run.guard.account_budget) = saved_run


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
        # 램프업을 사실상 끄고 계정별 최종 상한만 봅니다 (램프업은 test_ramp 에서 따로)
        {"max_live_per_day_account": 6, "account_ramp": [[0, 99]]},
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
                fm.Blog(id="x", name="x", blog_id="1", slots=["06:20", "15:00"], subject="가", since="2026-01-01"),
                fm.Blog(id="y", name="y", blog_id="2", slots=["09:00"], subject="나", since="2026-01-01"),
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
            # 실패한 슬롯은 한 번 더 시도, 두 번째도 실패하면 그날은 끝
            y = f2.blogs[1]
            fm.mark_ran(y, "planned", False, "예외: 글감 응답 잘림", now, slot="09:00")
            check("실패 1회 → 다시 대상", ("y", "09:00") in [(b.id, s) for b, s in fm.due_slots(f2, now, "planned")])
            fm.mark_ran(y, "planned", False, "예외: 또 실패", now, slot="09:00")
            check("실패 2회 → 그날은 포기", ("y", "09:00") not in [(b.id, s) for b, s in fm.due_slots(f2, now, "planned")])
            check("시도 횟수 기록", fm.load_runs(y)["planned:2026-09-20:09:00"]["attempts"] == 2)
            fm.mark_ran(f2.blogs[0], "planned", True, "ok", now, slot="15:00")
            check("성공은 1회로 끝", fm.load_runs(f2.blogs[0])["planned:2026-09-20:15:00"]["attempts"] == 1
                  and ("x", "15:00") not in [(b.id, s) for b, s in fm.due_slots(f2, now, "planned")])

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
        {"blog": "ok", "action": "set_posts_per_run", "value": 2, "reason": "꾸준해서 증량"},
        {"blog": "ghost", "action": "pause", "value": None, "reason": "?"},
        {"blog": "ok", "action": "note", "value": None, "reason": "메모"},
    ]
    actions, rejected = manager._validate_actions(proposed, metrics)
    got = {(a["blog"], a["action"], a.get("value")) for a in actions}
    check("근거 없는 pause 거부", ("ok", "pause", None) not in got)
    check("연속 실패 pause 허용", ("dead", "pause", None) in got)
    check("수동 중지 블로그 resume 거부", ("manual", "resume", None) not in got)
    check("발행량 변경은 관리 모델이 못 함 (램프업만)", not any(a[1] == "set_posts_per_run" for a in got))
    check("관리 모델 지시문에 증량 행동 없음", "set_posts_per_run" not in manager.SYSTEM)
    check("없는 블로그 거부", not any(a["blog"] == "ghost" for a in actions))
    check("note 는 항상 통과", ("ok", "note", None) in got)
    check("거부 사유 기록", len(rejected) == 4, f"{len(rejected)}")
    rule = manager._rule_based(metrics)
    check("규칙 기반 폴백: 연속 3회 실패 → pause", rule and rule[0]["blog"] == "dead")


def test_manual_toggle() -> None:
    section("함대: 사람이 끈 블로그는 관리 에이전트가 다시 켜지 않음")
    import contextlib
    import io
    import tempfile
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from scripts import fleet_cli
    from src import fleet as fm
    from src import manager

    # 관리자가 멈춘 블로그를 사람이 fleet_cli disable 로 다시 끄면 by=manager 가 그대로 남아,
    # 다음 날 밤 관리 모델의 resume 이 규칙을 통과했습니다.
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 9, 25, 23, 35, tzinfo=kst)
    resume = [{"blog": "p", "action": "resume", "value": None, "reason": "원인 해소"}]
    pause = [{"blog": "p", "action": "pause", "value": None, "reason": "연속 3회 실패"}]
    with tempfile.TemporaryDirectory() as tmp:
        yml = Path(tmp) / "blogs.yaml"
        yml.write_text('blogs:\n  - {id: p, name: p, blog_id: "1", subject: 가}\n', encoding="utf-8")
        saved = {k: getattr(fm, k) for k in ("MANAGER_STATE_PATH", "DATA_DIR", "load_fleet")}
        try:
            # 실제 data/fleet/manager_state.json·blogs.yaml 대신 임시 파일과 블로그 하나짜리 함대
            fm.MANAGER_STATE_PATH = Path(tmp) / "manager_state.json"
            fm.DATA_DIR = Path(tmp)
            fm.load_fleet = lambda *a, **k: saved["load_fleet"](yml)

            def metrics() -> list[dict]:
                fl = fm.load_fleet()
                return [manager._blog_metrics(fl, fl.get("p"), False, now)]

            manager._apply(pause, now)
            m = metrics()
            ok, _ = manager._validate_actions(resume, m)
            check("관리자가 중지 → paused_by=manager, resume 허용 (전제)",
                  m[0]["paused_by"] == "manager" and len(ok) == 1, f"{m[0]['paused_by']} {ok}")

            with contextlib.redirect_stdout(io.StringIO()):
                fleet_cli._toggle("p", False)
            m = metrics()
            check("그 뒤 사람이 disable → paused_by=manual", m[0]["paused_by"] == "manual", f"{m[0]['paused_by']}")
            ok, rejected = manager._validate_actions(resume, m)
            check("사람이 끈 블로그는 관리 모델 resume 거부", ok == [] and any("수동" in r for r in rejected),
                  f"{ok} {rejected}")

            with contextlib.redirect_stdout(io.StringIO()):
                fleet_cli._toggle("p", True)
            ms = fm.load_manager_state()["blogs"]["p"]
            m = metrics()
            check("enable 도 사람 조치로 기록 (켜진 동안 by 는 판단에 안 쓰임)",
                  ms["enabled"] is True and ms.get("by") == "manual" and m[0]["paused_by"] is None, f"{ms}")
            ok, _ = manager._validate_actions(pause, [{**m[0], "consecutive_failures": 3}])
            check("사람이 켠 블로그도 연속 실패면 관리자가 중지", len(ok) == 1, f"{ok}")
        finally:
            for k, v in saved.items():
                setattr(fm, k, v)


def test_agents(cfg: dict) -> None:
    """Claude Code 서브에이전트(.claude/agents/*.md)가 공식 형식이고, 자동화와 같은 모델·지시문을 쓰는지."""
    section("Claude Code 서브에이전트")
    import re

    import yaml

    from scripts import agent_brief
    from src import fleet as fm
    from src import planner
    from src.config import ROOT

    agents: dict[str, dict] = {}
    for path in sorted((ROOT / ".claude" / "agents").glob("*.md")):
        m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", path.read_text(encoding="utf-8"), re.DOTALL)
        meta = (yaml.safe_load(m.group(1)) or {}) if m else {}
        ok = (
            bool(m) and bool(m.group(2).strip())
            and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(meta.get("name", ""))))
            and bool(str(meta.get("description", "")).strip())
            and set(re.split(r",\s*", str(meta.get("tools", "")))) <= {"Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch"}
            and (meta.get("model") in ("sonnet", "opus", "haiku", "fable", "inherit") or str(meta.get("model", "")).startswith("claude-"))
            and meta.get("effort", "high") in ("low", "medium", "high", "xhigh", "max")
            and meta.get("color", "red") in ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")
        )
        check(f"{path.name} 프론트매터 형식", ok, f"{meta}")
        agents[str(meta.get("name"))] = meta

    # 모델·effort 가 config.yaml 과 어긋나면 Claude Code 에서 쓴 글과 자동화가 쓴 글의 기준이 달라집니다.
    for name, key, with_effort in (
        ("topic-planner", "planner", True), ("post-writer", "writer", True),
        ("post-reviewer", "review", True), ("fleet-manager", "manager", False),
    ):
        meta = agents.get(name, {})
        check(f"{name}: 모델 = config {key}.model", meta.get("model") == cfg[key]["model"], f"{meta.get('model')} ≠ {cfg[key]['model']}")
        if with_effort:
            check(f"{name}: effort = config {key}.effort", meta.get("effort") == cfg[key].get("effort"), f"{meta.get('effort')}")

    # 작업 지시서는 자동화 함수를 그대로 불러 모델에 가기 직전의 요청을 가로챕니다 (모델 호출 없음).
    blog = fm.Blog(id="t", name="시험", blog_id="1", subject="세금 기초", pillars=["연말정산", "종합소득세"])
    req = agent_brief.capture(planner.propose, cfg, blog, [])
    check("지시서: 기획 = planner.SYSTEM + 블로그 주제", req["system"] == planner.SYSTEM and "연말정산" in req["messages"][0]["content"])
    req = agent_brief.capture(writer.write, cfg, mk_candidate("연말정산 의료비 공제"), "자료", [], "2026년 09월 25일", persona="표로 정리합니다.")
    check("지시서: 작성 = 자동화 모델 + 블로그 성격", req["model"] == cfg["writer"]["model"] and "표로 정리합니다." in req["system"])
    art = writer.Article(keyword="k", title="시험 제목", description="요약", labels=["연말정산"], body_html="<p>본문</p>")
    req = agent_brief.capture(reviewer.review, cfg, art, "자료")
    check("지시서: 검수 = reviewer.SYSTEM + 심사 대상 글", req["system"] == reviewer.SYSTEM and "시험 제목" in req["messages"][0]["content"])


def main() -> int:
    # 실제 data/fleet/account_state.json(비상정지 기록)이 테스트에 섞이지 않게 임시 파일로 돌립니다.
    import tempfile
    from pathlib import Path as _P
    from src import fleet as _fm
    _tmp = tempfile.TemporaryDirectory()
    _fm.ACCOUNT_STATE_PATH = _P(_tmp.name) / "account_state.json"
    cfg = load_config()
    test_tokenize()
    test_aggregate(cfg)
    test_filters(cfg)
    test_dedupe(cfg)
    test_context(cfg)
    test_internal_links(cfg)
    test_no_why_searched(cfg)
    test_pages()
    test_scale_safety()
    test_llm_robustness(cfg)
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
    test_manual_toggle()
    test_agents(cfg)

    print("\n" + "=" * 50)
    if failures:
        print(f"실패 {len(failures)}건: {', '.join(failures)}")
        return 1
    print("전체 통과 (네트워크 없이 실행됨)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
