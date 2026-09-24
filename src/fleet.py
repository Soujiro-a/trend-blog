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
from .trends.base import normalize, similarity

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
    # 하루에 이 블로그가 글을 올리는 시각들. 슬롯 하나당 글 1건입니다.
    # 여러 건을 한 번에 몰아 올리면(2026-09-20: 2분 30초에 3건) 자동 스팸 신호로 잡히므로
    # 하루 2건이면 아침·오후로 나눠 slots 를 두 개 둡니다.
    slots: list[str] = field(default_factory=lambda: ["06:20"])
    account: str = "default"
    enabled: bool = True
    # 2026-09-20 사고 이후 기본 운영 방식. planned: 블로그 고유 주제 안에서 글감을 기획(권장).
    # trend: 실시간 검색어 추종(블로그 간 주제 충돌 위험이 구조적으로 남아 기본값에서 제외).
    content_mode: str = "planned"
    subject: str = ""                                   # 이 블로그만의 고유 주제 (다른 블로그와 겹치면 안 됨)
    pillars: list[str] = field(default_factory=list)    # 주제를 나눈 하위 축
    audience: str = ""                                  # 누가 읽는지
    niche: str = ""
    persona: str = ""
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=lambda: ["실시간이슈"])
    overrides: dict = field(default_factory=dict)
    # 소개 페이지 문구(goal / gap / caution). scripts/setup_pages.py 가 씁니다. 비우면 기본 문구.
    page: dict = field(default_factory=dict)
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
    def slot(self) -> str:
        """첫 슬롯. 표·보고서에서 블로그를 정렬·표시할 때 씁니다."""
        return self.slots[0] if self.slots else "06:20"

    @property
    def slot_minutes(self) -> int:
        return to_minutes(self.slot)


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


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

    def blogs_of(self, account: str, enabled_only: bool = True) -> list[Blog]:
        return [b for b in self.blogs if b.account == account and (b.enabled or not enabled_only)]

    @property
    def used_accounts(self) -> list[str]:
        """실제로 블로그가 붙어 있는 계정 이름들 (켜진 블로그 기준)."""
        return list(dict.fromkeys(b.account for b in self.blogs if b.enabled))

    def account_setting(self, account: str, key: str, default):
        """계정별 설정. 없으면 함대 공통 설정, 그것도 없으면 default.

        구글의 제한은 계정 단위로 붙으므로, 계정마다 다른 한도를 둘 수 있어야 합니다.
        새로 만든 계정은 낮게, 오래 운영해 신뢰가 쌓인 계정은 조금 높게.
        """
        acc = self.accounts.get(account) or {}
        if key in acc:
            return acc[key]
        return self.settings.get(key, default)


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


