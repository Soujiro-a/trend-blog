"""이미 공개된 글을 **같은 주소에서** 지금 기준으로 다시 씁니다. 겹치는 글 묶음을 한 편으로 합칠 때도 씁니다.

2026-10-29 gaganam1 이 애드센스에서 '가치가 별로 없는 콘텐츠'로 거절됐습니다. 원인은 (1) 같은 제도를 상황만 바꿔
여러 글로 쪼갠 묶음(운전면허 15편, 그중 적성검사 7편)과 (2) 9월 초기의 뉴스 요약형 글이었습니다.
retrofit_posts.py 는 문장만 다듬어서 이런 글을 고칠 수 없습니다. 이 스크립트는 지금 쓰는 작성 지시문·모델로
공식 자료를 다시 조사해 글을 새로 쓰고, 같은 검수(가치 부족 기준 포함)를 통과한 글만 반영합니다.

합칠 글(--absorb)은 **다뤄야 할 상황 목록**으로만 넘깁니다(제목과 소제목). 기존 글 본문은 AI 가 쓴 것이라
사실 근거로 쓰지 않습니다. 사실은 이번에 새로 모은 공식 안내 자료와 기사에서만 가져옵니다.

    python scripts/rewrite_post.py preview <주소> [--absorb <주소> ...] [--topic "글감"]  # 다시 써서 out/rewrite/ 에 저장
    python scripts/rewrite_post.py apply <주소>                  # 저장한 글을 그 주소에 반영 (요약 카드 포함)
    python scripts/rewrite_post.py revert <주소> --into <주소>    # 합쳐진 글을 임시저장으로 (삭제 아님, 되돌릴 수 있음)
    python scripts/rewrite_post.py run-queue [--dry-run]          # 밤마다: 대기열(data/fleet/rewrite_queue.json)에서 한 걸음

쓰기 요청은 한 번에 몰지 않습니다(2026-09-20 짧은 시간 여러 건으로 계정 쓰기가 막힘). run-queue 는 하루 밤에
블로그마다 다시 쓰기 1건 + 임시저장 1건 + 링크 고치기 2건까지만 하고, 임시저장은 그 글을 합친 중심 글이 반영된 뒤에만 합니다.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import card as card_mod  # noqa: E402
from src import fleet as fleet_mod  # noqa: E402
from src import net, research, reviewer, state, writer  # noqa: E402
from src.config import DATA_DIR, OUT_DIR, load_config, load_dotenv  # noqa: E402
from src.main import _revise_loop  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402
from src.trends import Candidate, Variant  # noqa: E402

PREVIEW_DIR = OUT_DIR / "rewrite"
QUEUE_PATH = DATA_DIR / "fleet" / "rewrite_queue.json"
FAIL_REPORT = OUT_DIR / "rewrite_failures.md"   # 있으면 워크플로가 이슈를 열어 메일로 알립니다
QUIET_HOURS = range(22, 24), range(0, 6)
RELINK_PER_NIGHT = 2   # 블로그마다 하룻밤 링크 고치기 편수 (2026-10-31: 1편이면 임시저장 속도를 못 따라가 깨진 링크가 쌓임)
HUB_LENGTH = (4000, 5500)      # 묶음을 합친 중심 글은 상황이 많아 기본(2,500~3,000)보다 길게
MAX_OFFICIAL_PAGES = 10


def path_of(url: str) -> str:
    """https://x.blogspot.com/2026/10/a.html?m=1 → /2026/10/a.html"""
    return urllib.parse.urlparse(url).path


def outline(content: str) -> list[str]:
    """글의 소제목(h2·h3) 글자만. 요약 카드·참고 자료·자주 묻는 질문 같은 공통 섹션은 뺍니다."""
    skip = ("함께 읽으면 좋은 글", "자주 묻는 질문", "정리", "참고한 자료")
    out = []
    for tag, text in re.findall(r"<(h[23])[^>]*>(.*?)</\1>", content, re.S):
        t = html_mod.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if t and t not in skip:
            out.append(("  - " if tag == "h3" else "- ") + t)
    return out


