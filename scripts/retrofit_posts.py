"""이미 공개된 글의 옛 흔적을 **하루 한 편씩, 가장 오래된 글부터** 고칩니다.

2026-09-29 이전 함대 글은 실시간 이슈용 틀로 쓰여 두 가지가 남아 있습니다.
  1) 참고한 자료 끝의 네이버 뉴스 검색 '이 주제 관련 최신 기사 더 보기' 링크
  2) 끝 문구 "…까지 공개된 보도를 기준으로 정리했습니다. 이후 상황이 달라질 수 있습니다."
안내 글에는 맞지 않아 1) 은 지우고 2) 는 새 문구(config writer.footer_note_guide, 원래 날짜 유지)로 바꿉니다.
3) 요약 카드(src/card.py)가 없는 글에는 본문에서 핵심 값을 뽑아 카드를 맨 앞에 넣습니다(글당 약 $0.02).
   뽑을 값이 없거나 카드를 못 만든 글은 retrofit.json 의 no_card 에 적어 다시 시도하지 않습니다.
본문 내용은 건드리지 않습니다.

한 번에 몰아서 고치지 않는 이유: 2026-09-20 에 짧은 시간 여러 건을 올린 계정의 API 쓰기가 막혔습니다.
수정도 쓰기 요청이므로 하루 한 건으로 나눕니다(data/fleet/retrofit.json 에 날짜를 남겨 두 번 돌아도 한 건).
글 작성 슬롯(06:20 KST 이후)과 겹치지 않게 KST 00~06시에만 고치고, 워크플로는 함대 실행과 같은
concurrency 그룹이라 동시에 돌지 않습니다.

    python scripts/retrofit_posts.py            # 오늘 몫 한 편 고치기 (이미 했으면 아무것도 안 함)
    python scripts/retrofit_posts.py --dry-run  # 고칠 대상 목록과 오늘 고칠 글의 바뀌는 부분만 보기
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import card as card_mod  # noqa: E402
from src import fleet as fleet_mod  # noqa: E402
from src import net  # noqa: E402
from src.config import load_config, load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402

STATE_PATH = fleet_mod.FLEET_DATA / "retrofit.json"
QUIET_HOURS = range(0, 6)   # KST. 첫 글 작성 슬롯이 06:20 입니다.

_NAVER_LI = re.compile(r"\s*<li>\s*<a[^>]*search\.naver\.com/search\.naver[^>]*>.*?</a>\s*</li>", re.S)
_OLD_FOOTER = re.compile(
    r"(?P<date>\d{4}년 \d{1,2}월 \d{1,2}일)까지 공개된 보도를 기준으로 정리했습니다\.\s*이후 상황이 달라질 수 있습니다\."
)


def retrofit_html(content: str, guide_note: str) -> tuple[str, list[str]]:
    """옛 흔적을 고친 본문과, 바꾼 것의 설명 목록. 고칠 게 없으면 (원문, [])."""
    changes: list[str] = []
    new, n = _NAVER_LI.subn("", content)
    if n:
        changes.append(f"네이버 뉴스 검색 링크 {n}줄 삭제")
    if guide_note:
        new, n = _OLD_FOOTER.subn(lambda m: guide_note.format(date=m.group("date")), new)
        if n:
            changes.append("끝 문구를 안내 글 문구로 교체")
    return new, changes


def needs_retrofit(content: str) -> bool:
    return bool(_NAVER_LI.search(content) or _OLD_FOOTER.search(content))


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_date": "", "done": [], "no_card": []}


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def announce(msg: str, detail: list[str] | None = None) -> None:
    """실행 결과 한 줄을 GitHub Actions 실행 화면에 올립니다 (요약 칸 + 알림 주석). 로컬에서는 출력만."""
    print(msg)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=기존 글 정리::{msg}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### 기존 글 정리\n\n" + msg + "\n\n" + "".join(f"- {d}\n" for d in detail or []) + "\n")


def _live_posts(blog_id: str) -> list[dict]:
    posts, page = [], None
    while True:
        params = {"status": "live", "maxResults": 100, "fetchBodies": "true", "view": "ADMIN"}
        if page:
            params["pageToken"] = page
        payload = net.get(
            f"{blogger.API_BASE}/blogs/{blog_id}/posts",
            params=params,
            headers={"Authorization": f"Bearer {blogger._access_token()}"},
        ).json()
        posts.extend(payload.get("items", []))
        page = payload.get("nextPageToken")
        if not page:
            return posts


def wants_card(content: str, url: str, card_on: bool, no_card: list[str]) -> bool:
    return card_on and url not in no_card and not card_mod.has_card(content)


def candidates(
    fleet: fleet_mod.Fleet, card_on: bool = False, no_card: list[str] | None = None,
) -> list[tuple[datetime, fleet_mod.Blog, dict]]:
    """고칠 글 전체를 공개 시각 오름차순으로. 꺼진 블로그와 비상정지된 계정은 뺍니다."""
    out = []
    st = fleet_mod.load_account_state()
    for b in fleet.blogs:
        if not b.enabled or fleet_mod.account_halted(b.account, st):
            continue
        fleet_mod.apply_env(fleet, b)
        for p in _live_posts(b.blog_id):
            content = p.get("content", "")
            if needs_retrofit(content) or wants_card(content, p["url"], card_on, no_card or []):
                out.append((datetime.fromisoformat(p["published"]), b, p))
    out.sort(key=lambda x: x[0])
    return out


def _patch(blog_id: str, post_id: str, content: str) -> dict:
    # 재시도 없는 세션: 쓰기 요청을 거듭 보내지 않습니다.
    resp = net.once().patch(
        f"{blogger.API_BASE}/blogs/{blog_id}/posts/{post_id}",
        headers={
            "Authorization": f"Bearer {blogger._access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={"content": content},
        timeout=30,
    )
    if resp.status_code == 403:
        raise blogger.BloggerForbidden(f"Blogger 수정 거부 (403): {resp.text[:500]}")
    if resp.status_code != 200:
        raise RuntimeError(f"Blogger 수정 실패 ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="고치지 않고 대상만 봅니다")
    args = ap.parse_args(argv)

    load_dotenv()
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if not args.dry_run:
        if state.get("last_date") == today:
            last = (state.get("done") or [{}])[-1]
            announce(f"⏭️ 건너뜀 — 오늘({today}) 몫은 이미 고쳤습니다: {last.get('url', '')}",
                     [f"남은 글 {state.get('remaining', '?')}편"])
            return 0
        if now.hour not in QUIET_HOURS:
            announce(f"⏭️ 건너뜀 — 지금은 {now:%H:%M} KST. 글 작성 시간대와 겹치지 않게 00~06시에만 고칩니다.")
            return 0

    fleet = fleet_mod.load_fleet()
    cfg = load_config()
    card_on = bool((cfg.get("card") or {}).get("enabled"))
    todo = candidates(fleet, card_on, state.get("no_card", []))
    print(f"고칠 글 {len(todo)}편 (오래된 순)")
    for when, b, p in todo[:30]:
        print(f"  {when:%m-%d %H:%M}  {b.id:13} {p['url']}")
    if not todo:
        if not args.dry_run:
            state.update(remaining=0, checked_at=now.isoformat(timespec="minutes"), last_result="남은 글 없음")
            save_state(state)
        announce("✅ 고칠 글이 없습니다 — 모두 고쳤습니다.")
        return 0

    when, blog, post = todo[0]
    guide = (cfg.get("writer") or {}).get("footer_note_guide", "")
    new, changes = retrofit_html(post["content"], guide)
    add_card = wants_card(post["content"], post["url"], card_on, state.get("no_card", []))
    print(f"\n오늘 고칠 글: {blog.id} {post['url']} ({when:%Y-%m-%d %H:%M}) — "
          f"{', '.join(changes + (['요약 카드 추가'] if add_card else []))}")
    if args.dry_run:
        announce(f"🔍 미리보기 — 고칠 글 {len(todo)}편, 다음 차례 {blog.id} {post['url']}")
        return 0

    state.update(remaining=len(todo), checked_at=now.isoformat(timespec="minutes"))
    if add_card:
        text, cost = card_mod.extract(cfg, post.get("title", ""), post["content"])
        block = card_mod.make(cfg, blog.id, blog.name, text, post["url"]) if text else ""
        print(f"카드 글 뽑기 약 ${cost:.3f}: {text[:200]!r}")
        if block:
            new = block + new
            changes.append("요약 카드 추가")
        elif not card_mod.parse(text):
            # 뽑을 값이 없거나 형식이 안 맞는 글은 다시 해도 같으므로 건너뜁니다.
            state.setdefault("no_card", []).append(post["url"])
            print("카드로 만들 값이 없어 이 글은 카드 없이 둡니다 (no_card 에 기록).")
        else:
            print("카드 업로드에 실패했습니다. 다음 실행에서 다시 시도합니다.")
    if not changes:
        state["last_result"] = f"{today} 고칠 것 없음 ({post['url']}, 카드 실패 또는 뽑을 값 없음)"
        save_state(state)
        announce(f"⚠️ 바꾼 것 없음 — {blog.id} {post['url']}: 카드를 만들지 못했습니다. 로그를 확인하세요.",
                 [f"남은 글 {len(todo)}편"])
        return 0

    fleet_mod.apply_env(fleet, blog)
    try:
        _patch(blog.blog_id, post["id"], new)
    except blogger.BloggerForbidden as exc:
        # 발행 때와 같은 규칙: 쓰기 차단 신호면 그 계정 전체를 멈춥니다(글 작성도 멈춤).
        fleet_mod.halt_account(blog.account, f"{blog.id} 기존 글 수정 403: {exc}")
        state["last_result"] = f"{today} ⛔ 수정 거부(403) — 계정 '{blog.account}' 비상정지"
        save_state(state)
        announce(f"⛔ 계정 '{blog.account}' 비상정지 — {exc}")
        return 3
    state["last_date"] = today
    state["remaining"] = len(todo) - 1
    state["last_result"] = f"{today} 고침 — {blog.id} {post['url']}"
    state.setdefault("done", []).append(
        {"at": now.isoformat(timespec="seconds"), "blog": blog.id, "url": post["url"], "changes": changes}
    )
    save_state(state)
    announce(f"✅ 고침 — {blog.id} {post['url']} (남은 글 {len(todo) - 1}편)",
             changes + [f"글 제목: {post.get('title', '')}", f"지금까지 {len(state['done'])}편 완료"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
