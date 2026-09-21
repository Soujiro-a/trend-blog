"""Blogger refresh token 발급 도우미 (최초 1회만 실행).

미리 준비할 것
--------------
1. https://console.cloud.google.com 에서 프로젝트 생성
2. 'API 및 서비스 > 라이브러리' 에서 **Blogger API v3** 사용 설정
3. 'API 및 서비스 > OAuth 동의 화면' 설정 (User Type: 외부, 테스트 사용자에 본인 계정 추가)
4. '사용자 인증 정보 > 사용자 인증 정보 만들기 > OAuth 클라이언트 ID'
   - 애플리케이션 유형: **데스크톱 앱** ← 반드시 이걸로 만드세요
   - 만들어진 클라이언트 ID / 보안 비밀번호를 준비

실행
----
    python scripts/get_blogger_token.py

브라우저가 열리면 블로그 소유 계정으로 로그인하고 권한을 허용하세요.
마지막에 출력되는 값 4개를 GitHub Secrets 에 넣으면 됩니다.

`400 오류: redirect_uri_mismatch` 가 나온다면
------------------------------------------
클라이언트를 '웹 애플리케이션' 유형으로 만든 경우입니다. 구글은 임의 포트로 돌아오는
주소(http://localhost:임의번호)를 **데스크톱 앱 유형에만** 허용합니다.

해결책은 둘 중 하나입니다.

  (A) 권장 — '데스크톱 앱' 유형으로 클라이언트를 새로 만들고 그대로 다시 실행
  (B) 지금 만든 웹 클라이언트를 그대로 쓰려면, 해당 클라이언트의
      '승인된 리디렉션 URI' 에 http://localhost:8080 을 추가한 뒤:

          python scripts/get_blogger_token.py --port 8080
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _update_env(path: Path, updates: dict[str, str]) -> None:
    """키가 있으면 그 줄만 바꾸고, 없으면 끝에 추가합니다. 주석·다른 값은 그대로 둡니다."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/blogger"
# --full 옵션: 주간 보고서가 검색 유입과 광고 수익까지 읽을 수 있게 읽기 권한을 함께 받습니다.
# Google Cloud 프로젝트에 'Google Search Console API' 와 'AdSense Management API' 도
# 사용 설정돼 있어야 합니다 (라이브러리에서 검색 → 사용).
EXTRA_SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/adsense.readonly",
]
BLOGS_URL = "https://www.googleapis.com/blogger/v3/users/self/blogs"

