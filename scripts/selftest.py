"""API 키 없이 파이프라인 전체를 점검합니다.

Claude 응답만 가짜로 채우고, 수집 → 집계 → 필터 → 리서치 → 파싱 → 저장 →
이력 기록까지 실제 코드로 돌립니다.

    python scripts/selftest.py
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import filters, research, state, trends, writer  # noqa: E402
from src.config import load_config  # noqa: E402
from src.publishers import local  # noqa: E402

FAKE_RESPONSE = """<<<TITLE>>>
테스트 제목: 자동화 파이프라인 점검용 글
<<<DESCRIPTION>>>
이 글은 파이프라인 점검을 위한 가짜 응답입니다. 실제 발행에는 쓰이지 않습니다.
<<<LABELS>>>
테스트, 자동화, 점검
<<<BODY>>>
<p>도입부 문단입니다.</p>
<h2>첫 번째 소제목</h2>
<p>본문 내용입니다.</p>
<h2>자주 묻는 질문</h2>
<h3>Q. 이건 무엇인가요?</h3>
<p>A. 점검용 가짜 글입니다.</p>
<p><em>이 글은 AI의 도움을 받아 작성했습니다.</em></p>
"""

PASS, FAIL = "  [OK]", "  [FAIL]"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {name}")
    else:
        print(f"{FAIL} {name} {detail}")
        failures.append(name)


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 4200
    output_tokens: int = 2600


@dataclass
class _Message:
    content: list
    usage: _Usage
    stop_reason: str = "end_turn"
    stop_details: None = None


class FakeClient:
    """anthropic.Anthropic 의 messages.stream 만 흉내 냅니다."""

    def __init__(self, raw: str = FAKE_RESPONSE):
        self.raw = raw
        self.messages = self

    @contextmanager
    def stream(self, **kwargs):
        self.last_kwargs = kwargs

        class _Stream:
            def get_final_message(_self):
                return _Message(content=[_Block(self.raw)], usage=_Usage())

        yield _Stream()


def main() -> int:
    cfg = load_config()
    print("\n== 1. 실시간 검색어 수집 ==")
    keyword_items, headline_items = trends.collect(cfg)
    by_source = {}
    for i in keyword_items + headline_items:
        by_source[i.source] = by_source.get(i.source, 0) + 1
    for s, n in sorted(by_source.items()):
        print(f"       {s}: {n}개")
    check("키워드 소스 3곳 중 2곳 이상 응답", len(by_source) >= 4, f"실제 {by_source}")
    check("최소 키워드 확보", len(keyword_items) >= cfg["run"]["min_keywords"])

    print("\n== 2. 집계 · 필터 ==")
    candidates = trends.aggregate(cfg, keyword_items, headline_items)
    kept, rejected = filters.apply(cfg, candidates)
    check("후보 생성됨", len(candidates) > 0)
    check("필터 통과 후보 있음", len(kept) > 0)
    check(
        "대표 키워드가 맥락을 유지함(1글자 토막 없음)",
        all(len(c.keyword) >= cfg["filters"]["min_keyword_length"] for c in kept),
    )
    print(f"       후보 {len(candidates)}개 → 통과 {len(kept)}개 → 제외 {len(rejected)}개")
    for c in kept[:3]:
        print(f"       · {c.keyword} (점수 {c.score}, 소스 {list(c.sources)})")

    print("\n== 3. 리서치 ==")
    target = kept[0]
    refs = research.gather(cfg, target)
    context = research.to_context(cfg, target, refs)
    check("참고 기사 수집됨", len(refs) > 0, f"'{target.keyword}' 기사 {len(refs)}건")
    check("리서치 컨텍스트에 키워드 포함", target.keyword in context)
    check(
        "컨텍스트 길이 제한 준수",
        len(context) <= cfg["research"]["max_context_chars"] + 20,
    )
    print(f"       '{target.keyword}' 기사 {len(refs)}건, 컨텍스트 {len(context)}자")

    print("\n== 4. 글 작성 (가짜 응답) ==")
    article = writer.write(
        cfg, target, context, refs, "2026년 09월 18일", client=FakeClient()
    )
    check("제목 파싱", article.title.startswith("테스트 제목"))
    check("요약 파싱", "파이프라인" in article.description)
    check("태그 파싱", article.labels == ["테스트", "자동화", "점검"], f"실제 {article.labels}")
    check("본문 파싱", "<h2>" in article.body_html)
    check("본문에 마커 잔여물 없음", "<<<" not in article.body_html)
    check("비용 계산", 0 < article.cost_usd < 1, f"${article.cost_usd:.4f}")
    print(f"       예상 비용 ${article.cost_usd:.4f} (모델 {article.model})")

    print("\n== 5. SKIP 처리 ==")
    try:
        writer.write(
            cfg, target, context, refs, "2026년 09월 18일",
            client=FakeClient("SKIP: 개인 사생활 관련 주제입니다"),
        )
        check("모델 SKIP 시 예외 발생", False, "예외가 안 났습니다")
    except writer.SkippedByModel:
        check("모델 SKIP 시 예외 발생", True)

    print("\n== 6. 로컬 저장 ==")
    post = local.publish(cfg, article)
    saved = Path(post["url"])
    check("HTML 파일 생성", saved.exists(), str(saved))
    if saved.exists():
        html = saved.read_text(encoding="utf-8")
        check("HTML 에 제목 포함", article.title in html)
        check("HTML 에 본문 포함", "첫 번째 소제목" in html)

    print("\n== 7. 이력 · 중복 차단 ==")
    with tempfile.TemporaryDirectory() as tmp:
        hist_path = Path(tmp) / "history.json"
        entries = state.record([], keyword=target.keyword, title=article.title)
        state.save(entries, hist_path)
        reloaded = state.load(hist_path)
        check("이력 저장/복원", len(reloaded) == 1 and reloaded[0]["keyword"] == target.keyword)

        fresh, skipped = state.filter_seen(cfg, kept, reloaded)
        check(
            "방금 쓴 키워드는 다시 안 뽑힘",
            all(c.keyword != target.keyword for c in fresh),
            f"건너뜀 {len(skipped)}건",
        )

    print("\n" + "=" * 46)
    if failures:
        print(f"실패 {len(failures)}건: {', '.join(failures)}")
        return 1
    print("전체 통과. 남은 것은 실제 Claude API 호출과 Blogger 인증뿐입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
