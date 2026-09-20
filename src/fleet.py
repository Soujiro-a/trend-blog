"""블로그 함대(fleet): 여러 Blogger 블로그를 같은 파이프라인으로, 서로 다른 시간에 운영합니다.

한 블로그를 '실행 컨텍스트'로 바꾸는 일이 이 모듈의 전부입니다.

* fleet/blogs.yaml 을 읽고 관리 에이전트의 상태(data/fleet/manager_state.json)를 덧씌움
* 블로그별 데이터 폴더 (data/blogs/<id>/history.json, last_run.md, runs.json)
* 블로그별 환경변수 (BLOGGER_BLOG_ID, 계정별 토큰)
* 블로그별 config 덮어쓰기 (fleet.defaults → blog.overrides)
* 슬롯 계산: 오늘 아직 안 돌았고 슬롯 시각이 지난 블로그 = 실행 대상
* 키워드 선점(claims): 같은 날 다른 블로그가 쓴 주제를 건너뛰어 100개 블로그가 같은 글을 찍어내지 않게 함
"""

from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .config import DATA_DIR, ROOT
from .state import KST
from .trends import Candidate
from .trends.base import similarity

log = logging.getLogger(__name__)

FLEET_PATH = ROOT / "fleet" / "blogs.yaml"
FLEET_DATA = DATA_DIR / "fleet"
MANAGER_STATE_PATH = FLEET_DATA / "manager_state.json"
CLAIMS_PATH = FLEET_DATA / "claims.json"


@dataclass
class Blog:
    id: str
    name: str
    blog_id: str
    slot: str = "04:30"
    account: str = "default"
    enabled: bool = True
    niche: str = ""
    persona: str = ""
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=lambda: ["실시간이슈"])
    overrides: dict = field(default_factory=dict)
    manager_note: str = ""   # 관리 에이전트가 남긴 이유 (표시용)

    @property
    def dir(self) -> Path:
        return DATA_DIR / "blogs" / self.id

    @property
    def history_path(self) -> Path:
        return self.dir / "history.json"

    @property
    def report_path(self) -> Path:
        return self.dir / "last_run.md"

    @property
    def runs_path(self) -> Path:
        return self.dir / "runs.json"

    @property
    def slot_minutes(self) -> int:
        h, m = self.slot.split(":")
        return int(h) * 60 + int(m)


@dataclass
class Fleet:
    settings: dict
    accounts: dict
    blogs: list[Blog]

    def get(self, blog_id: str) -> Blog:
        for b in self.blogs:
            if b.id == blog_id:
                return b
        raise KeyError(f"함대에 없는 블로그: {blog_id} (있는 것: {', '.join(b.id for b in self.blogs)})")

    @property
    def step(self) -> int:
        return int(self.settings.get("slot_step_minutes", 10))


# ---------------------------------------------------------------- 로딩