def coverage_block(hub: dict, absorbed: list[dict]) -> str:
    """중심 글과 합칠 글들이 다루던 상황 목록. 사실 근거가 아니라 '빠뜨리지 말 범위'입니다."""
    if not absorbed:
        return ""
    lines = ["", "", "## 이번 글이 다룰 범위 (같은 블로그의 기존 글 묶음을 한 편으로 합칩니다)", "",
             "아래는 이 글 하나로 합칠 기존 글들의 제목과 소제목입니다. **사실 근거가 아닙니다.** 독자가 이 주제로 겪는 "
             "상황을 빠뜨리지 않기 위한 목록이니, 위 공식 안내 자료·참고 기사로 확인되는 상황만 다루고 "
             "자료로 확인되지 않는 상황은 빼세요. 여러 글을 이어 붙이지 말고, 독자 상황별로 찾아보기 쉬운 한 편으로 씁니다.", ""]
    for p in [hub, *absorbed]:
        lines.append(f"### {p['title']}")
        lines += outline(p.get("content", ""))[:8]
        lines.append("")
    return "\n".join(lines)


def fix_block(fix: str) -> str:
    """기존 글 점검(2026-10-30)에서 이 글이 지적받은 점. 사실 근거가 아니라 '이번에 꼭 메울 빈칸'입니다."""
    if not fix:
        return ""
    return ("\n\n## 이번 다시 쓰기에서 꼭 고칠 점 (기존 글 점검 결과)\n\n"
            "기존 글이 아래 이유로 '가치가 별로 없는 콘텐츠' 위험 판정을 받았습니다. **사실 근거가 아닙니다.** "
            "지적에 나온 수치·규칙도 위 공식 안내 자료로 확인될 때만 쓰고, 자료로 확인되지 않으면 그 질문을 "
            "제목·범위에서 빼세요. 핵심 질문을 문의처로 넘기는 것으로 메우지 마세요.\n\n- " + fix + "\n")


def _headers() -> dict:
    return {"Authorization": f"Bearer {blogger._access_token()}"}


def fetch_post(blog: fleet_mod.Blog, url: str) -> dict:
    return net.get(f"{blogger.API_BASE}/blogs/{blog.blog_id}/posts/bypath",
                   params={"path": path_of(url), "view": "ADMIN"}, headers=_headers()).json()


def blog_of(fleet: fleet_mod.Fleet, url: str) -> fleet_mod.Blog:
    host = urllib.parse.urlparse(url).netloc.lower()
    for b in fleet.blogs:
        if b.domain.lower() == host:
            return b
    raise SystemExit(f"함대에 없는 블로그 주소입니다: {url}")


def _short(title: str) -> str:
    """'운전면허 적성검사 시력 미달 불합격, 1종·2종 기준' → '운전면허 적성검사 시력 미달 불합격' (검색용)"""
    return re.split(r"[,(]", title)[0].strip()


def research_context(cfg: dict, blog: fleet_mod.Blog, topic: str, absorbed: list[dict],
                     history: list[dict], skip_paths: set[str]) -> tuple[Candidate, str, float]:
    terms = [topic] + [_short(p["title"]) for p in absorbed]
    cand = Candidate(keyword=topic, seen=[Variant(t, "planner", 1) for t in dict.fromkeys(terms[:4])],
                     sources={"planner": 1})
    refs = research.gather(cfg, cand)
    pages, cost = research.official(cfg, cand)
    # 합칠 글마다 공식 자료를 따로 찾습니다. 중심 글 검색어 하나로는 묶음의 상황을 다 덮지 못합니다.
    for p in absorbed:
        sub = Candidate(keyword=_short(p["title"]), seen=[Variant(_short(p["title"]), "planner", 1)],
                        sources={"planner": 1})
        more, c = research.official(cfg, sub)
        cost += c
        known = {x.url for x in pages}
        pages += [x for x in more if x.url not in known]
    pages = pages[:MAX_OFFICIAL_PAGES]
    # 다시 쓰는 글과 합칠 글은 내부 링크 후보에서 뺍니다 (자기 자신·곧 비공개될 글로 링크하지 않게).
    own = [h for h in blog.own_history(history) if path_of(h.get("url", "")) not in skip_paths]
    context = research.to_context(cfg, cand, refs, pages)
    context += research.internal_links_block(own, topic)
    context += research.contacts_block(blog.contacts)
    return cand, context, cost


