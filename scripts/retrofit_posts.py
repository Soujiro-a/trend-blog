"""이미 공개된 글의 옛 흔적을 **하루 한 편씩, 가장 오래된 글부터** 고칩니다.

2026-09-29 이전 함대 글은 실시간 이슈용 틀로 쓰여 두 가지가 남아 있습니다.
  1) 참고한 자료 끝의 네이버 뉴스 검색 '이 주제 관련 최신 기사 더 보기' 링크
  2) 끝 문구 "…까지 공개된 보도를 기준으로 정리했습니다. 이후 상황이 달라질 수 있습니다."
안내 글에는 맞지 않아 1) 은 지우고 2) 는 새 문구(config writer.footer_note_guide, 원래 날짜 유지)로 바꿉니다.
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
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
        return {"last_date": "", "done": []}


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


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


def candidates(fleet: fleet_mod.Fleet) -> list[tuple[datetime, fleet_mod.Blog, dict]]:
    """고칠 글 전체를 공개 시각 오름차순으로. 꺼진 블로그와 비상정지된 계정은 뺍니다."""
    out = []
    st = fleet_mod.load_account_state()
    for b in fleet.blogs:
        if not b.enabled or fleet_mod.account_halted(b.account, st):
            continue
        fleet_mod.apply_env(fleet, b)
        for p in _live_posts(b.blog_id):
            if needs_retrofit(p.get("content", "")):
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
            print(f"오늘({today}) 몫은 이미 고쳤습니다.")
            return 0
        if now.hour not in QUIET_HOURS:
            print(f"지금은 {now:%H:%M} KST — 글 작성 시간대와 겹치지 않게 00~06시에만 고칩니다.")
            return 0

    fleet = fleet_mod.load_fleet()
    todo = candidates(fleet)
    print(f"고칠 글 {len(todo)}편 (오래된 순)")
    for when, b, p in todo[:30]:
        print(f"  {when:%m-%d %H:%M}  {b.id:13} {p['url']}")
    if not todo:
        print("모두 고쳤습니다.")
        return 0

    when, blog, post = todo[0]
    guide = (load_config().get("writer") or {}).get("footer_note_guide", "")
    new, changes = retrofit_html(post["content"], guide)
    print(f"\n오늘 고칠 글: {blog.id} {post['url']} ({when:%Y-%m-%d %H:%M}) — {', '.join(changes)}")
    if args.dry_run:
        return 0

    fleet_mod.apply_env(fleet, blog)
    try:
        _patch(blog.blog_id, post["id"], new)
    except blogger.BloggerForbidden as exc:
        # 발행 때와 같은 규칙: 쓰기 차단 신호면 그 계정 전체를 멈춥니다(글 작성도 멈춤).
        fleet_mod.halt_account(blog.account, f"{blog.id} 기존 글 수정 403: {exc}")
        print(f"⛔ 계정 '{blog.account}' 비상정지 — {exc}")
        return 3
    state["last_date"] = today
    state.setdefault("done", []).append(
        {"at": now.isoformat(timespec="seconds"), "blog": blog.id, "url": post["url"], "changes": changes}
    )
    save_state(state)
    print(f"고쳤습니다. 남은 글 {len(todo) - 1}편.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
