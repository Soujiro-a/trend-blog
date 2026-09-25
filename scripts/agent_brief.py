"""Claude Code 서브에이전트(.claude/agents/*.md)에게 줄 작업 지시서.

자동화(src/*.py)가 모델에 보내는 **요청 원문**(system·user)을 그대로 뽑아 줍니다.
서브에이전트는 이 출력을 자기 규칙으로 삼아 일하므로, 자동화 프롬프트를 고치면
서브에이전트도 따로 손대지 않아도 같은 기준으로 움직입니다.

자동화 코드는 건드리지 않습니다. 실제 함수(planner.propose · writer.write · reviewer.review)를
그대로 부르고, 모델을 부르기 직전에 요청만 가로챕니다. 모델 호출도 비용도 없습니다.

    python scripts/agent_brief.py planner  --blog gaganam1
    python scripts/agent_brief.py writer   --blog gaganam1 --topic "자동차 정기검사 준비물" [--search "자동차 정기검사"] [--pillar "..."]
    python scripts/agent_brief.py reviewer --dir out/agents/gaganam1/20260925-221500

writer 는 참고 기사를 자동화와 같은 방식으로 모으고(네트워크), 작업 폴더
out/agents/<블로그>/<시각>/ 에 brief.json · context.md 를 남깁니다. 작성 에이전트가 같은 폴더에
draft.md 를 쓰면, reviewer 가 그 폴더를 읽어 검수 요청 원문과 미리보기(preview.html)를 만듭니다.
out/ 은 깃에 올라가지 않고, data/ 의 이력 파일은 읽기만 합니다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import planner, research, reviewer, state, writer  # noqa: E402
from src.config import OUT_DIR, ROOT, load_config  # noqa: E402
from src.main import RunContext  # noqa: E402
from src.publishers import local  # noqa: E402
from src.state import KST  # noqa: E402
from src.trends import Candidate, Variant  # noqa: E402

AGENT_DIR = OUT_DIR / "agents"


class _Captured(Exception):
    def __init__(self, request: dict):
        super().__init__("모델 요청을 가로챘습니다")
        self.request = request


class _CaptureClient:
    """anthropic.Anthropic 대역. 모델에 보낼 요청을 받는 즉시 멈춥니다."""

    def __init__(self) -> None:
        self.messages = self

    def create(self, **request):
        raise _Captured(request)

    def stream(self, **request):
        raise _Captured(request)


def capture(fn, *args, **kwargs) -> dict:
    """자동화 함수를 그대로 부르고, 모델 호출 직전의 요청(model·system·messages …)을 돌려줍니다."""
    try:
        fn(*args, client=_CaptureClient(), **kwargs)
    except _Captured as c:
        return c.request
    raise RuntimeError(f"{fn.__module__}.{fn.__name__} 가 모델을 부르지 않았습니다")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _print_request(title: str, request: dict, notes: list[str]) -> None:
    effort = (request.get("output_config") or {}).get("effort", "-")
    print(f"# {title} — 자동화가 {request.get('model')} (effort {effort}) 에 보내는 요청 원문")
    print()
    for line in notes:
        print(line)
    print("\n===== SYSTEM =====\n")
    print(request.get("system", ""))
    for m in request.get("messages", []):
        print(f"\n===== {str(m.get('role', '')).upper()} =====\n")
        print(m.get("content", ""))


def _blog(blog_id: str) -> tuple[fleet_mod.Blog, dict]:
    """함대 실행(src/main.py)과 같은 방식으로 블로그와 블로그별 설정을 만듭니다."""
    fleet = fleet_mod.load_fleet()
    blog = fleet.get(blog_id)
    return blog, fleet_mod.apply_config(load_config(), fleet, blog)


def cmd_planner(args) -> int:
    blog, cfg = _blog(args.blog)
    history = state.load(blog.history_path)
    request = capture(planner.propose, cfg, blog, history)
    dd = cfg["dedupe"]
    _print_request("글감 기획 (src/planner.py)", request, [
        f"- 블로그: {blog.name} ({blog.id})",
        f"- 자동화는 응답 뒤에 최근 {dd['history_days']}일 이력과 유사도 {dd['similarity_threshold']} 이상인 "
        "글감을 버리고(state.filter_seen), 남은 것 중 위에서부터 씁니다.",
    ])
    return 0


def cmd_writer(args) -> int:
    blog, cfg = _blog(args.blog)
    history = state.load(blog.history_path)
    now = datetime.now(KST)
    search = (args.search or args.topic).strip()

    # planner.propose 가 만드는 1순위 글감과 같은 모양입니다.
    candidate = Candidate(
        keyword=args.topic.strip(),
        seen=[Variant(args.topic.strip(), "planner", 1), Variant(search, "planner", 1)],
        sources={"planner": 1},
        score=round(1.0 - 0.02, 4),
    )
    candidate.pillar = planner.match_pillar((args.pillar or "").strip(), blog.pillars)

    refs = research.gather(cfg, candidate)
    if len(refs) < 2:
        print(
            f"참고 기사가 {len(refs)}건뿐입니다. 자동화라면 이 글감은 건너뜁니다(src/main.py). "
            "--search 로 검색어를 바꿔 다시 시도하세요.",
            file=sys.stderr,
        )
        return 1

    # src/main.py 와 같은 순서: 참고 기사 블록 + 같은 블로그의 공개 글(내부 링크 후보)
    context = research.to_context(cfg, candidate, refs)
    context += research.internal_links_block(history, candidate.keyword)
    request = capture(
        writer.write, cfg, candidate, context, refs, now.strftime("%Y년 %m월 %d일"),
        mode=blog.content_mode, persona=RunContext(blog=blog).persona,
    )

    run_dir = AGENT_DIR / blog.id / now.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "context.md").write_text(context, encoding="utf-8")
    (run_dir / "brief.json").write_text(
        json.dumps(
            {"blog": blog.id, "topic": candidate.keyword, "search": search,
             "pillar": candidate.pillar, "created_at": now.isoformat(timespec="seconds")},
            ensure_ascii=False, indent=1,
        ),
        encoding="utf-8",
    )
    _print_request("글 작성 (src/writer.py)", request, [
        f"- 블로그: {blog.name} ({blog.id}) · 글감: {candidate.keyword} · 하위 축: {candidate.pillar or '(없음)'}",
        f"- 참고 기사 {len(refs)}건 · 작업 폴더: {_rel(run_dir)}",
        f"- 결과는 {_rel(run_dir)}/draft.md 에 SYSTEM 의 출력 형식 그대로(<<<TITLE>>> … <<<BODY>>>) 저장하세요.",
    ])
    return 0


def cmd_reviewer(args) -> int:
    run_dir = Path(args.dir)
    for name in ("brief.json", "context.md", "draft.md"):
        if not (run_dir / name).exists():
            print(f"{_rel(run_dir)} 에 {name} 가 없습니다.", file=sys.stderr)
            return 1
    brief = json.loads((run_dir / "brief.json").read_text(encoding="utf-8"))
    context = (run_dir / "context.md").read_text(encoding="utf-8")
    blog, cfg = _blog(brief["blog"])

    # 자동화의 파서로 읽습니다. 여기서 실패하는 초안은 자동화에서도 '작성 실패'로 버려집니다.
    try:
        article = writer._parse(
            (run_dir / "draft.md").read_text(encoding="utf-8"), brief["topic"], cfg["writer"]["model"]
        )
    except writer.SkippedByModel as exc:
        print(f"초안이 SKIP 입니다. 검수할 글이 없습니다: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"초안 형식이 자동화 형식과 다릅니다(src/writer.py _parse): {exc}", file=sys.stderr)
        return 1

    # src/main.py 와 같은 라벨 규칙: 하위 축 하나, 없으면 작성 모델 태그 앞 2개
    article.labels = [brief["pillar"]] if brief.get("pillar") else article.labels[:2]
    request = capture(reviewer.review, cfg, article, context)

    # 발행될 때 붙는 라벨(블로그 고정 라벨 + 위 라벨)로 미리보기를 만듭니다. (publishers/blogger.py 와 같은 규칙)
    labels = list(dict.fromkeys([*cfg["publish"].get("default_labels", []), *article.labels]))
    preview = run_dir / "preview.html"
    preview.write_text(
        local.PAGE.format(
            title=article.title, description=article.description, keyword=article.keyword,
            labels=", ".join(labels), body=article.body_html,
        ),
        encoding="utf-8",
    )
    _print_request("검수 (src/reviewer.py)", request, [
        f"- 블로그: {blog.name} ({blog.id}) · 제목: {article.title}",
        f"- 자동화의 판정: verdict 가 publish 이고 score ≥ {cfg['review'].get('min_score', 80)} 이면 공개, "
        "reject 면 올리지 않음, 그 밖에는 임시저장 (src/main.py decide)",
        f"- 결과 JSON 은 {_rel(run_dir)}/review.json 에 저장하세요. 미리보기: {_rel(preview)}",
    ])
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="서브에이전트용 작업 지시서 (자동화 요청 원문)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("planner", help="글감 기획 요청 원문")
    p.add_argument("--blog", required=True)
    p.set_defaults(fn=cmd_planner)
    w = sub.add_parser("writer", help="참고 기사를 모으고 글 작성 요청 원문을 만듭니다")
    w.add_argument("--blog", required=True)
    w.add_argument("--topic", required=True, help="글감 (글 제목이 될 검색어형 주제)")
    w.add_argument("--search", help="참고 기사 검색어 (기본: 글감)")
    w.add_argument("--pillar", help="하위 축 이름 (blogs.yaml 의 pillars 중 하나)")
    w.set_defaults(fn=cmd_writer)
    r = sub.add_parser("reviewer", help="작업 폴더의 초안으로 검수 요청 원문을 만듭니다")
    r.add_argument("--dir", required=True, help="writer 가 만든 작업 폴더")
    r.set_defaults(fn=cmd_reviewer)
    args = parser.parse_args(argv)

    try:
        return args.fn(args)
    except (KeyError, ValueError, state.HistoryCorrupted) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
