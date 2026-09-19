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


def load(path: Path = HISTORY_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("이력 파일을 읽지 못해 빈 이력으로 시작합니다: %s", exc)
        return []


def save(entries: list[dict], path: Path = HISTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


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