def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_manager_state(path: Path = MANAGER_STATE_PATH) -> dict:
    if not path.exists():
        return {"blogs": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("관리 상태 파일을 읽지 못해 무시합니다: %s", exc)
        return {"blogs": {}}


def save_manager_state(state: dict, path: Path = MANAGER_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_fleet(path: Path = FLEET_PATH, manager_state: dict | None = None) -> Fleet:
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    settings = raw.get("fleet", {}) or {}
    accounts = raw.get("accounts", {}) or {"default": {}}
    manager_state = manager_state if manager_state is not None else load_manager_state()
    mstate = manager_state.get("blogs", {})

    blogs: list[Blog] = []
    for entry in raw.get("blogs", []) or []:
        b = Blog(
            id=str(entry["id"]),
            name=str(entry.get("name", entry["id"])),
            blog_id=str(entry["blog_id"]),
            slot=str(entry.get("slot", settings.get("slot_start", "04:30"))),
            account=str(entry.get("account", "default")),
            enabled=bool(entry.get("enabled", True)),
            niche=str(entry.get("niche") or ""),
            persona=str(entry.get("persona") or ""),
            include_patterns=list(entry.get("include_patterns") or []),
            exclude_patterns=list(entry.get("exclude_patterns") or []),
            labels=list(entry.get("labels") or ["실시간이슈"]),
            overrides=dict(entry.get("overrides") or {}),
        )
        # 관리 에이전트의 결정 덧씌우기 (파일 주석을 지키기 위해 blogs.yaml 은 건드리지 않음)
        ms = mstate.get(b.id, {})
        if "enabled" in ms:
            b.enabled = bool(ms["enabled"])
        if ms.get("overrides"):
            b.overrides = _deep_merge(b.overrides, ms["overrides"])
        b.manager_note = str(ms.get("note", ""))
        blogs.append(b)

    validate(Fleet(settings, accounts, blogs))
    return Fleet(settings, accounts, blogs)


def validate(fleet: Fleet) -> None:
    ids = [b.id for b in fleet.blogs]
    if len(ids) != len(set(ids)):
        raise ValueError("blogs.yaml 에 id 가 중복됩니다")
    blog_ids = [b.blog_id for b in fleet.blogs]
    if len(blog_ids) != len(set(blog_ids)):
        raise ValueError("blogs.yaml 에 blog_id 가 중복됩니다")
    for b in fleet.blogs:
        if b.account not in fleet.accounts:
            raise ValueError(f"{b.id}: 계정 '{b.account}' 이(가) accounts 에 없습니다")
        h, m = b.slot.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"{b.id}: 슬롯 형식 오류 '{b.slot}'")
    slots = sorted(b.slot_minutes for b in fleet.blogs if b.enabled)
    for a, c in zip(slots, slots[1:]):
        if c - a < fleet.step:
            raise ValueError(f"슬롯 간격이 {fleet.step}분보다 좁습니다: {a // 60:02d}:{a % 60:02d} 와 {c // 60:02d}:{c % 60:02d}")


# ---------------------------------------------------------------- 슬롯

def next_free_slot(fleet: Fleet) -> str:
    """기존 슬롯과 step 이상 떨어진 가장 이른 빈 슬롯."""
    start_h, start_m = str(fleet.settings.get("slot_start", "04:30")).split(":")
    t = int(start_h) * 60 + int(start_m)
    taken = sorted(b.slot_minutes for b in fleet.blogs)
    while any(abs(t - s) < fleet.step for s in taken):
        t += fleet.step
        if t >= 24 * 60:
            raise ValueError("하루에 넣을 수 있는 슬롯이 다 찼습니다 (slot_step_minutes 를 줄이세요)")
    return f"{t // 60:02d}:{t % 60:02d}"


def load_runs(blog: Blog) -> dict:
    if not blog.runs_path.exists():
        return {}
    try:
        return json.loads(blog.runs_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def mark_ran(blog: Blog, mode: str, ok: bool, summary: str, now: datetime | None = None) -> None:
    now = now or datetime.now(KST)
    runs = load_runs(blog)
    key = f"{mode}:{now.strftime('%Y-%m-%d')}"
    runs[key] = {"at": now.isoformat(timespec="seconds"), "ok": ok, "summary": summary[:300]}
    # 최근 60일만 유지
    keep = sorted(runs.keys())[-120:]
    runs = {k: runs[k] for k in keep}
    blog.runs_path.parent.mkdir(parents=True, exist_ok=True)
    blog.runs_path.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")


def ran_today(blog: Blog, mode: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(KST)
    return f"{mode}:{now.strftime('%Y-%m-%d')}" in load_runs(blog)


def due_blogs(fleet: Fleet, now: datetime | None = None, mode: str = "trend") -> list[Blog]:
    """지금 실행해야 할 블로그. 슬롯 순서대로.

    catch_up 이 켜져 있으면 '슬롯이 지났고 오늘 아직 안 돈' 블로그 전부, 꺼져 있으면
    슬롯이 현재 10분 창 안에 있는 블로그만.
    """
    now = now or datetime.now(KST)
    minute_now = now.hour * 60 + now.minute
    catch_up = bool(fleet.settings.get("catch_up", True))
    due = []
    for b in sorted(fleet.blogs, key=lambda b: b.slot_minutes):
        if not b.enabled or ran_today(b, mode, now):
            continue
        if catch_up:
            if b.slot_minutes <= minute_now:
                due.append(b)
        elif b.slot_minutes <= minute_now < b.slot_minutes + fleet.step:
            due.append(b)
    return due


# ---------------------------------------------------------------- 실행 컨텍스트

def env_name(base: str, account: str) -> str:
    return base if account == "default" else f"{base}_{account.upper()}"


def apply_env(fleet: Fleet, blog: Blog) -> None:
    """이 블로그용 환경변수를 설정합니다. 계정별 토큰이 있으면 기본 변수명으로 복사해 publishers 가 그대로 쓰게 합니다."""
    os.environ["BLOGGER_BLOG_ID"] = blog.blog_id
    os.environ["FLEET_BLOG"] = blog.id
    for base in ("BLOGGER_REFRESH_TOKEN", "BLOGGER_CLIENT_ID", "BLOGGER_CLIENT_SECRET"):
        specific = env_name(base, blog.account)
        if specific != base and os.environ.get(specific):
            os.environ[base] = os.environ[specific]


def apply_config(cfg: dict, fleet: Fleet, blog: Blog) -> dict:
    merged = _deep_merge(cfg, fleet.settings.get("defaults", {}) or {})
    merged = _deep_merge(merged, blog.overrides)
    merged.setdefault("publish", {})["default_labels"] = list(blog.labels)
    # 블로그별 추가 차단 키워드
    if blog.exclude_patterns:
        f = merged.setdefault("filters", {})
        f["block_keywords"] = list(f.get("block_keywords", [])) + list(blog.exclude_patterns)
    return merged


def filter_niche(blog: Blog, candidates: list[Candidate]) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """include_patterns 가 있으면 그중 하나를 포함한 키워드만 남깁니다."""
    if not blog.include_patterns:
        return candidates, []
    kept, dropped = [], []
    for c in candidates:
        hay = " ".join([c.keyword, *c.variants, *c.headline_hits])
        if any(p in hay for p in blog.include_patterns):
            kept.append(c)
        else:
            dropped.append((c.keyword, "블로그 주제 밖"))
    return kept, dropped


# ---------------------------------------------------------------- 키워드 선점 (블로그 간 중복 방지)

def load_claims(path: Path = CLAIMS_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_claims(claims: dict, path: Path = CLAIMS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(claims, ensure_ascii=False, indent=2), encoding="utf-8")


def _recent_claims(claims: dict, days: int, now: datetime) -> list[dict]:
    out = []
    for date_str, entries in claims.items():
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (now.date() - d).days < days:
            out.extend(entries)
    return out


def filter_claimed(
    fleet: Fleet, cfg: dict, blog: Blog, candidates: list[Candidate], claims: dict, now: datetime | None = None
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """다른 블로그가 최근에 선점한 키워드를 뺍니다. share_topics 가 true 면 아무것도 안 뺍니다."""
    if fleet.settings.get("share_topics", False):
        return candidates, []
    now = now or datetime.now(KST)
    # 블로그 간 선점은 같은 블로그의 14일 중복 판정보다 느슨하게 잡습니다. 실시간 검색어는 같은 사건이
    # '최두호' / '최두호 핏불 1R TKO패' / '최두호 UFC 첫 피니시' 처럼 길이가 다른 표현으로 동시에 올라오는데,
    # 기본 유사도(0.6)로는 서로 다른 주제로 보여 세 블로그가 같은 사건을 같은 날 쓰게 됩니다(2026-09-20 관측).
    threshold = float(fleet.settings.get("claim_similarity", 0.4))
    recent = [c for c in _recent_claims(claims, int(fleet.settings.get("claim_days", 2)), now) if c.get("blog") != blog.id]
    kept, skipped = [], []
    for c in candidates:
        clash = next((r for r in recent if _same_topic(c, r.get("keyword", ""), threshold)), None)
        if clash:
            skipped.append((c.keyword, f"{clash.get('blog')} 가 선점"))
        else:
            kept.append(c)
    return kept, skipped


def _same_topic(candidate: Candidate, claimed: str, threshold: float) -> bool:
    """후보(대표 키워드 + 다른 표현들)가 선점된 키워드와 같은 사건인지.

    유사도 외에, 짧은 쪽 토큰이 긴 쪽에 전부 들어가면(포함율 1.0) 같은 사건으로 봅니다.
    '최두호' ⊂ '최두호 핏불에게 1R TKO패' 가 그 경우입니다.
    """
    from .trends.base import tokenize

    claimed_tokens = tokenize(claimed)
    claimed_head = _head_token(claimed)
    for expr in [candidate.keyword, *candidate.variants]:
        if similarity(expr, claimed) >= threshold:
            return True
        toks = tokenize(expr)
        shorter, longer = (toks, claimed_tokens) if len(toks) <= len(claimed_tokens) else (claimed_tokens, toks)
        if shorter and shorter <= longer:
            return True
        # 실시간 검색어는 '주어(인물·팀·행사) + 서술' 꼴이 많습니다. 주어가 같으면 같은 날엔 같은 사건으로 봅니다.
        # ('최두호, 핏불에게 1R TKO패' 와 '최두호, UFC 첫 피니시 소감' — 유사도로는 다른 주제로 보입니다)
        if claimed_head and _head_token(expr) == claimed_head:
            return True
    return False


def _head_token(text: str) -> str:
    """키워드의 첫 토큰(주어). 3글자 미만이면 주어로 보기 어려워 빈 문자열."""
    import re as _re
    first = _re.sub(r"[^0-9A-Za-z가-힣]+", " ", text).split()
    return first[0].lower() if first and len(first[0]) >= 3 else ""


def claim(claims: dict, blog: Blog, keyword: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    claims.setdefault(day, []).append({"blog": blog.id, "keyword": keyword, "at": now.isoformat(timespec="seconds")})
    # 오래된 날짜 정리 (30일)
    for d in list(claims.keys()):
        try:
            if (now.date() - datetime.strptime(d, "%Y-%m-%d").date()).days > 30:
                del claims[d]
        except ValueError:
            del claims[d]
    return claims


# ---------------------------------------------------------------- 표시용

def schedule_table(fleet: Fleet) -> str:
    lines = ["| 슬롯(KST) | id | 이름 | 상태 | 글/일 | 비고 |", "|---|---|---|---|---:|---|"]
    for b in sorted(fleet.blogs, key=lambda b: b.slot_minutes):
        ppr = (b.overrides.get("run", {}) or {}).get("posts_per_run") or (fleet.settings.get("defaults", {}).get("run", {}) or {}).get("posts_per_run", "-")
        lines.append(f"| {b.slot} | {b.id} | {b.name} | {'켜짐' if b.enabled else '꺼짐'} | {ppr} | {b.manager_note or b.niche} |")
    return "\n".join(lines)