def _read_slots(entry: dict, settings: dict) -> list[str]:
    """slots(목록) 우선, 없으면 옛 slot(단수) 도 받습니다."""
    raw = entry.get("slots") or entry.get("slot") or settings.get("slot_start", "06:20")
    slots = [str(raw)] if isinstance(raw, str) else [str(s) for s in raw]
    return sorted(dict.fromkeys(slots), key=to_minutes)


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
            slots=_read_slots(entry, settings),
            account=str(entry.get("account", "default")),
            enabled=bool(entry.get("enabled", True)),
            content_mode=str(entry.get("content_mode", "planned")),
            subject=str(entry.get("subject") or ""),
            pillars=list(entry.get("pillars") or []),
            audience=str(entry.get("audience") or ""),
            niche=str(entry.get("niche") or ""),
            persona=str(entry.get("persona") or ""),
            include_patterns=list(entry.get("include_patterns") or []),
            exclude_patterns=list(entry.get("exclude_patterns") or []),
            labels=list(entry.get("labels") or ["실시간이슈"]),
            overrides=dict(entry.get("overrides") or {}),
            page=dict(entry.get("page") or {}),
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
    subjects: dict[str, str] = {}  # subject -> blog id
    for b in fleet.blogs:
        if b.account not in fleet.accounts:
            raise ValueError(f"{b.id}: 계정 '{b.account}' 이(가) accounts 에 없습니다")
        if not b.slots:
            raise ValueError(f"{b.id}: 슬롯이 없습니다")
        for s in b.slots:
            try:
                mins = to_minutes(s)
            except ValueError as exc:
                raise ValueError(f"{b.id}: 슬롯 형식 오류 '{s}'") from exc
            if not (0 <= mins < 24 * 60):
                raise ValueError(f"{b.id}: 슬롯 범위 오류 '{s}'")
        if b.content_mode not in ("planned", "trend"):
            raise ValueError(f"{b.id}: content_mode 는 planned 또는 trend 여야 합니다 ('{b.content_mode}')")
        if b.enabled and b.content_mode == "planned":
            if not b.subject:
                raise ValueError(f"{b.id}: planned 모드에는 subject(고유 주제)가 필요합니다")
            # 주제가 겹치면 블로그를 나눈 의미가 없고, 중복 콘텐츠 위험이 그대로 돌아옵니다.
            # 어순만 바꾼 주제도 같은 것으로 봅니다.
            for prev_subject, prev_id in subjects.items():
                if similarity(b.subject, prev_subject) >= 0.7:
                    raise ValueError(
                        f"{b.id}: subject '{b.subject}' 가 {prev_id} 의 '{prev_subject}' 와 겹칩니다 "
                        "— 블로그마다 다른 주제를 쓰세요"
                    )
            subjects[b.subject] = b.id
    # 함대 전체의 모든 슬롯이 서로 step 분 이상 떨어져 있어야 합니다.
    # 여러 블로그가 같은 시각에 올리면 계정 전체가 한꺼번에 움직이는 것으로 보입니다.
    all_slots = sorted((to_minutes(s), b.id) for b in fleet.blogs if b.enabled for s in b.slots)
    for (a, ida), (c, idc) in zip(all_slots, all_slots[1:]):
        if c - a < fleet.step:
            raise ValueError(
                f"슬롯 간격이 {fleet.step}분보다 좁습니다: {ida} {to_hhmm(a)} 와 {idc} {to_hhmm(c)}"
            )


# ---------------------------------------------------------------- 슬롯

def next_free_slots(fleet: Fleet, count: int = 1, extra_taken: list[str] | None = None) -> list[str]:
    """빈 슬롯을 count 개 찾습니다. 기존 슬롯 전부와 step 분 이상 떨어뜨립니다.

    같은 블로그의 슬롯끼리는 min_gap_minutes 이상 벌려, 하루치 글이 몇 분 안에 몰리지 않게 합니다.
    """
    start = to_minutes(str(fleet.settings.get("slot_start", "06:20")))
    end = to_minutes(str(fleet.settings.get("slot_end", "21:00")))
    own_gap = int(fleet.settings.get("own_slot_gap_minutes", 240))
    taken = [to_minutes(s) for b in fleet.blogs for s in b.slots]
    taken += [to_minutes(s) for s in (extra_taken or [])]

    out: list[str] = []
    # extra_taken 은 "아직 blogs.yaml 에 없지만 이미 배정하기로 한 슬롯"입니다.
    # 같은 블로그의 슬롯 간격(own_gap)은 이번에 새로 뽑는 것들끼리만 따집니다.
    mine: list[int] = []
    t = start
    while len(out) < count:
        if t > end:
            raise ValueError(
                f"슬롯이 다 찼습니다 ({fleet.settings.get('slot_start')}~{fleet.settings.get('slot_end')}, "
                f"간격 {fleet.step}분). slot_end 를 늘리거나 slot_step_minutes 를 줄이세요."
            )
        if all(abs(t - s) >= fleet.step for s in taken) and all(abs(t - s) >= own_gap for s in mine):
            out.append(to_hhmm(t))
            taken.append(t)
            mine.append(t)
        t += fleet.step
    return out


def next_free_slot(fleet: Fleet) -> str:
    return next_free_slots(fleet, 1)[0]


