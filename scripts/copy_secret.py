"""`.env` 값을 화면에 띄우지 않고 클립보드로 복사합니다.

GitHub Secrets 에 등록할 때 쓰세요. 값이 터미널에 남지 않고,
길이가 긴 키를 눈으로 옮겨 적다 틀릴 일도 없습니다.

    python scripts/copy_secret.py                    # 목록에서 골라서 복사
    python scripts/copy_secret.py ANTHROPIC_API_KEY  # 바로 복사
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

CREDENTIAL_VARS = ("BLOGGER_CLIENT_ID", "BLOGGER_CLIENT_SECRET", "BLOGGER_REFRESH_TOKEN")


def secret_names() -> list[str]:
    """GitHub Secrets 에 등록할 이름들 (순서대로). 계정은 fleet/blogs.yaml 의 accounts 에서 읽습니다.

    default 계정은 접미사 없는 표준 이름, 그 외 계정은 _<계정이름대문자> 가 붙습니다 (src/fleet.py env_name).
    """
    import yaml

    names = ["ANTHROPIC_API_KEY"]
    try:
        raw = yaml.safe_load((ROOT / "fleet" / "blogs.yaml").read_text(encoding="utf-8")) or {}
        accounts = list(raw.get("accounts") or {"default": {}})
    except (OSError, yaml.YAMLError):
        accounts = ["default"]
    for acc in accounts:
        names += [base if acc == "default" else f"{base}_{acc.upper()}" for base in CREDENTIAL_VARS]
    return names + ["COUPANG_ACCESS_KEY", "COUPANG_SECRET_KEY"]


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        print(f".env 파일이 없습니다: {ENV_PATH}")
        print("copy .env.example .env 로 만든 뒤 값을 채우세요.")
        sys.exit(1)

    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def to_clipboard(text: str) -> bool:
    if sys.platform == "win32":
        cmd = ["clip"]
    elif sys.platform == "darwin":
        cmd = ["pbcopy"]
    else:
        cmd = ["xclip", "-selection", "clipboard"]

    try:
        subprocess.run(cmd, input=text.encode("utf-8"), check=True, shell=False)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def main() -> int:
    values = read_env()

    if len(sys.argv) > 1:
        name = sys.argv[1].strip()
    else:
        names = secret_names()
        print("GitHub Secrets 에 등록할 항목 (쿠팡은 선택):\n")
        for i, key in enumerate(names, start=1):
            value = values.get(key, "")
            mark = f"{len(value)}자" if value else "비어있음"
            print(f"  {i}. {key}  ({mark})")
        choice = input("\n번호 또는 이름: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            name = names[int(choice) - 1]
        else:
            name = choice

    value = values.get(name)
    if not value:
        print(f"\n'{name}' 값이 .env 에 없거나 비어 있습니다.")
        return 1

    if to_clipboard(value):
        print(f"\n[복사됨] {name} ({len(value)}자)")
        print("GitHub 의 Secret 입력칸에 Ctrl+V 로 붙여넣으세요.")
        print("이름은 위의 대문자 그대로 적어야 합니다.")
    else:
        print("\n클립보드 복사에 실패했습니다. .env 파일을 직접 열어 복사하세요.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
