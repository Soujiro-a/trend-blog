"""애드센스 사이트 승인 상태 — 블로그의 두 번째 슬롯을 켤지 정하는 데 씁니다 (2026-10-22 사용자 지시).

블로그가 4주를 넘겨도 애드센스 심사를 통과(사이트 상태 READY)하기 전에는 하루 1건만 씁니다. 수익이 나지 않는
블로그에 글을 더 쓰는 비용을 아끼고, 심사 중인 블로그는 글 수보다 품질로 보이게 하려는 것입니다.

함대 실행(src/fleet_run.py)이 몇 시간마다 애드센스 API 로 사이트 목록을 받아 data/fleet/adsense.json 에 적고,
슬롯 계산(src/fleet.py planned_slots)은 이 파일만 읽습니다. default 계정 블로그도 second 계정 애드센스에
관리자로 붙여 신청하므로, 자격증명이 있는 모든 계정의 사이트 목록을 합칩니다.
조회가 실패하면 이전 기록을 그대로 둡니다. 기록이 없으면 '승인 없음'으로 봅니다(슬롯을 늘리지 않는 쪽).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from . import fleet as fleet_mod
from . import net
from .state import KST

log = logging.getLogger(__name__)

API = "https://adsense.googleapis.com/v2"
STATE_PATH = fleet_mod.ADSENSE_STATE_PATH


def fetch_sites(token: str) -> dict[str, str]:
    """{도메인: 상태}. 애드센스 계정이 없거나 권한이 없으면 빈 dict, 네트워크 오류는 예외."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = net.session().get(f"{API}/accounts", headers=headers, timeout=30)
    if resp.status_code != 200:
        return {}
    out: dict[str, str] = {}
    for acc in resp.json().get("accounts", []):
        r = net.session().get(f"{API}/{acc['name']}/sites", headers=headers, timeout=30)
        if r.status_code == 200:
            for s in r.json().get("sites", []):
                if s.get("domain"):
                    out[s["domain"].lower()] = s.get("state", "")
    return out


def load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def refresh(fleet: fleet_mod.Fleet, max_age_hours: float = 3, now: datetime | None = None) -> dict:
    """기록이 max_age_hours 보다 오래됐으면 다시 받습니다. 받은 상태 기록을 돌려줍니다."""
    from .publishers import blogger

    now = now or datetime.now(KST)
    st = load()
    try:
        fresh = now - datetime.fromisoformat(st.get("checked_at", "")) < timedelta(hours=max_age_hours)
    except ValueError:
        fresh = False
    if fresh:
        return st
    sites: dict[str, str] = {}
    ok = False
    for acc in fleet.used_accounts:
        sample = fleet.blogs_of(acc, enabled_only=False)
        if not sample or fleet_mod.missing_credentials(fleet, acc):
            continue
        try:
            fleet_mod.apply_env(fleet, sample[0])
            sites.update(fetch_sites(blogger._access_token()))
            ok = True
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 이전 기록으로 버팁니다
            log.warning("애드센스 사이트 조회 실패 (계정 %s): %s", acc, exc)
    if not ok:
        return st
    st = {"checked_at": now.isoformat(timespec="minutes"), "sites": dict(sorted(sites.items()))}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    ready = sorted(d for d, s in sites.items() if s == "READY")
    log.info("애드센스 사이트 %d곳 · 승인(READY) %s", len(sites), ", ".join(ready) or "없음")
    return st
