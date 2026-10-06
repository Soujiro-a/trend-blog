"""함대 글의 '검색 설명'(메타 설명)을 채울 목록을 만들고, 넣었는지 확인합니다.

Blogger API(v3)에는 글별 검색 설명 칸이 없습니다. 비워 두면 모든 글 페이지의 description·og:description 이
블로그 전체 설명과 똑같이 나갑니다(2026-10-03 확인). 글 편집 화면의 글 설정 → '검색 설명' 칸에 넣으면
그 글 페이지에 바로 반영되므로, 앱 예약 작업이 내장 브라우저로 이 목록을 하나씩 넣습니다.

설명은 작성 모델이 쓴 것(이력의 description)을 그대로 쓰고, 그 기능이 생기기 전 글은
config search_description.model 이 본문에서 한 번 만들어 out/blogger/search_desc.json 에 저장해 둡니다.

    python scripts/search_desc.py next --limit 10   # 넣을 글 (JSON 한 줄에 하나)
    python scripts/search_desc.py next --fresh      # 매시간 작업용: 최근 72시간 새 글만 (몇 초)
    python scripts/search_desc.py verify <url>      # 페이지에 반영됐는지 확인하고 기록
    python scripts/search_desc.py mark <url> failed # 넣지 못한 글 기록
    python scripts/search_desc.py status            # 블로그별 완료/남음
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fleet as fleet_mod  # noqa: E402
from src import net, state  # noqa: E402
from src.config import OUT_DIR, load_config, load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402

LOG_PATH = OUT_DIR / "blogger" / "search_desc.json"
FRESH_HOURS = 72   # --fresh: 이 시간 안에 공개된 글만 봅니다 (매시간 작업이 글 올라온 직후 넣도록)

SYSTEM = """블로그 글의 검색 설명(메타 설명)을 씁니다. 검색 결과 제목 아래에 나오는 문장입니다.
- 1~2문장, 80~140자. 150자를 넘기지 마세요.
- 독자가 검색했을 표현을 앞쪽에 두고, 글에서 무엇을 알 수 있는지 구체적으로 씁니다.
- 본문에 있는 사실만 씁니다. 본문에 없는 숫자·단정은 넣지 마세요.
- 따옴표, 이모지, 해시태그, "이 글에서는" 같은 머리말 없이 설명 문장만 출력합니다."""


def load_log() -> dict:
    try:
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"drafts": {}, "done": {}, "failed": {}}


def save_log(log: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def fit(text: str, max_chars: int = 150) -> str:
    """한 줄로 정리하고 max_chars 안으로. 넘치면 마지막 문장 끝에서, 문장 끝이 없으면 글자 수로 자릅니다."""
    t = re.sub(r"\s+", " ", text).strip().strip("\"'“”")
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
    if ends and ends[-1] >= max_chars // 2:
        return cut[: ends[-1]].strip()
    return cut[: max_chars - 1].rstrip() + "…"


def page_description(url: str) -> str:
    """공개 페이지의 <meta name='description'> 값. 없으면 빈 문자열."""
    resp = net.session().get(url, params={"nocache": datetime.now().strftime("%H%M%S")}, timeout=20)
    m = re.search(r"<meta\s+content='([^']*)'\s+name='description'\s*/?>", resp.text) or \
        re.search(r'<meta\s+name="description"\s+content="([^"]*)"', resp.text)
    return html.unescape(m.group(1)) if m else ""


def _live_posts(blog_id: str, since: datetime | None = None) -> list[dict]:
    posts, page = [], None
    while True:
        params = {"status": "live", "maxResults": 100, "fetchBodies": "true", "view": "ADMIN"}
        if since:
            params["startDate"] = since.isoformat(timespec="seconds")
        if page:
            params["pageToken"] = page
        payload = net.get(
            f"{blogger.API_BASE}/blogs/{blog_id}/posts", params=params,
            headers={"Authorization": f"Bearer {blogger._access_token()}"},
        ).json()
        posts.extend(payload.get("items", []))
        page = payload.get("nextPageToken")
        if not page:
            return posts


def _generate(cfg: dict, title: str, body_html: str) -> str:
    from src import card, llm
    sd = cfg.get("search_description") or {}
    response = llm.client().messages.create(
        model=sd.get("model", "claude-sonnet-5-5"),
        max_tokens=2000,
        system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": f"제목: {title}\n\n본문:\n{card._plain(body_html)[:6000]}"}],
    )
    return llm.text_of(response, f"[{title[:20]}] 검색 설명").strip()


def todo(fleet: fleet_mod.Fleet, cfg: dict, log: dict, since: datetime | None = None) -> list[dict]:
    """설명을 넣어야 할 글 (오래된 순, since 가 있으면 그 뒤에 공개된 글만). 꺼진 블로그와 비상정지된 계정은 뺍니다."""
    out = []
    st = fleet_mod.load_account_state()
    for b in fleet.blogs:
        if not b.enabled or fleet_mod.account_halted(b.account, st):
            continue
        fleet_mod.apply_env(fleet, b)
        written = {h.get("url"): h.get("description", "") for h in state.load(b.history_path)}
        for p in _live_posts(b.blog_id, since):
            if p["url"] in log["done"]:
                continue
            out.append({"blog": b.id, "blog_id": b.blog_id, "post_id": p["id"], "url": p["url"],
                        "title": p.get("title", ""), "published": p["published"],
                        "written": written.get(p["url"], ""), "content": p.get("content", "")})
    out.sort(key=lambda x: datetime.fromisoformat(x["published"]))
    return out


def cmd_next(args, fleet, cfg) -> int:
    log = load_log()
    max_chars = (cfg.get("search_description") or {}).get("max_chars", 150)
    picked = []
    home_desc: dict[str, str] = {}
    since = datetime.now(KST) - timedelta(hours=FRESH_HOURS) if args.fresh else None
    for item in todo(fleet, cfg, log, since):
        if item["url"] in log["failed"] and log["failed"][item["url"]] >= 2:
            continue   # 두 번 실패한 글은 사람이 보도록 남겨 둡니다 (status 에 나옵니다)
        # 이미 글별 설명이 있는 글(첫 화면 설명과 다름 — 사람이 넣었거나 이전 실행)은 완료로 기록하고 넘어갑니다.
        if item["blog"] not in home_desc:
            home_desc[item["blog"]] = page_description(f"https://{item['blog']}.blogspot.com/")
        current = page_description(item["url"])
        if current and current != home_desc[item["blog"]]:
            log["done"][item["url"]] = datetime.now(KST).isoformat(timespec="seconds")
            continue
        desc = item["written"] or log["drafts"].get(item["url"])
        if not desc:
            desc = _generate(cfg, item["title"], item["content"])
        desc = fit(desc, max_chars)
        if not desc:
            continue
        log["drafts"][item["url"]] = desc
        picked.append({"blog": item["blog"], "url": item["url"],
                       "edit_url": f"https://www.blogger.com/blog/post/edit/{item['blog_id']}/{item['post_id']}",
                       "description": desc})
        if len(picked) >= args.limit:
            break
    save_log(log)
    for p in picked:
        print(json.dumps(p, ensure_ascii=False))
    if not picked:
        print("# 새로 올라온 글이 없습니다." if args.fresh else "# 검색 설명을 넣을 글이 없습니다.")
    return 0


def cmd_verify(args, fleet, cfg) -> int:
    log = load_log()
    want = log["drafts"].get(args.url, "")
    got = page_description(args.url)
    if want and got == want:
        log["done"][args.url] = datetime.now(KST).isoformat(timespec="seconds")
        log["failed"].pop(args.url, None)
        save_log(log)
        print(f"OK 반영됨: {args.url}")
        return 0
    print(f"아직 다름: {args.url}\n  넣을 값: {want}\n  페이지 : {got or '(글별 설명 없음)'}")
    return 1


def cmd_mark(args, fleet, cfg) -> int:
    log = load_log()
    log["failed"][args.url] = log["failed"].get(args.url, 0) + 1
    save_log(log)
    print(f"기록: failed {args.url} ({log['failed'][args.url]}회)")
    return 0


def cmd_status(args, fleet, cfg) -> int:
    log = load_log()
    rows: dict[str, list[int]] = {}
    for item in todo(fleet, cfg, {"done": {}}):
        r = rows.setdefault(item["blog"], [0, 0, 0])   # 전체, 완료, 실패 2회 이상
        r[0] += 1
        r[1] += item["url"] in log["done"]
        r[2] += log["failed"].get(item["url"], 0) >= 2 and item["url"] not in log["done"]
    print("블로그          전체  완료  남음  (실패 2회↑)")
    for blog, (total, done, failed) in rows.items():
        print(f"{blog:14} {total:5} {done:5} {total - done:5}  {failed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("next")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--fresh", action="store_true", help=f"최근 {FRESH_HOURS}시간 안에 공개된 글만 (매시간 작업용, 빠름)")
    p = sub.add_parser("verify")
    p.add_argument("url")
    p = sub.add_parser("mark")
    p.add_argument("url")
    p.add_argument("result", choices=["failed"])
    sub.add_parser("status")
    args = ap.parse_args(argv)

    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    fleet = fleet_mod.load_fleet()
    cfg = load_config()
    return {"next": cmd_next, "verify": cmd_verify, "mark": cmd_mark, "status": cmd_status}[args.cmd](args, fleet, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
