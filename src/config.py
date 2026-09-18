"""설정 로딩. 일반 설정은 config.yaml, 시크릿은 환경변수에서 읽습니다."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"


def load_dotenv(path: Path | None = None) -> None:
    """로컬 실행용 .env 로더. 이미 설정된 환경변수는 덮어쓰지 않습니다.

    GitHub Actions 에서는 Secrets 가 환경변수로 들어오므로 이 파일이 없어도 됩니다.
    """
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: Path | None = None) -> dict[str, Any]:
    load_dotenv()
    cfg_path = path or ROOT / "config.yaml"
    with cfg_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"환경변수 {name} 이(가) 설정되지 않았습니다. "
            f".env 파일이나 GitHub Secrets 를 확인하세요."
        )
    return value
