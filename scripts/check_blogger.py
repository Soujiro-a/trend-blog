"""Blogger 인증 정보가 실제로 동작하는지 확인합니다 — .env 의 표준 변수(default 계정)와 BLOGGER_BLOG_ID 기준.

글을 쓰지도, 올리지도 않습니다. Claude API 도 호출하지 않으므로 비용이 들지 않습니다.
토큰 갱신 → 블로그 조회까지만 해보고 결과를 알려줍니다.

    python scripts/check_blogger.py

함대의 다른 계정(예: second)은 이 스크립트 대신 `python scripts/account_cli.py check <계정>` 으로 확인합니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import net  # noqa: E402
from src.config import env, load_dotenv  # noqa: E402
from src.publishers.blogger import API_BASE, _access_token  # noqa: E402

REQUIRED = [
    "BLOGGER_CLIENT_ID",
    "BLOGGER_CLIENT_SECRET",
    "BLOGGER_REFRESH_TOKEN",
    "BLOGGER_BLOG_ID",
]


def main() -> int:
    load_dotenv()

    print("== 1. 환경변수 확인 ==")
    missing = [name for name in REQUIRED if not env(name)]
    for name in REQUIRED:
        value = env(name)
        if value:
            # 값 자체는 찍지 않습니다. 길이와 앞 4글자만 보여줍니다.
            print(f"  [OK]   {name} ({len(value)}자, {value[:4]}…)")
        else:
            print(f"  [없음] {name}")
    if missing:
        print(f"\n{len(missing)}개가 비어 있습니다. .env 파일을 확인하세요.")
        print("(.env.example 을 .env 로 복사한 뒤 값을 채우면 됩니다)")
        return 1

    print("\n== 2. 액세스 토큰 갱신 ==")
    try:
        token = _access_token()
    except Exception as exc:
        print(f"  [실패] {exc}")
        return 1
    print(f"  [OK] 토큰 발급됨 ({len(token)}자)")

    print("\n== 3. 블로그 조회 ==")
    blog_id = env("BLOGGER_BLOG_ID")
    try:
        resp = net.get(
            f"{API_BASE}/blogs/{blog_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as exc:
        print(f"  [실패] {exc}")
        print("  BLOGGER_BLOG_ID 가 맞는지, 그 블로그의 소유 계정으로 인증했는지 확인하세요.")
        return 1

    blog = resp.json()
    print(f"  [OK] 이름  : {blog.get('name')}")
    print(f"       주소  : {blog.get('url')}")
    print(f"       글 수 : {blog.get('posts', {}).get('totalItems', 0)}개")

    print("\n" + "=" * 52)
    print("Blogger 연결 정상입니다.")
    print("함대 계정별 확인: python scripts/account_cli.py check <계정>")
    print("글 1건 시험 (Blogger 대신 out/ 에 저장, 이력 안 남김, 약 $0.4):")
    print("  python -m src.fleet_run --blog <블로그id> --target local")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
