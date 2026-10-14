"""오래된 사실 갱신 (하루 한 편) — 공개된 글의 금액·기한·조건을 공식 자료로 다시 확인해 바뀐 값만 고칩니다.

연말정산·지원금·연금·청약처럼 해마다 값이 바뀌는 주제가 많습니다. 틀린 숫자가 남아 있으면 독자 신뢰와
애드센스 심사 모두에 불리합니다(2026-10-14 개선점 3). 기존 글 정리(retrofit_posts.py)는 형식만 고치고
내용은 손대지 않았는데, 이 스크립트가 **내용**을 맡습니다.

순서
 1) 고칠 글 고르기 — (a) 글감 기획이 "기존 글과 겹친다"고 넘긴 보강 후보(data/blogs/<id>/reinforce.json)
    → (b) Search Console 클릭 많은 글 → (c) 오래된 글 순. 최근 recheck_days 안에 본 글은 건너뜁니다.
 2) 그 글의 글감으로 공식 안내 자료를 다시 조사합니다 (src/research.official — 새 글 쓸 때와 같은 조사).
    자료를 못 찾으면 글을 고치지 않고 '확인 필요'로만 기록합니다 (임의 수정 금지).
 3) 확인 모델(Sonnet)이 본문의 값과 자료를 대조해 **바뀐 곳만** `from → to` 로 냅니다. 글 전체를 다시 쓰지 않습니다.
 4) 코드가 from 이 본문에 딱 한 번 있는지, 새로 들어온 숫자가 to 안의 숫자뿐인지 확인하고 치환합니다.
    (글의 구조·링크·다른 문장은 바뀔 수 없습니다.)
 5) 검수 모델이 각 변경이 자료와 맞는지 한 번 더 봅니다. 통과하면 Blogger 에 올리고 끝 문구의 기준 날짜를 오늘로 바꿉니다.
하루 한 건, 밤 22~06시(retrofit_posts 와 같은 규칙), 같은 계정이 1시간 안에 글을 올렸으면 건너뜁니다.
글당 약 $0.2 (공식 자료 조사 $0.1 + 대조·확인 $0.05~0.1).

    python scripts/refresh_facts.py                 # 오늘 몫 한 편
    python scripts/refresh_facts.py --dry-run       # 순서대로 대상만 보기
    python scripts/refresh_facts.py --url <글 주소> # 특정 글 (시간대·간격 무시)
    python scripts/refresh_facts.py --preview       # 고칠 값을 찾되 올리지 않음 (out/refresh-preview/)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import retrofit_posts as rp  # noqa: E402
from src import fleet as fleet_mod  # noqa: E402
from src import gsc, llm, research, reviewer, state  # noqa: E402
from src.config import load_config, load_dotenv  # noqa: E402
from src.publishers import blogger  # noqa: E402
from src.state import KST  # noqa: E402
from src.trends import Candidate, Variant  # noqa: E402

STATE_PATH = fleet_mod.FLEET_DATA / "refresh.json"
ROOT = Path(__file__).resolve().parent.parent

SYSTEM = """당신은 한국어 생활정보 블로그의 사실 확인 담당자입니다. 이미 공개된 글과, 지금 다시 조사한 정부·공공기관의 \
공식 안내 자료를 받습니다. 글에 적힌 **금액·기한·비율·나이·연도·횟수·신청처·필요 서류** 가운데 자료와 다른 곳만 찾습니다.

규칙
- 자료에 그 값이 **분명히** 적혀 있을 때만 "다르다"고 합니다. 자료에 없는 값은 바꾸지 않고 notes 에 "확인 불가"로 적습니다.
- 바꿀 때는 글의 문장 일부를 **글자 그대로**(태그 제외, 10~80자) from 에 옮기고, 숫자·표현만 바꾼 결과를 to 에 씁니다. \
문장 전체를 다시 쓰거나 새 문장·이유·조언을 보태지 않습니다. 링크 주소는 건드리지 않습니다.
- from 은 본문에서 한 번만 나오는 조각이어야 합니다. 같은 값이 여러 곳에 있으면 곳마다 따로 적습니다(표의 칸 포함).
- 기준 연도가 글에 "2025년" 처럼 적혀 있고 자료가 2026년 기준이면 연도와 값을 함께 고칩니다.
- 자료가 글보다 오래된 것 같으면(자료의 기준일이 글의 작성일보다 앞) 바꾸지 말고 notes 에 적습니다.
- 바뀐 곳이 없으면 verdict 를 "same", 자료가 너무 적어 판단할 수 없으면 "unsure" 로 합니다.

