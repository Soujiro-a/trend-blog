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

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/blogger"
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
    args = parser.parse_args()

    print("─" * 60)
    print("  시작 전 확인 — OAuth 앱이 '프로덕션'으로 게시돼 있어야 합니다.")
    print()
    print("  게시 상태가 '테스트'면 여기서 받은 토큰이 7일 뒤 만료됩니다.")
    print("  그러면 매주 이 작업을 다시 해야 합니다.")
    print()
    print("  Google Cloud > Google 인증 플랫폼 > 대상(Audience) > [앱 게시]")
    print("─" * 60)
    print()

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
        "scope": SCOPE,
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

    server_thread_timeout = 300
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

    print("\n" + "=" * 64)
    print("아래 4개를 GitHub 저장소의 Settings > Secrets and variables > Actions")
    print("에 각각 'New repository secret' 으로 등록하세요.")
    print("=" * 64 + "\n")
    print(f"BLOGGER_CLIENT_ID\n{client_id}\n")
    print(f"BLOGGER_CLIENT_SECRET\n{client_secret}\n")
    print(f"BLOGGER_REFRESH_TOKEN\n{refresh_token}\n")
    print(f"BLOGGER_BLOG_ID\n{blog_id or '(직접 확인 필요)'}\n")
    print("=" * 64)
    print("이 값들은 비밀번호와 같습니다. 채팅창이나 공개 저장소에 올리지 마세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
