"""발행 안전장치 — 로컬 상태가 어떻게 망가져도 과다 발행이 일어나지 않게 막습니다.

2026-09-20 사고 요약
--------------------
PC 스케줄러와 GitHub cron 이 겹쳐 함대가 하루 17회 실행됐고, 동시 실행이 이력 파일에
깃 충돌 마커를 남겼습니다. 당시 `state.load()` 는 깨진 파일을 조용히 '빈 이력'으로
처리했기 때문에 중복 방지가 풀려, 한 블로그가 하루 8건(상한 3건)을, 계정 전체가
16건을 발행했습니다. 신규 블로그 4개가 20분 안에 12건을 쏟아낸 패턴까지 겹쳐
구글이 계정의 Blogger API 쓰기를 차단했습니다(403 PERMISSION_DENIED).

그래서 이 모듈의 원칙은 하나입니다: **상한은 로컬 파일이 아니라 Blogger 서버에 물어본다.**
로컬 이력이 사라지든 깨지든, 오늘 이미 올라간 글 수는 서버가 알고 있습니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .publishers import blogger
from .state import KST

log = logging.getLogger(__name__)


@dataclass
class Budget:
    """이번 실행에서 이 블로그가 공개해도 되는 글 수."""

    allowed: int
    already_today: int
    daily_cap: int
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.allowed <= 0


def _day_start_iso(now: datetime) -> str:
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def daily_budget(cfg: dict, blog_id: str | None = None, now: datetime | None = None) -> Budget:
    """오늘 이 블로그에 몇 건 더 올려도 되는지 서버 기준으로 계산합니다.

    서버 조회가 실패하면 **0건**을 돌려줍니다(fail-closed). 모르는 상태에서 올리는 것이
    바로 이번 사고의 원인이었으므로, 알 수 없으면 올리지 않습니다.
    """
    now = now or datetime.now(KST)
    pub = cfg["publish"]
    cap = int(pub.get("max_live_per_day", pub.get("max_live_per_run", 2)))

    try:
        already = blogger.published_since(_day_start_iso(now), blog_id)
    except Exception as exc:  # noqa: BLE001
        log.error("오늘 발행 수를 확인하지 못해 이번 실행은 발행하지 않습니다: %s", exc)
        return Budget(0, -1, cap, f"발행 수 확인 실패({str(exc)[:80]})")

    allowed = max(0, cap - already)
    reason = "" if allowed else f"오늘 이미 {already}건 공개 (상한 {cap}건)"
    return Budget(allowed, already, cap, reason)


def account_budget(fleet, account: str, now: datetime | None = None) -> Budget:
    """**한 구글 계정**이 오늘 몇 건 더 올려도 되는지. 블로그별 상한만으로는 부족합니다.

    2026-09-20 차단 당시 블로그별로는 2~8건이었지만 **계정 전체로는 하루 16건**이었습니다.
    구글의 제한은 계정에 붙으므로, 한 계정에 블로그를 여러 개 두면 이 상한이 실질적인 제동장치입니다.
    계정을 나누면 상한도 계정마다 따로 계산됩니다 — 그게 계정을 나누는 실익이기도 합니다.

    계정별 자격증명으로 바꿔 가며 세기 때문에, 호출 뒤 환경변수는 마지막 블로그 계정 상태로 남습니다.
    호출부는 이어서 쓸 블로그에 대해 다시 apply_env 를 부르세요.
    """
    from . import fleet as fleet_mod

    now = now or datetime.now(KST)
    cap = int(fleet.account_setting(account, "max_live_per_day_account", 6))
    start = _day_start_iso(now)

    total = 0
    for blog in fleet.blogs_of(account):
        try:
            fleet_mod.apply_env(fleet, blog)
            total += blogger.published_since(start, blog.blog_id)
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] 오늘 발행 수 확인 실패 — 계정 '%s' 상한을 계산할 수 없습니다: %s", blog.id, account, exc)
            return Budget(0, -1, cap, f"{blog.id} 발행 수 확인 실패")

    allowed = max(0, cap - total)
    reason = "" if allowed else f"계정 '{account}' 이 오늘 이미 {total}건 공개 (계정 상한 {cap}건)"
    return Budget(allowed, total, cap, reason)


def min_gap_ok(cfg: dict, blog_id: str | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """직전 발행과 최소 간격이 확보됐는지. 몇 분 사이에 여러 건을 올리는 패턴을 막습니다.

    9/19 에 신규 블로그 4개가 20분 안에 12건을 올린 것이 자동 스팸 신호로 잡혔습니다.
    """
    now = now or datetime.now(KST)
    gap_min = int(cfg["publish"].get("min_gap_minutes", 0))
    if gap_min <= 0:
        return True, ""
    since = (now - timedelta(minutes=gap_min)).isoformat()
    try:
        recent = blogger.published_since(since, blog_id)
    except Exception as exc:  # noqa: BLE001
        return False, f"최근 발행 확인 실패({str(exc)[:60]})"
    if recent:
        return False, f"최근 {gap_min}분 안에 {recent}건 공개됨 — 간격 확보 대기"
    return True, ""
