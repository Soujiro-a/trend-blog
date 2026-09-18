"""발행 대상 선택."""

from __future__ import annotations

from . import blogger, local

TARGETS = {
    "blogger": blogger,
    "local": local,
}


def get(name: str):
    if name not in TARGETS:
        raise ValueError(f"알 수 없는 발행 대상: {name} (가능: {', '.join(TARGETS)})")
    return TARGETS[name]