JSON 한 덩어리만 출력합니다:
{"verdict": "same" | "changed" | "unsure",
 "changes": [{"from": "본문 조각 그대로", "to": "고친 조각", "source": "근거 자료 주소", "why": "자료의 어떤 문장 때문인지 한 줄"}],
 "notes": ["확인 못 한 값이나 주의할 점"]}"""

USER = """## 글 (제목: {title}, 공개일: {published})

{body}

{official}

위 자료와 대조해 글에서 바뀐 값만 찾아 JSON 으로 내세요."""

_JSON = re.compile(r"\{.*\}", re.DOTALL)
_FOOTER_DATE = re.compile(r"\d{4}년 \d{1,2}월 \d{1,2}일(?= 기준으로 정리한 내용입니다)")


# ---------------------------------------------------------------- 상태

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_date": "", "checked": {}, "done": [], "needs_review": []}


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _reinforce(blog: fleet_mod.Blog) -> list[dict]:
    try:
        return json.loads((blog.dir / "reinforce.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------- 대상 고르기

def order(
    posts: list[tuple[fleet_mod.Blog, dict]], reinforce_urls: list[str], clicks: dict[str, int],
    checked: dict, now: datetime, *, min_age_days: int, recheck_days: int,
) -> list[tuple[fleet_mod.Blog, dict, str]]:
    """(블로그, 글, 이유) 를 볼 순서대로. 보강 후보 → 클릭 많은 글 → 오래된 글."""
    out = []
    for b, p in posts:
        url = p["url"]
        published = datetime.fromisoformat(p["published"]).astimezone(KST)
        last = (checked.get(url) or {}).get("at", "")
        if last:
            seen = datetime.fromisoformat(last)
            seen = seen if seen.tzinfo else seen.replace(tzinfo=KST)
            if (now - seen).days < recheck_days:
                continue
        if url in reinforce_urls:
            key, why = (0, reinforce_urls.index(url)), "기획이 넘긴 보강 후보"
        elif (now - published).days < min_age_days:
            continue
        elif clicks.get(url, 0) > 0:
            key, why = (1, -clicks[url]), f"검색 클릭 {clicks[url]}회"
        else:
            key, why = (2, published.timestamp()), f"{published:%Y-%m-%d} 공개"
        out.append((key, b, p, why))
    out.sort(key=lambda x: x[0])
    return [(b, p, why) for _, b, p, why in out]


def _clicks_by_url(blog: fleet_mod.Blog, days: int) -> dict[str, int]:
    try:
        token = blogger._access_token()
        pages = gsc.top_pages(token, blog.home_url, days)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, int] = {}
    for page, c, _ in pages:
        if c:
            out[page.split("?")[0]] = c
    return out


# ---------------------------------------------------------------- 대조·치환

def parse_verdict(raw: str) -> dict:
    m = _JSON.search(raw)
    if not m:
        return {"verdict": "unsure", "changes": [], "notes": ["응답에 JSON 이 없음"]}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"verdict": "unsure", "changes": [], "notes": ["JSON 파싱 실패"]}
    changes = []
    for c in data.get("changes") or []:
        if isinstance(c, dict) and str(c.get("from", "")).strip() and str(c.get("to", "")).strip():
            changes.append({k: str(c.get(k, "")).strip() for k in ("from", "to", "source", "why")})
    notes = [str(n) for n in (data.get("notes") or []) if str(n).strip()]
    verdict = str(data.get("verdict", "unsure"))
    if verdict not in ("same", "changed", "unsure"):
        verdict = "unsure"
    return {"verdict": verdict, "changes": changes, "notes": notes}


def _nums(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,.]*\d|\d", re.sub(r"<[^>]+>", " ", text)))


def apply_changes(body: str, changes: list[dict]) -> tuple[str, list[dict], list[str]]:
    """from→to 를 본문에 적용합니다. (새 본문, 적용된 변경, 적용 못 한 이유).

    from 은 본문에 정확히 한 번 있어야 하고 to 는 from 과 달라야 합니다. 태그가 든 조각이나 링크 주소를 바꾸는
    변경은 거부합니다. 본문에 새로 생긴 숫자는 to 에 적힌 숫자뿐이어야 합니다(모델이 엉뚱한 곳을 건드리지 못하게).
    """
    new = body
    applied, problems = [], []
    for c in changes:
        src, dst = c["from"], c["to"]
        if src == dst:
            problems.append(f"바뀐 것 없음: {src[:40]}")
            continue
        if "<" in src or "<" in dst or "href" in src or "http" in dst:
            problems.append(f"태그·링크가 든 변경은 거부: {src[:40]}")
            continue
        n = new.count(src)
        if n != 1:
            problems.append(f"본문에 {n}번 나옴 (1번이어야 함): {src[:40]}")
            continue
        new = new.replace(src, dst, 1)
        applied.append(c)
    if applied:
        added = _nums(new) - _nums(body)
        allowed = set().union(*(_nums(c["to"]) for c in applied))
        stray = added - allowed
        if stray:
            problems.append(f"to 에 없는 숫자가 본문에 생김: {sorted(stray)[:6]}")
            return body, [], problems
        if rp.split_card(body)[0] != rp.split_card(new)[0]:
            return body, [], problems + ["요약 카드 블록이 바뀜"]
    return new, applied, problems


def bump_footer_date(body: str, now: datetime) -> str:
    return _FOOTER_DATE.sub(f"{now.year}년 {now.month:02d}월 {now.day:02d}일", body, count=1)


def compare(cfg: dict, title: str, published: str, body: str, official_block: str, client=None) -> tuple[dict, float]:
    rc = cfg.get("refresh") or {}
    model = rc.get("model", "claude-sonnet-5-5")
    client = client or llm.client()
    response = client.messages.create(
        model=model,
        max_tokens=int(rc.get("max_tokens", 6000)),
        system=llm.cached(SYSTEM),
        output_config={"effort": rc.get("effort", "medium")},
        messages=[{"role": "user", "content": USER.format(title=title, published=published[:10], body=body,
                                                           official=official_block)}],
    )
    cost = llm.cost_of(response.usage, model, (2.00, 10.00))
    if response.stop_reason == "refusal":
        return {"verdict": "unsure", "changes": [], "notes": ["모델이 대조를 거부함"]}, cost
    return parse_verdict(llm.text_of(response, f"[{title[:20]}] 사실 대조")), cost


def _keyword_for(url: str, history: list[dict], title: str) -> tuple[str, list[str]]:
    for h in history:
        if h.get("url") == url and h.get("keyword"):
            return h["keyword"], [h.get("title", "")]
    return title, []


def _note(st: dict, blog: fleet_mod.Blog, url: str, now: datetime, result: str, review: str = "",
          notes: list[str] | None = None) -> None:
    st.setdefault("checked", {})[url] = {"at": now.isoformat(timespec="minutes"), "blog": blog.id, "result": result,
                                         **({"notes": [n[:200] for n in notes[:3]]} if notes else {})}
    if review:
        st.setdefault("needs_review", []).append({"url": url, "blog": blog.id, "at": now.strftime("%Y-%m-%d"),
                                                  "note": review[:300]})
        st["needs_review"] = st["needs_review"][-50:]


# ---------------------------------------------------------------- 실행

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="고치지 않고 볼 순서만 출력")
    ap.add_argument("--preview", action="store_true", help="대조까지 하되 올리지 않음 (out/refresh-preview/)")
    ap.add_argument("--url", help="이 글만 (시간대·발행 간격 무시)")
    ap.add_argument("--blog", help="이 블로그만")
    args = ap.parse_args(argv)

    load_dotenv()
    cfg = load_config()
    rc = cfg.get("refresh") or {}
    if not rc.get("enabled", True) and not args.url:
        rp.announce("⏭️ 사실 갱신이 꺼져 있습니다 (config refresh.enabled)")
        return 0
    now = datetime.now(KST)
    today = rp.night_of(now)
    st = load_state()
    manual = bool(args.url)
    if not (args.dry_run or args.preview or manual):
        if st.get("last_date") == today:
            rp.announce(f"⏭️ 건너뜀 — 오늘({today}) 몫은 이미 확인했습니다: {st.get('last_result', '')}")
            return 0
        if now.hour not in rp.QUIET_HOURS:
            rp.announce(f"⏭️ 건너뜀 — 지금은 {now:%H:%M} KST. 글 작성 시간대와 겹치지 않게 22~06시에만 고칩니다.")
            return 0

    fleet = fleet_mod.load_fleet()
    acc_state = fleet_mod.load_account_state()
    posts: list[tuple[fleet_mod.Blog, dict]] = []
    reinforce_urls: list[str] = []
    clicks: dict[str, int] = {}
    for b in fleet.blogs:
        if not b.enabled or fleet_mod.account_halted(b.account, acc_state) or (args.blog and b.id != args.blog):
            continue
        fleet_mod.apply_env(fleet, b)
        for p in rp._live_posts(b.blog_id):
            if args.url and p["url"] != args.url:
                continue
            posts.append((b, p))
        reinforce_urls += [r["url"] for r in _reinforce(b) if r.get("url")]
        if not args.url:
            clicks.update(_clicks_by_url(b, int(rc.get("gsc_days", 28))))

    todo = order(posts, reinforce_urls, clicks, {} if manual else st.get("checked", {}), now,
                 min_age_days=0 if manual else int(rc.get("min_age_days", 14)),
                 recheck_days=int(rc.get("recheck_days", 30)))
    print(f"확인할 글 {len(todo)}편")
    for b, p, why in todo[:20]:
        print(f"  {b.id:13} {p['url']}  — {why}")
    if args.dry_run:
        rp.announce(f"🔍 미리보기 — 확인할 글 {len(todo)}편" + (f", 다음 차례 {todo[0][1]['url']}" if todo else ""))
        return 0
    if not todo:
        st.update(last_result="확인할 글 없음", checked_at=now.isoformat(timespec="minutes"))
        save_state(st)
        rp.announce("✅ 확인할 글이 없습니다 (모두 최근에 확인했거나 아직 어립니다).")
        return 0

    if not manual:
        # 같은 계정이 방금 글을 올렸으면(예약 지연으로 밤늦게 밀린 슬롯) 그 계정 글은 이번에 건너뜁니다.
        recent = {}
        for acc in {b.account for b, _, _ in todo}:
            times = [t for t in (fleet_mod.last_live_at(x) for x in fleet.blogs_of(acc, enabled_only=False)) if t]
            recent[acc] = bool(times) and now - max(times) < rp.ACCOUNT_GAP
        todo = [t for t in todo if not recent[t[0].account]] or ([] if not args.preview else todo)
        if not todo:
            rp.announce("⏭️ 건너뜀 — 확인할 글의 계정이 최근 1시간 안에 글을 올렸습니다. 다음 실행에서 확인합니다.")
            return 0

    blog, post, why = todo[0]
    url, title = post["url"], post.get("title", "")
    history = state.load(blog.history_path)
    keyword, variants = _keyword_for(url, history, title)
    print(f"\n오늘 확인할 글: {blog.id} {url} ({why}) — 글감 「{keyword}」")

    fleet_mod.apply_env(fleet, blog)
    bcfg = fleet_mod.apply_config(cfg, fleet, blog)
    cand = Candidate(keyword=keyword, sources={"planner": 1},
                     seen=[Variant(v, "planner", 1) for v in [keyword, *variants] if v])
    pages, cost = research.official(bcfg, cand)
    cost_total = cost
    if not pages:
        _note(st, blog, url, now, "공식 자료 없음 — 확인 필요", review="공식 안내 자료를 찾지 못해 사람이 확인해야 합니다")
        st.update(last_date=today, last_result=f"{today} 자료 없음 — {blog.id} {url}")
        save_state(st)
        rp.announce(f"⚠️ 공식 자료를 찾지 못해 고치지 않음 — {blog.id} {url} (확인 필요 목록에 기록)", [f"비용 ${cost:.3f}"])
        return 0

    official_block = research.official_block(pages)
    card, body = rp.split_card(post.get("content", ""))
    verdict, c2 = compare(bcfg, title, post.get("published", ""), body, official_block)
    cost_total += c2
    print(f"대조 결과: {verdict['verdict']} · 변경 {len(verdict['changes'])}건 · ${c2:.3f}")
    for c in verdict["changes"]:
        print(f"  - {c['from'][:60]!r} → {c['to'][:60]!r}  ({c['source']})")
    for n in verdict["notes"]:
        print(f"  · {n}")

    new_body, applied, problems = apply_changes(body, verdict["changes"])
    for p_ in problems:
        print(f"  ✗ {p_}")
    if args.preview:
        out = ROOT / "out" / "refresh-preview"
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{today}-{blog.id}-{post['id']}"
        (out / f"{stem}.json").write_text(json.dumps({"url": url, "title": title, "verdict": verdict, "applied": applied,
                                                      "problems": problems, "official": [p.url for p in pages],
                                                      "cost": round(cost_total, 4)}, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
        (out / f"{stem}.after.html").write_text(card + new_body, encoding="utf-8")
        rp.announce(f"🔍 사실 갱신 미리보기 — {blog.id} {url} → out/refresh-preview/{stem}.*")
        return 0

    if verdict["verdict"] == "unsure" or (verdict["verdict"] == "changed" and not applied):
        note = "; ".join(verdict["notes"][:3] + problems[:3])
        _note(st, blog, url, now, f"판단 보류 — {note[:120]}", review=note or "대조 모델이 판단하지 못함")
        st.update(last_date=today, last_result=f"{today} 보류 — {blog.id} {url}")
        save_state(st)
        rp.announce(f"⚠️ 고치지 않음(확인 필요) — {blog.id} {url}: {note[:160]}", [f"비용 ${cost_total:.3f}"])
        return 0
    if not applied:
        _note(st, blog, url, now, "자료와 같음", notes=verdict["notes"])
        st.update(last_date=today, last_result=f"{today} 변경 없음 — {blog.id} {url}")
        save_state(st)
        rp.announce(f"✅ 자료와 같음 — {blog.id} {url} (고칠 값 없음)", [f"비용 ${cost_total:.3f}"] + verdict["notes"][:3])
        return 0

    ok, issues, c3 = reviewer.fact_changes_ok(bcfg, applied, official_block)
    cost_total += c3
    if not ok:
        note = "; ".join(issues[:3])
        _note(st, blog, url, now, f"검수 불통 — {note[:120]}", review=f"고칠 값을 찾았지만 검수에서 막힘: {note}")
        st.update(last_date=today, last_result=f"{today} 검수 불통 — {blog.id} {url}")
        save_state(st)
        rp.announce(f"⚠️ 검수에서 막힘 — {blog.id} {url}: {note[:160]}", [f"비용 ${cost_total:.3f}"])
        return 0

    new_body = bump_footer_date(new_body, now)
    try:
        rp._patch(blog.blog_id, post["id"], card + new_body)
    except blogger.BloggerForbidden as exc:
        fleet_mod.halt_account(blog.account, f"{blog.id} 사실 갱신 403: {exc}")
        st.update(last_date=today, last_result=f"{today} ⛔ 수정 거부(403) — 계정 '{blog.account}' 비상정지")
        save_state(st)
        rp.announce(f"⛔ 계정 '{blog.account}' 비상정지 — {exc}")
        return 3
    _note(st, blog, url, now, f"고침 {len(applied)}곳", notes=verdict["notes"])
    st.setdefault("done", []).append({"at": now.isoformat(timespec="seconds"), "blog": blog.id, "url": url,
                                      "changes": [f"{c['from'][:40]} → {c['to'][:40]}" for c in applied],
                                      "sources": sorted({c["source"] for c in applied if c["source"]}),
                                      "cost_usd": round(cost_total, 4)})
    st["done"] = st["done"][-200:]
    st.update(last_date=today, last_result=f"{today} 고침 {len(applied)}곳 — {blog.id} {url}")
    save_state(st)
    rp.announce(f"✅ 사실 갱신 — {blog.id} {url} ({len(applied)}곳)",
                [f"{c['from'][:50]} → {c['to'][:50]}" for c in applied] + [f"비용 ${cost_total:.3f}"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