_result: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)

        if "code" in params:
            _result["code"] = params["code"][0]
            _result["state"] = params.get("state", [""])[0]
            message = "인증이 끝났습니다. 이 창을 닫고 터미널로 돌아가세요."
        else:
            _result["error"] = params.get("error", ["unknown"])[0]
            message = f"인증 실패: {_result['error']}"

        body = f"<html><meta charset='utf-8'><body style='font-family:sans-serif;padding:3rem'><h2>{message}</h2></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # 서버 로그 끄기
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url: str, data: dict) -> dict:
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    parser = argparse.ArgumentParser(description="Blogger refresh token 발급")
    parser.add_argument(
        "--port",
        type=int,
        help="콜백 받을 고정 포트. '웹 애플리케이션' 유형 클라이언트를 쓸 때, "
        "여기 적은 포트로 http://localhost:<포트> 를 승인된 리디렉션 URI 에 "
        "먼저 등록해야 합니다. 생략하면 빈 포트를 자동으로 고릅니다"
        "('데스크톱 앱' 유형에서만 동작).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Search Console·애드센스 읽기 권한도 함께 받습니다 (주간 보고서에 유입·수익 표시). "
        "Google Cloud 에서 두 API 를 먼저 사용 설정하세요.",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="CLIENT ID/SECRET 을 묻지 않고 .env 의 BLOGGER_CLIENT_ID / BLOGGER_CLIENT_SECRET 을 씁니다.",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=300,
        help="브라우저 승인을 기다릴 최대 초 (기본 300)",
    )
    parser.add_argument(
        "--account",
        default="default",
        help="이 자격증명을 어느 함대 계정에 쓸지. default 가 아니면 "
        "BLOGGER_CLIENT_ID_<대문자> 처럼 계정별 변수명으로 읽고 씁니다.",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="발급된 refresh token 을 화면에 찍지 않고 .env 의 BLOGGER_REFRESH_TOKEN 에 바로 저장합니다.",
    )
    args = parser.parse_args()
    scope = " ".join([SCOPE, *EXTRA_SCOPES]) if args.full else SCOPE
    env_path = Path(__file__).resolve().parent.parent / ".env"

    def var(base: str) -> str:
        """계정별 환경변수 이름. default 계정은 표준 이름 그대로."""
        return base if args.account == "default" else f"{base}_{args.account.upper()}"

    print("─" * 60)
    print("  시작 전 확인 — OAuth 앱이 '프로덕션'으로 게시돼 있어야 합니다.")
    print()
    print("  게시 상태가 '테스트'면 여기서 받은 토큰이 7일 뒤 만료됩니다.")
    print("  그러면 매주 이 작업을 다시 해야 합니다.")
    print()
    print("  Google Cloud > Google 인증 플랫폼 > 대상(Audience) > [앱 게시]")
    print("─" * 60)
    print()

    if args.account != "default":
        print(f"대상 함대 계정: {args.account}  (변수 {var('BLOGGER_REFRESH_TOKEN')})\n")

    if args.from_env:
        current = _read_env(env_path)
        client_id = current.get(var("BLOGGER_CLIENT_ID"), "")
        client_secret = current.get(var("BLOGGER_CLIENT_SECRET"), "")
        if not client_id:
            print(f".env 에 {var('BLOGGER_CLIENT_ID')} 가 없습니다. 먼저 채우거나 --from-env 없이 실행하세요.")
            return 1
        print(f".env 의 클라이언트 정보 사용 (ID {client_id[:12]}…)\n")
    else:
        print("Google Cloud Console 에서 만든 OAuth 클라이언트 정보를 입력하세요.\n")
        client_id = input("  CLIENT ID     : ").strip()
        client_secret = input("  CLIENT SECRET : ").strip()
    if not client_id or not client_secret:
        print("\n두 값 모두 필요합니다.")
        return 1

    port = args.port or _free_port()
    redirect_uri = f"http://localhost:{port}"
    if args.port:
        print(f"\n고정 포트 {port} 사용. 구글 클라이언트의 '승인된 리디렉션 URI' 에")
        print(f"  {redirect_uri}")
        print("가 등록돼 있어야 합니다.")
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",   # refresh token 을 받기 위해 필수
        "prompt": "consent",        # 이미 승인했어도 refresh token 을 다시 받기 위해
        "state": state,
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        print(f"\n포트 {port} 를 열 수 없습니다: {exc}")
        print("다른 프로그램이 쓰고 있다면 --port 로 다른 번호를 지정하세요")
        print("(그 번호도 구글 클라이언트의 리디렉션 URI 에 등록해야 합니다).")
        return 1
    threading.Thread(target=server.handle_request, daemon=True).start()

    print(f"\n브라우저를 엽니다. 안 열리면 아래 주소를 직접 붙여넣으세요:\n\n{auth_url}\n")
    webbrowser.open(auth_url)
    print("승인을 기다리는 중...")

    server_thread_timeout = max(30, args.wait)
    for _ in range(server_thread_timeout * 2):
        if _result:
            break
        threading.Event().wait(0.5)

    if "code" not in _result:
        print(f"\n인증을 받지 못했습니다: {_result.get('error', '시간 초과')}")
        return 1
    if _result.get("state") != state:
        print("\nstate 값이 일치하지 않습니다. 중단합니다.")
        return 1

    print("승인 확인. 토큰을 교환합니다...")
    try:
        tokens = _post(
            TOKEN_URL,
            {
                "code": _result["code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    except urllib.error.HTTPError as exc:
        print(f"\n토큰 교환 실패: {exc.read().decode('utf-8', 'replace')[:400]}")
        return 1

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print(
            "\nrefresh_token 이 오지 않았습니다.\n"
            "https://myaccount.google.com/permissions 에서 이 앱의 권한을 삭제한 뒤 "
            "다시 실행해 보세요."
        )
        return 1

    print("\n블로그 목록을 불러옵니다...")
    try:
        blogs = _get(BLOGS_URL, tokens["access_token"]).get("items", [])
    except urllib.error.HTTPError as exc:
        print(f"블로그 목록 조회 실패: {exc}")
        blogs = []

    blog_id = ""
    if not blogs:
        print("  이 계정에 블로그가 없습니다. https://blogger.com 에서 먼저 만들고 다시 실행하세요.")
    elif args.account != "default":
        # 함대 계정은 블로그를 account_cli.py import 로 한꺼번에 등록하므로 하나를 고를 필요가 없습니다.
        print(f"  이 계정의 블로그 {len(blogs)}개:")
        for b in blogs:
            print(f"    - {b['name']}  {b['url']}")
        print("  → 등록은 다음 단계에서: python scripts/account_cli.py import " + args.account)
    elif len(blogs) == 1:
        blog_id = blogs[0]["id"]
        print(f"  블로그 1개 발견: {blogs[0]['name']} ({blogs[0]['url']})")
    else:
        print("  여러 개의 블로그가 있습니다:")
        for i, b in enumerate(blogs, 1):
            print(f"    {i}. {b['name']}  {b['url']}  (id={b['id']})")
        choice = input("  사용할 번호: ").strip()
        try:
            blog_id = blogs[int(choice) - 1]["id"]
        except (ValueError, IndexError):
            print("  잘못된 선택입니다. blog id 는 직접 넣으세요.")

    if args.write_env:
        updates = {
            var("BLOGGER_CLIENT_ID"): client_id,
            var("BLOGGER_CLIENT_SECRET"): client_secret,
            var("BLOGGER_REFRESH_TOKEN"): refresh_token,
        }
        if args.account == "default" and blog_id and not _read_env(env_path).get("BLOGGER_BLOG_ID"):
            updates["BLOGGER_BLOG_ID"] = blog_id
        _update_env(env_path, updates)
        print("\n" + "=" * 64)
        print(f".env 에 저장했습니다: {var('BLOGGER_REFRESH_TOKEN')} 갱신 "
              f"(권한: {'전체' if args.full else 'Blogger 만'})")
        print("GitHub Secrets 도 같은 값으로 바꾸려면:")
        print(f"  python scripts/copy_secret.py {var('BLOGGER_REFRESH_TOKEN')}")
        if args.account != "default":
            print(f"  (Secrets 이름도 {var('BLOGGER_CLIENT_ID')} / {var('BLOGGER_CLIENT_SECRET')} / "
                  f"{var('BLOGGER_REFRESH_TOKEN')} 로 등록하세요)")
        print("=" * 64)
        return 0

    print("\n" + "=" * 64)
    print("아래 값들을 GitHub 저장소의 Settings > Secrets and variables > Actions")
    print("에 각각 'New repository secret' 으로 등록하세요.")
    print("=" * 64 + "\n")
    print(f"{var('BLOGGER_CLIENT_ID')}\n{client_id}\n")
    print(f"{var('BLOGGER_CLIENT_SECRET')}\n{client_secret}\n")
    print(f"{var('BLOGGER_REFRESH_TOKEN')}\n{refresh_token}\n")
    if args.account == "default":
        print(f"BLOGGER_BLOG_ID\n{blog_id or '(직접 확인 필요)'}\n")
    print("=" * 64)
    print("이 값들은 비밀번호와 같습니다. 채팅창이나 공개 저장소에 올리지 마세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
