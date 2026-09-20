"""작성 이력 관리. 같은 주제로 두 번 쓰지 않게 막습니다."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR
from .trends import Candidate
from .trends.base import similarity

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
HISTORY_PATH = DATA_DIR / "history.json"


def _now() -> datetime:
    return datetime.now(KST)


class HistoryCorrupted(Exception):
    """이력 파일이 깨졌습니다. 이 상태로 진행하면 중복 방지가 통째로 풀립니다."""


def load(path: Path = HISTORY_PATH) -> list[dict]:
    """작성 이력을 읽습니다. 파일이 없으면 빈 이력, **깨졌으면 예외**.

    2026-09-20 사고: 동시 실행이 이 파일을 두고 깃 충돌을 일으켜 충돌 마커가 커밋됐고,
    그때 이 함수가 조용히 빈 이력을 돌려주는 바람에 중복 방지가 풀려 같은 블로그가
    하루 8건을 발행했습니다(정상 상한 3건). 계정이 정책 위반으로 API 쓰기 차단됐습니다.
    깨진 이력은 '이력 없음'이 아니라 '알 수 없음'이므로, 조용히 넘기지 않고 멈춥니다.
    """
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HistoryCorrupted(f"이력 파일을 읽지 못했습니다: {path} ({exc})") from exc

    if "<<<<<<<" in raw or ">>>>>>>" in raw:
        raise HistoryCorrupted(
            f"이력 파일에 깃 충돌 마커가 있습니다: {path}. "
            "동시 실행이 겹친 흔적입니다. 해결 전에는 발행하지 않습니다."
        )
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HistoryCorrupted(f"이력 파일이 깨졌습니다: {path} ({exc})") from exc
    if not isinstance(entries, list):
        raise HistoryCorrupted(f"이력 파일 형식이 목록이 아닙니다: {path}")
    return entries


def save(entries: list[dict], path: Path = HISTORY_PATH) -> None:
    """원자적으로 저장합니다. 중간에 죽어도 반쪽짜리 파일이 남지 않게 임시 파일에 쓰고 바꿉니다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def recent(entries: list[dict], days: int) -> list[dict]:
    cutoff = _now() - timedelta(days=days)
    out = []
    for e in entries:
        try:
            when = datetime.fromisoformat(e["posted_at"])
        except (KeyError, ValueError):
            continue
        if when >= cutoff:
            out.append(e)
    return out


def filter_seen(
    cfg: dict, candidates: list[Candidate], entries: list[dict]
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """최근에 다룬 주제를 제외합니다. (남은 후보, [(키워드, 겹친 이력)]) 반환."""
    days = cfg["dedupe"]["history_days"]
    threshold = cfg["dedupe"]["similarity_threshold"]
    history = recent(entries, days)

    kept: list[Candidate] = []
    skipped: list[tuple[str, str]] = []
    for c in candidates:
        clash = next(
            (
                h["keyword"]
                for h in history
                if similarity(c.keyword, h.get("keyword", "")) >= threshold
            ),
            None,
        )
        if clash:
            skipped.append((c.keyword, clash))
        else:
            kept.append(c)
    return kept, skipped


def record(
    entries: list[dict],
    keyword: str,
    title: str,
    post_id: str = "",
    url: str = "",
    status: str = "draft",
    mode: str = "trend",
    cost_usd: float = 0.0,
    review_score: int | None = None,
    extras: list[str] | None = None,
) -> list[dict]:
    """이력에 한 건 추가합니다.

    status: live(공개) | draft(보류) | rejected(올리지 않음)
    mode:   trend(실시간 이슈) | evergreen(장수 해설)
    extras: 적용된 수익화 항목 (예: ["coupang"])
    """
    entries.append(
        {
            "keyword": keyword,
            "title": title,
            "post_id": post_id,
            "url": url,
            "status": status,
            "mode": mode,
            "cost_usd": round(cost_usd, 4),
            "review_score": review_score,
            "extras": extras or [],
            "posted_at": _now().isoformat(timespec="seconds"),
        }
    )
    # 파일이 무한정 커지지 않게 최근 500건만 유지
    return entries[-500:]