def load_runs(blog: Blog) -> dict:
    if not blog.runs_path.exists():
        return {}
    try:
        return json.loads(blog.runs_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# 슬롯 하나를 하루에 몇 번까지 시도할지. 성공하면 그걸로 끝이고, 실패하면 다음 실행 기회에 한 번 더 합니다.
# 2026-09-23 에 글감 기획 응답이 잘려 한 번 실패한 슬롯이 그날 다시 시도되지 않아 하루치 글이 날아갔습니다.
# 무한 재시도는 비용 사고가 나므로 2회로 묶습니다. (API 한도·인증 문제는 fleet_run 이 아예 기록하지 않습니다)
SLOT_MAX_ATTEMPTS = 2


def mark_ran(blog: Blog, mode: str, ok: bool, summary: str, now: datetime | None = None, slot: str | None = None) -> None:
    now = now or datetime.now(KST)
    runs = load_runs(blog)
    key = _run_key(mode, now, slot)
    attempts = int((runs.get(key) or {}).get("attempts", 0)) + 1
    runs[key] = {
        "at": now.isoformat(timespec="seconds"),
        "ok": ok,
        "attempts": attempts,
        "summary": summary[:300],
    }
    # 최근 120건만 유지 (시각 순으로 — 키 문자열 순으로 자르면 모드별로 엉뚱한 것이 지워집니다)
    keep = sorted(runs.items(), key=lambda kv: kv[1].get("at", ""))[-120:]
    runs = dict(keep)
    blog.runs_path.parent.mkdir(parents=True, exist_ok=True)
    blog.runs_path.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")


def _slot_done(record: dict | None) -> bool:
    """이 슬롯을 오늘 더 시도하지 않아도 되는지. 성공했거나, 시도 한도를 다 썼으면 끝."""
    if not record:
        return False
    return bool(record.get("ok")) or int(record.get("attempts", 1)) >= SLOT_MAX_ATTEMPTS


def _run_key(mode: str, now: datetime, slot: str | None) -> str:
    base = f"{mode}:{now.strftime('%Y-%m-%d')}"
    return f"{base}:{slot}" if slot else base


def ran_today(blog: Blog, mode: str, now: datetime | None = None, slot: str | None = None) -> bool:
    now = now or datetime.now(KST)
    runs = load_runs(blog)
    if slot is None:
        # 슬롯을 안 주면 "오늘 끝난 실행이 하나라도 있나"
        prefix = f"{mode}:{now.strftime('%Y-%m-%d')}"
        return any((k == prefix or k.startswith(prefix + ":")) and _slot_done(v) for k, v in runs.items())
    return _slot_done(runs.get(_run_key(mode, now, slot)))


def due_slots(fleet: Fleet, now: datetime | None = None, mode: str = "planned") -> list[tuple[Blog, str]]:
    """지금 처리해야 할 (블로그, 슬롯) 목록. 슬롯 시각 순서대로.

    슬롯 하나 = 글 한 건입니다. catch_up 이 켜져 있으면 '시각이 지났고 아직 안 돈' 슬롯 전부,
    꺼져 있으면 현재 step 분 창 안의 슬롯만.
    """
    now = now or datetime.now(KST)
    minute_now = now.hour * 60 + now.minute
    catch_up = bool(fleet.settings.get("catch_up", True))
    due: list[tuple[int, Blog, str]] = []
    for b in fleet.blogs:
        if not b.enabled:
            continue
        for s in b.slots:
            if ran_today(b, mode, now, s):
                continue
            mins = to_minutes(s)
            if (mins <= minute_now) if catch_up else (mins <= minute_now < mins + fleet.step):
                due.append((mins, b, s))
    return [(b, s) for _, b, s in sorted(due, key=lambda x: x[0])]


def due_blogs(fleet: Fleet, now: datetime | None = None, mode: str = "planned") -> list[Blog]:
    """due_slots 의 블로그만. 같은 블로그가 여러 슬롯이면 중복 없이 한 번."""
    seen, out = set(), []
    for b, _ in due_slots(fleet, now, mode):
        if b.id not in seen:
            seen.add(b.id)
            out.append(b)
    return out


# ---------------------------------------------------------------- 실행 컨텍스트

def env_name(base: str, account: str) -> str:
    return base if account == "default" else f"{base}_{account.upper()}"


CREDENTIAL_VARS = ("BLOGGER_REFRESH_TOKEN", "BLOGGER_CLIENT_ID", "BLOGGER_CLIENT_SECRET")

# default 계정의 원본 자격증명. apply_env 가 표준 변수명을 덮어쓰기 때문에, 한 번 다른 계정으로
# 바꾸고 나면 원본을 잃어버립니다. 그 상태로 default 블로그를 처리하면 **직전 계정의 토큰으로
# default 블로그에 글을 쓰게 됩니다.** 그래서 처음 본 값을 따로 보관해 두고 복원합니다.
_default_credentials: dict[str, str] | None = None


def _snapshot_default() -> dict[str, str]:
    global _default_credentials
    if _default_credentials is None:
        _default_credentials = {v: os.environ.get(v, "") for v in CREDENTIAL_VARS}
    return _default_credentials


def account_credentials(account: str) -> dict[str, str]:
    """그 계정의 자격증명 값 {표준변수명: 값}. 없으면 빈 문자열."""
    if account == "default":
        return dict(_snapshot_default())
    return {base: os.environ.get(env_name(base, account), "") for base in CREDENTIAL_VARS}


def apply_env(fleet: Fleet, blog: Blog) -> None:
    """이 블로그 계정의 자격증명을 표준 변수명에 올립니다.

    계정이 'default' 가 아니면 BLOGGER_REFRESH_TOKEN_<계정대문자> 같은 계정별 변수를 찾아
    BLOGGER_REFRESH_TOKEN 자리에 넣고, 'default' 면 처음 보관해 둔 원본으로 되돌립니다.
    publishers/blogger.py 는 표준 이름만 보면 되고, 토큰 캐시가 자격증명 해시로 키를 잡으므로
    계정이 바뀌면 캐시도 자동으로 갈립니다.

    자격증명이 비어 있으면 표준 변수를 **지웁니다**. 이전 계정 값이 남아 엉뚱한 계정에
    글을 쓰는 것보다, 인증 오류로 실패하는 편이 안전합니다.
    """
    _snapshot_default()
    os.environ["BLOGGER_BLOG_ID"] = blog.blog_id
    os.environ["FLEET_BLOG"] = blog.id
    os.environ["FLEET_ACCOUNT"] = blog.account
    for base, value in account_credentials(blog.account).items():
        if value:
            os.environ[base] = value
        else:
            os.environ.pop(base, None)


def missing_credentials(fleet: Fleet, account: str) -> list[str]:
    """이 계정에 필요한데 없는 환경변수 이름들. 비어 있으면 정상입니다."""
    _snapshot_default()
    values = account_credentials(account)
    return [env_name(base, account) for base in CREDENTIAL_VARS if not values.get(base)]


def credential_report(fleet: Fleet) -> dict[str, list[str]]:
    """계정별로 빠진 자격증명. 실행 전 점검과 CLI 표시에 씁니다."""
    return {acc: missing_credentials(fleet, acc) for acc in fleet.used_accounts}


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
        hay = " ".join([c.keyword, *c.variants, *c.headline_hits, c.note])
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
    lines = ["| 슬롯(KST) | id | 이름 | 상태 | 글/일 | 주제 |", "|---|---|---|---|---:|---|"]
    for b in sorted(fleet.blogs, key=lambda b: b.slot_minutes):
        lines.append(
            f"| {', '.join(b.slots)} | {b.id} | {b.name} | {'켜짐' if b.enabled else '꺼짐'} | "
            f"{len(b.slots)} | {b.manager_note or b.subject or b.niche} |"
        )
    return "\n".join(lines)
