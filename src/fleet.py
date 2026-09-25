"""블로그 함대(fleet): 여러 Blogger 블로그를 같은 파이프라인으로, 서로 다른 시간에 운영합니다.

한 블로그를 '실행 컨텍스트'로 바꾸는 일이 이 모듈의 전부입니다.

* fleet/blogs.yaml 을 읽고 관리 에이전트의 상태(data/fleet/manager_state.json)를 덧씌움
* 블로그별 데이터 폴더 (data/blogs/<id>/history.json, last_run.md, runs.json)
* 블로그별 환경변수 (BLOGGER_BLOG_ID, 계정별 토큰)
* 블로그별 config 덮어쓰기 (fleet.defaults → blog.overrides)
* 슬롯 계산: 오늘 아직 안 돌았고 슬롯 시각이 지난 블로그 = 실행 대상
* 램프업: 블로그·계정 나이에 따라 켜지는 슬롯 수와 계정 하루 상한이 **자동으로** 늘어남
* 계정 비상정지: Blogger 가 403 을 돌려주면 그 계정의 모든 블로그를 즉시 멈춤
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
# 계정 비상정지 상태. manager_state 와 파일을 나눈 이유: 함대 실행(비상정지)과 관리 에이전트(매일 밤)가
# 서로 다른 워크플로에서 쓰므로, 한 파일을 같이 쓰면 깃 병합 충돌이 날 수 있습니다.
ACCOUNT_STATE_PATH = FLEET_DATA / "account_state.json"

# 램프업 기본값 — [나이(일), 값] 목록. 나이가 그 일수 이상이면 그 값이 적용됩니다.
# 블로그: 하루에 켜지는 슬롯 수. 계정: 계정 전체의 하루 공개 상한.
# blogs.yaml 의 fleet.blog_ramp / accounts.<이름>.ramp 로 바꿀 수 있습니다.
DEFAULT_BLOG_RAMP = [[0, 1], [28, 2]]
DEFAULT_ACCOUNT_RAMP = [[0, 4], [28, 6], [56, 8]]


@dataclass
class Blog:
    id: str
    name: str
    blog_id: str
    # 하루에 이 블로그가 글을 올리는 시각들. 슬롯 하나당 글 1건입니다.
    # 여러 건을 한 번에 몰아 올리면(2026-09-20: 2분 30초에 3건) 자동 스팸 신호로 잡히므로
    # 하루 2건이면 아침·오후로 나눠 slots 를 두 개 둡니다.
    slots: list[str] = field(default_factory=lambda: ["06:20"])
    # 첫 글을 올린 날(YYYY-MM-DD). 램프업 기준입니다. 비우면 '오늘 시작한 블로그'로 봅니다(가장 보수적).
    # 미래 날짜면 그날까지 글을 올리지 않습니다 — 새 블로그 여러 개를 날짜를 벌려 순서대로 시작할 때 씁니다.
    since: str = ""
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


def load_manager_state(path: Path | None = None) -> dict:
    # 경로를 호출할 때 정합니다 (계정 상태와 같게). 기본값에 박아 두면 테스트가 MANAGER_STATE_PATH 를
    # 임시 파일로 바꿔도 실제 data/fleet/manager_state.json 을 읽고 씁니다.
    path = path or MANAGER_STATE_PATH
    if not path.exists():
        return {"blogs": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("관리 상태 파일을 읽지 못해 무시합니다: %s", exc)
        return {"blogs": {}}


def save_manager_state(state: dict, path: Path | None = None) -> None:
    path = path or MANAGER_STATE_PATH
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
            since=str(entry.get("since") or ""),
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
        if b.since and _parse_date(b.since) is None:
            raise ValueError(f"{b.id}: since 는 YYYY-MM-DD 형식이어야 합니다 ('{b.since}')")
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
    # 계정 하나에 블로그를 무한정 붙이지 않습니다. 계정 상한이 있어 글 수는 어차피 안 늘고,
    # 한 계정이 막히면 그 계정의 블로그가 전부 멈추므로 피해 범위만 커집니다.
    max_blogs = int(fleet.settings.get("max_blogs_per_account", 6))
    for acc in fleet.accounts:
        n = len(fleet.blogs_of(acc))
        if n > max_blogs:
            raise ValueError(
                f"계정 '{acc}' 에 켜진 블로그가 {n}개입니다 (계정당 최대 {max_blogs}개). "
                "새 블로그는 새 계정에 만드세요 — python scripts/account_cli.py add <이름>"
            )
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


# ---------------------------------------------------------------- 계정 비상정지
#
# 2026-09-20 차단 때는 403 이 난 뒤에도 실행이 계속 돌았습니다. 글을 쓰는 데 드는 모델 비용은
# 그대로 나가고, 막힌 계정에 쓰기 요청을 반복하는 것 자체도 좋을 게 없습니다.
# 그래서 Blogger 가 쓰기 요청에 403 을 돌려주면 그 계정을 즉시 멈추고, **사람이 풀 때까지** 멈춰 둡니다.
# 푸는 명령(account_cli.py resume)은 램프업도 처음 단계로 되돌립니다.

def load_account_state(path: Path | None = None) -> dict:
    path = path or ACCOUNT_STATE_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # 읽을 수 없으면 멈춘 계정이 있었는지 알 수 없습니다. 모르는 채로 발행하지 않도록 전부 멈춘 것으로 봅니다.
        log.error("계정 상태 파일을 읽지 못했습니다 — 모든 계정을 멈춘 것으로 봅니다: %s", exc)
        return {"*": {"halted": True, "reason": f"account_state.json 손상: {exc}"}}


def save_account_state(st: dict, path: Path | None = None) -> None:
    path = path or ACCOUNT_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def account_halted(account: str, st: dict | None = None) -> str:
    """멈춘 계정이면 그 이유, 아니면 빈 문자열."""
    st = load_account_state() if st is None else st
    for key in ("*", account):
        entry = st.get(key) or {}
        if entry.get("halted"):
            return str(entry.get("reason") or "비상정지")
    return ""


def halt_account(account: str, reason: str, now: datetime | None = None) -> None:
    now = now or datetime.now(KST)
    st = load_account_state()
    entry = st.setdefault(account, {})
    if not entry.get("halted"):
        entry["halted_at"] = now.isoformat(timespec="seconds")
    entry.update({"halted": True, "reason": reason[:300]})
    save_account_state(st)
    log.error("계정 '%s' 비상정지: %s", account, reason[:200])


def resume_account(account: str, now: datetime | None = None) -> None:
    """비상정지를 풉니다. 램프업은 오늘부터 다시 시작합니다(가장 낮은 단계)."""
    now = now or datetime.now(KST)
    st = load_account_state()
    entry = st.setdefault(account, {})
    entry.update({"halted": False, "ramp_from": now.strftime("%Y-%m-%d"), "resumed_at": now.isoformat(timespec="seconds")})
    save_account_state(st)


# ---------------------------------------------------------------- 램프업 (자동 증량)
#
# 발행량은 사람이 날짜를 기억했다가 손으로 올리지 않고, 블로그·계정의 **나이**로 정해집니다.
#   - 블로그: 처음 4주는 하루 1건, 그 뒤 슬롯에 적힌 만큼(최대 2건)
#   - 계정  : 처음 4주는 하루 4건, 8주까지 6건, 그 뒤 8건 (단, accounts.<이름>.max_live_per_day_account 를 넘지 않음)
# 그래서 슬롯은 미리 2개씩 적어 둬도 됩니다. 때가 되면 저절로 켜집니다.
# 계정 상한보다 켜질 슬롯이 많으면 **가장 어린 블로그의 추가 슬롯부터** 꺼서 상한에 맞춥니다.
# 즉 블로그를 새로 붙여도 계정의 하루 총량은 늘지 않고, 새 블로그는 자리가 날 때까지 기다립니다.

def _parse_date(s: str):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _stage(ramp: list, age_days: int) -> int:
    value = 0
    for days, v in sorted(ramp, key=lambda x: int(x[0])):
        if age_days >= int(days):
            value = int(v)
    return value


def _ramp_start(fleet: Fleet, account: str, st: dict | None = None):
    """계정 램프업 기준일: accounts.<이름>.since 와 비상정지 해제일(ramp_from) 중 늦은 날."""
    st = load_account_state() if st is None else st
    dates = [
        _parse_date((fleet.accounts.get(account) or {}).get("since") or ""),
        _parse_date((st.get(account) or {}).get("ramp_from") or ""),
    ]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def account_cap(fleet: Fleet, account: str, now: datetime | None = None, st: dict | None = None) -> int:
    """오늘 이 계정이 공개할 수 있는 총 글 수 (램프업 적용, 최종 상한 이하)."""
    now = now or datetime.now(KST)
    ceiling = int(fleet.account_setting(account, "max_live_per_day_account", 6))
    ramp = (fleet.accounts.get(account) or {}).get("ramp") or fleet.settings.get("account_ramp") or DEFAULT_ACCOUNT_RAMP
    start = _ramp_start(fleet, account, st)
    age = (now.date() - start).days if start else 0   # 시작일을 모르면 첫 단계
    return min(ceiling, _stage(ramp, age))


def _first_live_date(blog: Blog):
    """이력에서 처음 공개한 날. since 를 안 적은 블로그의 나이를 여기서 구합니다."""
    try:
        entries = json.loads(blog.history_path.read_text(encoding="utf-8"))
        dates = [_parse_date(e.get("posted_at", "")) for e in entries if e.get("status") == "live"]
        dates = [d for d in dates if d]
        return min(dates) if dates else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def last_live_at(blog: Blog) -> datetime | None:
    """이력에서 마지막으로 공개한 시각. 같은 계정 발행 간격 계산에 씁니다."""
    try:
        entries = json.loads(blog.history_path.read_text(encoding="utf-8"))
        times = [datetime.fromisoformat(e["posted_at"]) for e in entries if e.get("status") == "live" and e.get("posted_at")]
        return max(times) if times else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def blog_age_days(fleet: Fleet, blog: Blog, now: datetime | None = None, st: dict | None = None) -> int:
    """블로그 나이. since → 없으면 이력의 첫 공개일 → 그것도 없으면 오늘(0일).
    비상정지가 풀린 계정이면 해제일부터 다시 셉니다."""
    now = now or datetime.now(KST)
    start = (_parse_date(blog.since) if blog.since else None) or _first_live_date(blog) or now.date()
    st = load_account_state() if st is None else st
    restart = _parse_date((st.get(blog.account) or {}).get("ramp_from") or "")
    if restart and restart > start:
        start = restart
    return (now.date() - start).days


def planned_slots(fleet: Fleet, now: datetime | None = None, st: dict | None = None) -> dict[str, list[str]]:
    """오늘 실제로 켜지는 슬롯 {블로그 id: [슬롯, ...]}. 램프업·계정 상한·비상정지를 모두 반영합니다."""
    now = now or datetime.now(KST)
    st = load_account_state() if st is None else st
    blog_ramp = fleet.settings.get("blog_ramp") or DEFAULT_BLOG_RAMP
    out: dict[str, list[str]] = {}
    for acc in fleet.accounts:
        blogs = fleet.blogs_of(acc)
        if not blogs:
            continue
        if account_halted(acc, st):
            for b in blogs:
                out[b.id] = []
            continue
        ages = {b.id: blog_age_days(fleet, b, now, st) for b in blogs}
        active = {
            b.id: (b.slots[: _stage(blog_ramp, ages[b.id])] if ages[b.id] >= 0 else [])
            for b in blogs
        }
        cap = account_cap(fleet, acc, now, st)
        # 상한을 넘으면 어린 블로그부터 줄입니다: 먼저 추가 슬롯을, 그래도 넘으면 첫 슬롯까지.
        youngest_first = sorted(blogs, key=lambda b: (ages[b.id], b.id))
        for keep_first in (True, False):
            for b in youngest_first:
                floor = 1 if keep_first else 0
                while sum(len(v) for v in active.values()) > cap and len(active[b.id]) > floor:
                    active[b.id].pop()
        out.update(active)
    return out


def due_slots(fleet: Fleet, now: datetime | None = None, mode: str = "planned") -> list[tuple[Blog, str]]:
    """지금 처리해야 할 (블로그, 슬롯) 목록. 슬롯 시각 순서대로.

    슬롯 하나 = 글 한 건입니다. catch_up 이 켜져 있으면 '시각이 지났고 아직 안 돈' 슬롯 전부,
    꺼져 있으면 현재 step 분 창 안의 슬롯만. 램프업으로 아직 안 켜진 슬롯과 멈춘 계정은 빠집니다.
    """
    now = now or datetime.now(KST)
    minute_now = now.hour * 60 + now.minute
    catch_up = bool(fleet.settings.get("catch_up", True))
    active = planned_slots(fleet, now)
    due: list[tuple[int, Blog, str]] = []
    for b in fleet.blogs:
        if not b.enabled:
            continue
        for s in active.get(b.id, []):
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

def schedule_table(fleet: Fleet, now: datetime | None = None) -> str:
    """슬롯표. 램프업으로 아직 안 켜진 슬롯은 (괄호)로 표시합니다."""
    now = now or datetime.now(KST)
    st = load_account_state()
    plan = planned_slots(fleet, now, st)
    lines = ["| 슬롯(KST) | id | 이름 | 계정 | 상태 | 나이 | 오늘 글 | 주제 |", "|---|---|---|---|---|---:|---:|---|"]
    for b in sorted(fleet.blogs, key=lambda b: (b.account, b.slot_minutes)):
        on = plan.get(b.id, []) if b.enabled else []
        slots = ", ".join(s if s in on else f"({s})" for s in b.slots)
        status = "켜짐" if b.enabled else "꺼짐"
        if b.enabled and account_halted(b.account, st):
            status = "⛔ 계정 비상정지"
        age = f"{blog_age_days(fleet, b, now, st)}일" if b.enabled else "-"
        lines.append(
            f"| {slots} | {b.id} | {b.name} | {b.account} | {status} | {age} | "
            f"{len(on)} | {b.manager_note or b.subject or b.niche} |"
        )
    return "\n".join(lines)


def ramp_warnings(fleet: Fleet, now: datetime | None = None) -> list[str]:
    """당장 막을 일은 아니지만 사람이 알아야 할 것들 (fleet_cli validate/list 에서 표시)."""
    now = now or datetime.now(KST)
    out = []
    for b in fleet.blogs:
        if b.enabled and not b.since:
            out.append(f"{b.id}: since(첫 글 날짜)가 없습니다 — 이력의 첫 공개일로 대신 셉니다")
    for acc in fleet.accounts:
        blogs = fleet.blogs_of(acc)
        if not blogs:
            continue
        ceiling = int(fleet.account_setting(acc, "max_live_per_day_account", 6))
        full = sum(len(b.slots) for b in blogs)
        if full > ceiling:
            out.append(
                f"계정 '{acc}': 적어 둔 슬롯 {full}개 > 최종 상한 {ceiling}건 — "
                f"램프업이 끝나도 {full - ceiling}개 슬롯은 켜지지 않습니다"
            )
        # 새 블로그를 한꺼번에 여러 개 시작하는 패턴 (2026-09-19: 새 블로그 4개가 동시에 시작 → 차단)
        starts = sorted(
            d for d in (_parse_date(b.since) for b in blogs if b.since)
            if d and 0 <= (now.date() - d).days < 14
        )
        for a, c in zip(starts, starts[1:]):
            if (c - a).days < 7:
                out.append(
                    f"계정 '{acc}': 새 블로그가 {a}·{c} 에 잇달아 시작했습니다 — "
                    "다음 블로그는 1주 이상 간격을 두고 시작하세요 (since 를 미래 날짜로 두면 그날부터 돕니다)"
                )
                break
    return out