def preview_path(blog: fleet_mod.Blog, post_id: str) -> Path:
    return PREVIEW_DIR / blog.id / f"{post_id}.json"


def do_preview(cfg: dict, fleet: fleet_mod.Fleet, url: str, absorb: list[str], topic: str = "",
               fix: str = "") -> dict:
    blog = blog_of(fleet, url)
    fleet_mod.apply_env(fleet, blog)
    cfg = fleet_mod.apply_config(cfg, fleet, blog)
    if absorb:
        cfg = {**cfg, "writer": {**cfg["writer"], "target_length": HUB_LENGTH[0], "max_length": HUB_LENGTH[1]}}
    hub = fetch_post(blog, url)
    absorbed = [fetch_post(blog, u) for u in absorb]
    topic = topic or _short(hub["title"])
    history = state.load(blog.history_path)
    # 대기열에서 임시저장될 글(다른 묶음 몫 포함)도 링크 후보에서 뺍니다. 곧 사라질 주소로 링크하면 깨집니다.
    skip = {path_of(u) for u in [url, *absorb]} | {path_of(it["url"]) for it in load_queue() if it["action"] == "revert"}
    cand, context, cost = research_context(cfg, blog, topic, absorbed, history, skip)
    context += coverage_block(hub, absorbed) + fix_block(fix)
    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    article = writer.write(cfg, cand, context, [], date_str, mode="planned", persona=blog.persona)
    cost += article.cost_usd
    rv = reviewer.review(cfg, article, context)
    cost += rv.cost_usd
    article, rv, extra, revisions = _revise_loop(cfg, cand, context, date_str, article, rv, "planned", blog.persona)
    cost += extra
    min_score = cfg["review"].get("min_score", 80)
    out = {
        "url": hub["url"], "post_id": hub["id"], "blog": blog.id, "topic": topic,
        "absorb": [p["url"] for p in absorbed], "fix": fix,
        "old_title": hub["title"], "title": article.title, "description": article.description,
        "labels": hub.get("labels") or [], "card": article.card, "body_html": article.body_html,
        "review": {"verdict": rv.verdict, "score": rv.score, "issues": rv.issues, "card_ok": rv.card_ok,
                   "approved": rv.approved(min_score), "revisions": revisions},
        "cost_usd": round(cost, 3), "made_at": datetime.now(KST).isoformat(timespec="seconds"),
    }
    p = preview_path(blog, hub["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    p.with_suffix(".html").write_text(
        f"<meta charset='utf-8'><h1>{html_mod.escape(article.title)}</h1><p><em>{html_mod.escape(article.description)}</em></p>"
        f"<pre>{html_mod.escape(article.card or '')}</pre>{article.body_html}", encoding="utf-8")
    print(f"미리보기: {p.with_suffix('.html')}")
    print(f"검수: {rv.verdict} {rv.score}점 (고쳐 쓰기 {revisions}회) · 비용 약 ${cost:.2f}")
    for i in rv.issues:
        print(f"  - {i}")
    return out


def _patch(blog: fleet_mod.Blog, post_id: str, body: dict) -> dict:
    # 재시도 없는 세션: 쓰기 요청을 거듭 보내지 않습니다.
    resp = net.once().patch(f"{blogger.API_BASE}/blogs/{blog.blog_id}/posts/{post_id}",
                            headers={**_headers(), "Content-Type": "application/json; charset=UTF-8"},
                            json=body, timeout=30)
    if resp.status_code == 403:
        raise blogger.BloggerForbidden(f"Blogger 수정 거부 (403): {resp.text[:500]}")
    if resp.status_code != 200:
        raise RuntimeError(f"Blogger 수정 실패 ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def _update_history(blog: fleet_mod.Blog, url: str, **fields) -> None:
    history = state.load(blog.history_path)
    key = path_of(url)
    for h in history:
        if path_of(h.get("url", "")) == key:
            h.update(fields)
    state.save(history, blog.history_path)


def do_apply(cfg: dict, fleet: fleet_mod.Fleet, url: str) -> dict:
    blog = blog_of(fleet, url)
    fleet_mod.apply_env(fleet, blog)
    cfg = fleet_mod.apply_config(cfg, fleet, blog)
    post = fetch_post(blog, url)
    p = preview_path(blog, post["id"])
    data = json.loads(p.read_text(encoding="utf-8"))
    if not data["review"]["approved"]:
        raise SystemExit(f"검수를 통과하지 못한 글이라 반영하지 않습니다: {data['review']}")
    body = data["body_html"]
    if data.get("card") and data["review"].get("card_ok", True):
        try:
            body = card_mod.make(cfg, blog.id, blog.name, data["card"], data["topic"], blog.domain) + body
        except Exception as exc:  # noqa: BLE001 — 카드는 덤입니다
            print(f"요약 카드 없이 반영합니다: {exc}")
    _patch(blog, post["id"], {"title": data["title"], "content": body})
    now = datetime.now(KST).isoformat(timespec="seconds")
    _update_history(blog, url, title=data["title"], description=data["description"],
                    review_score=data["review"]["score"], rewritten_at=now)
    data["applied_at"] = now
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"반영: {post['url']} — {data['title']}")
    print(f"  검색 설명(새로 넣을 값): {data['description']}")
    return data


def do_revert(fleet: fleet_mod.Fleet, url: str, into: str) -> None:
    blog = blog_of(fleet, url)
    fleet_mod.apply_env(fleet, blog)
    post = fetch_post(blog, url)
    resp = net.once().post(f"{blogger.API_BASE}/blogs/{blog.blog_id}/posts/{post['id']}/revert",
                           headers=_headers(), timeout=30)
    if resp.status_code == 403:
        raise blogger.BloggerForbidden(f"Blogger 임시저장 거부 (403): {resp.text[:500]}")
    if resp.status_code != 200:
        raise RuntimeError(f"임시저장 실패 ({resp.status_code}): {resp.text[:500]}")
    _update_history(blog, url, status="draft", merged_into=into,
                    reverted_at=datetime.now(KST).isoformat(timespec="seconds"))
    print(f"임시저장: {url} (합친 글: {into})")


# ---------------------------------------------------------------- 대기열 (밤마다 한 걸음)

def load_queue() -> list[dict]:
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def save_queue(q: list[dict]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(q, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def key_of(url: str) -> str:
    """블로그+경로. 경로만 비교하면 다른 블로그의 같은 주소(/2026/10/1.html)와 섞입니다."""
    return host_of(url) + path_of(url)


def next_steps(q: list[dict], night: str) -> list[dict]:
    """오늘 밤 할 일: **블로그마다** 아직 안 한 다시 쓰기 1건 + 중심 글이 반영된 임시저장 1건.
    그 블로그가 오늘 밤 이미 한 종류는 건너뜁니다. (2026-10-29: 함대 전체 1건이면 세 블로그 정리에 한 달이 걸려 블로그별로)"""
    q = [it for it in q if not it.get("hold")]   # hold: 사람이 미리보기를 확인할 글 (손으로 apply 후 done_at 을 적음)
    applied = {key_of(it["url"]) for it in q if it["action"] == "rewrite" and it.get("done_at")}
    out = []
    for host in dict.fromkeys(host_of(it["url"]) for it in q if it["action"] in ("rewrite", "revert")):
        mine = [it for it in q if host_of(it["url"]) == host]
        done_tonight = {it["action"] for it in mine if it.get("night") == night}
        pending = [it for it in mine if not it.get("done_at") and not it.get("failed_at")]
        if "rewrite" not in done_tonight:
            out += [it for it in pending if it["action"] == "rewrite"][:1]
        if "revert" not in done_tonight:
            out += [it for it in pending if it["action"] == "revert" and key_of(it["into"]) in applied][:1]
    return out


def busy_urls(q: list[dict]) -> set[str]:
    """곧 다시 쓰거나 임시저장할 글(key_of). 문장 다듬기·사실 갱신은 이 글을 건너뜁니다 — 고쳐 봐야 곧 새로 쓰이거나
    내려가서 비용과 쓰기 요청만 듭니다(2026-10-31 점검: 다듬은 24편 중 17편이 대기열 글).
    중심 글 다시 쓰기가 실패해 멈춘 임시저장은 사람이 정할 때까지 공개로 남으므로 넣지 않습니다."""
    rewrites = [it for it in q if it["action"] == "rewrite"]
    applied = {key_of(it["url"]) for it in rewrites if it.get("done_at")}
    coming = {key_of(it["url"]) for it in rewrites if not it.get("done_at") and not it.get("failed_at")}
    reverts = {key_of(it["url"]) for it in q if it["action"] == "revert" and not it.get("done_at")
               and not it.get("failed_at") and key_of(it["into"]) in applied | coming}
    return coming | reverts


def note_failure(it: dict) -> None:
    """실패를 out/ 에 적어 둡니다. 워크플로가 이 파일을 보고 이슈를 엽니다(2026-10-31: 검수 보류가 대기열 기록에만
    남아 사람이 다음 보고 때에야 알았습니다)."""
    FAIL_REPORT.parent.mkdir(parents=True, exist_ok=True)
    with FAIL_REPORT.open("a", encoding="utf-8") as f:
        f.write(f"- {it['action']} {it['url']}\n  - {it.get('reason', '')}\n")


def relink(content: str, moved: dict[str, str]) -> tuple[str, int]:
    """임시저장된 글(경로 → 합친 중심 글 주소)로 가는 링크를 중심 글로 바꿉니다. (새 본문, 바꾼 수)"""
    count = 0

    def sub(m: re.Match) -> str:
        nonlocal count
        target = moved.get(path_of(m.group(2)))
        if not target:
            return m.group(0)
        count += 1
        return f'{m.group(1)}{target}"'

    return re.sub(r'(href=")(https?://[^"]+)"', sub, content), count


def relink_one(fleet: fleet_mod.Fleet, q: list[dict], host: str) -> str:
    """그 블로그에서 임시저장된 글로 링크하는 공개 글 하나를 찾아 링크를 고칩니다(블로그마다 하룻밤 한 편).
    고친 글 주소, 없으면 ''."""
    done = [it for it in q if it["action"] == "revert" and it.get("done_at") and host_of(it["url"]) == host]
    if not done:
        return ""
    moved = {path_of(it["url"]): it["into"].split("?")[0] for it in done}
    blog = blog_of(fleet, done[0]["url"])
    fleet_mod.apply_env(fleet, blog)
    page = None
    while True:
        params = {"status": "live", "maxResults": 100, "fetchBodies": "true", "view": "ADMIN"}
        if page:
            params["pageToken"] = page
        data = net.get(f"{blogger.API_BASE}/blogs/{blog.blog_id}/posts", params=params, headers=_headers()).json()
        for post in data.get("items", []):
            if path_of(post["url"]) in moved:
                continue
            new, n = relink(post.get("content", ""), moved)
            if n:
                _patch(blog, post["id"], {"content": new})
                print(f"링크 고침: {post['url']} ({n}곳 → 중심 글)")
                return post["url"]
        page = data.get("nextPageToken")
        if not page:
            return ""


def relink_quota(q: list[dict], night: str) -> dict[str, int]:
    """{블로그: 오늘 밤 더 고칠 수 있는 링크 편수}. 임시저장이 있었던 블로그만."""
    hosts = dict.fromkeys(host_of(it["url"]) for it in q if it["action"] == "revert")
    used = Counter(host_of(r["url"]) for r in q if r["action"] == "relink" and r.get("night") == night)
    return {h: RELINK_PER_NIGHT - used[h] for h in hosts if used[h] < RELINK_PER_NIGHT}


def run_queue(cfg: dict, fleet: fleet_mod.Fleet, dry_run: bool) -> int:
    now = datetime.now(KST)
    night = (now.date() if now.hour >= 12 else now.date() - timedelta(days=1)).isoformat()   # 밤 = 시작한 날
    q = load_queue()
    steps = next_steps(q, night)
    quota = relink_quota(q, night)
    if not steps and not quota:
        print("다시 쓰기 대기열: 오늘 할 일 없음")
        return 0
    if not dry_run and not any(now.hour in r for r in QUIET_HOURS):
        print(f"건너뜀 — 지금은 {now:%H:%M} KST. 글 작성 시간대와 겹치지 않게 22~06시에만 고칩니다.")
        return 0
    wrote = False
    for it in steps:
        print(f"{'(dry-run) ' if dry_run else ''}{it['action']}: {it['url']}")
        if dry_run:
            continue
        if wrote:
            time.sleep(120)   # 쓰기 요청 사이 간격
        it["night"] = night
        try:
            if it["action"] == "rewrite":
                data = do_preview(cfg, fleet, it["url"], it.get("absorb") or [], it.get("topic", ""), it.get("fix", ""))
                if not data["review"]["approved"]:
                    it.update(failed_at=now.isoformat(timespec="seconds"),
                              reason=f"검수 {data['review']['verdict']} {data['review']['score']}점: "
                                     + "; ".join(data["review"]["issues"][:3]))
                    note_failure(it)
                    save_queue(q)
                    continue
                do_apply(cfg, fleet, it["url"])
                it["new_description"] = data["description"]
            else:
                do_revert(fleet, it["url"], it["into"])
            it["done_at"] = datetime.now(KST).isoformat(timespec="seconds")
            wrote = True
        except blogger.BloggerForbidden as exc:
            it.update(failed_at=now.isoformat(timespec="seconds"), reason=str(exc)[:300])
            note_failure(it)
            save_queue(q)
            raise
        except Exception as exc:  # noqa: BLE001 — 한 건 실패로 기록을 남기고 이슈로 알립니다
            it.update(failed_at=now.isoformat(timespec="seconds"), reason=str(exc)[:300])
            note_failure(it)
        save_queue(q)
    # 임시저장된 글로 가는 다른 공개 글의 링크를 블로그마다 하룻밤 RELINK_PER_NIGHT 편까지 중심 글로 바꿉니다 (깨진 내부 링크 방지).
    for host, left in ({} if dry_run else quota).items():
        for _ in range(left):
            if wrote:
                time.sleep(120)
            try:
                fixed = relink_one(fleet, q, host)
            except blogger.BloggerForbidden:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"링크 고치기 실패 ({host}): {exc}")
                fixed = ""
            if not fixed:
                break
            wrote = True
            q.append({"action": "relink", "url": fixed, "night": night,
                      "done_at": datetime.now(KST).isoformat(timespec="seconds")})
            save_queue(q)
    save_queue(q)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview")
    p.add_argument("url")
    p.add_argument("--absorb", nargs="*", default=[])
    p.add_argument("--topic", default="")
    p.add_argument("--fix", default="", help="기존 글 점검에서 지적받은 점 (이번에 메울 빈칸)")
    sub.add_parser("apply").add_argument("url")
    p = sub.add_parser("revert")
    p.add_argument("url")
    p.add_argument("--into", required=True)
    sub.add_parser("run-queue").add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    cfg = load_config()
    fleet = fleet_mod.load_fleet()
    if args.cmd == "preview":
        do_preview(cfg, fleet, args.url, args.absorb, args.topic, args.fix)
    elif args.cmd == "apply":
        do_apply(cfg, fleet, args.url)
    elif args.cmd == "revert":
        do_revert(fleet, args.url, args.into)
    else:
        return run_queue(cfg, fleet, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
