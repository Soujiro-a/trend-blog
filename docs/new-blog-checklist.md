# 새 블로그 체크리스트

블로그를 하나 만들 때마다 이 순서대로 합니다. 2026-09~10 에 gaganam1·gaganamc1·gaganamissue·gaganams1 을
세팅하면서 실제로 한 일을 모았습니다. **누가** 칸의 뜻은 다음과 같습니다.

- **사람:** 로그인, 계정 선택, 권한 허용, PIN, Blogger 설정·테마 수정처럼 사람만 할 수 있는 일
- **Claude:** 사람이 로그인해 둔 화면에서 Claude 가 이어서 하는 일
- **자동:** 스크립트나 예약 작업이 하는 일

새 **구글 계정**으로 시작한다면 먼저 [README 4장](../README.md#4-블로그계정-늘리기)의 계정 추가 1~7단계
(Cloud 프로젝트, OAuth, 토큰, GitHub Secrets, 워크플로 env)를 끝내고 오세요. 기존 계정에 붙인다면 바로 1단계부터 하면 됩니다.

---

## 1. 함대 등록

| ✓ | 누가 | 할 일 |
|---|---|---|
| ☐ | 사람 | blogger.com 에서 블로그 생성. 한 계정에서 여러 개를 한꺼번에 만들지 말고 **1주 이상 간격**을 둡니다(2026-09-19 차단 사고) |
| ☐ | 자동 | `python scripts/fleet_cli.py discover --account <계정> --add` → 슬롯 자동 배정 |
| ☐ | Claude | `fleet/blogs.yaml` 에 `subject`·`pillars`·`audience`·`persona`·`labels`·`since` 채우기. 다른 블로그와 주제가 겹치면 안 됩니다 |
| ☐ | 자동 | `python scripts/fleet_cli.py validate` → topic-planner 로 글감이 주제 안에서 나오는지 미리보기 |
| ☐ | 자동 | `python scripts/setup_pages.py --blog <id>` (소개·개인정보처리방침 페이지) |
| ☐ | Claude | 커밋·푸시. 다음 슬롯부터 글이 올라갑니다 |

## 2. Blogger 설정 (사람)

설정 변경은 자동 실행 안전 검사가 막는 경우가 있어(2026-10-03), 사람이 직접 하는 편이 확실합니다.

| ✓ | 누가 | 할 일 |
|---|---|---|
| ☐ | 사람 | 설정 → 기본 → **블로그 설명** (주제를 구체적으로 한두 문장) |
| ☐ | 사람 | 설정 → 메타 태그 → **검색 설명 사용** 켜고 첫 화면 검색 설명 입력. 꺼져 있으면 글마다 넣는 검색 설명(5단계)도 나가지 않습니다 |
| ☐ | 사람 | 설정 → 서식 → **시간대 서울** |
| ☐ | 사람 | 레이아웃 → 첫 화면에 소개·개인정보처리방침으로 가는 메뉴(페이지 가젯). `python scripts/adsense_check.py --blog <id>` 가 빠진 것을 알려 줍니다 |

## 3. Google

| ✓ | 누가 | 할 일 |
|---|---|---|
| ☐ | 사람 | [Search Console](https://search.google.com/search-console) 에 **그 블로그를 가진 구글 계정으로** 속성 추가 (지금 블로그들은 도메인 속성 `sc-domain:<id>.blogspot.com`) |
| ☐ | 사람 | Sitemaps 에 `https://<id>.blogspot.com/sitemap.xml` 제출. 빠지면 주간 보고가 경고합니다 |
| ☐ | 자동 | 색인 요청: 예약 작업 `gsc-index-requests` 가 매시 50분(검색 설명을 먼저 넣은 뒤)(07:50~22:50) `index_queue.py next --fresh` 로 새 글을 올라온 지 1시간 안에 요청합니다(구글 계정마다 하루 약 10건. 밀린 주소는 하루 한 번 3건). 새 블로그는 대기열에 자동으로 들어갑니다. 요청 주소는 `?m=1` 모바일 주소입니다(원래 주소는 REDIRECT_ERROR) |
| ☐ | 사람 | **새 구글 계정의 블로그라면:** 내장 브라우저에 그 계정을 **추가로** 로그인합니다(기존 계정은 로그아웃하지 않음) |
| ☐ | Claude | `fleet/blogs.yaml` 의 `accounts.<계정>.browser: {user: <번호>, name: "<화면 이름>"}` 에 그 계정의 `/u/<번호>/` 와 이름을 적습니다. 예약 작업이 이 번호로 계정별 화면을 열고, 할당량도 계정마다 따로 셉니다 |

## 4. 네이버 · 다음 · Bing

### 네이버 서치어드바이저 (사람)

네이버 사이트는 Claude 가 쓰는 브라우저(내장·Chrome 확장)에서 모두 막혀 있어 사람이 해야 합니다.

| ✓ | 누가 | 할 일 |
|---|---|---|
| ☐ | 사람 | 웹마스터 도구 → 사이트 추가 `https://<id>.blogspot.com` |
| ☐ | 사람 | 소유확인 → **HTML 태그** → `<meta name="naver-site-verification" …>` 를 테마 HTML 의 `<head>` 바로 아래에 넣고 저장 → 소유확인 |
| ☐ | 사람 | 요청 → 사이트맵 제출 `https://<id>.blogspot.com/sitemap.xml` |
| ☐ | 사람 | 요청 → RSS 제출 `https://<id>.blogspot.com/feeds/posts/default?alt=rss` |

### 다음 웹마스터도구

| ✓ | 누가 | 할 일 |
|---|---|---|
| ☐ | 사람 | [webmaster.daum.net](https://webmaster.daum.net) 에서 블로그 주소로 **PIN코드 발급**. 비밀번호와 같으니 Claude 에게 PIN 은 알려 주지 마세요 |
| ☐ | 사람 | 발급 화면의 `#DaumWebMasterTool:…` 한 줄을 Blogger 설정 → 크롤러 및 색인 생성 → **맞춤 robots.txt 사용** 켜고 맨 위에 넣기. 아래 기본 규칙은 그대로 둡니다 (아래 템플릿) |
| ☐ | 사람 | 다음 웹마스터도구에 **블로그 주소 + PIN 으로 로그인** → Claude 에게 "했다"고 알림 |
| ☐ | Claude | 수집요청(`/tool/collect`) → **수집 Seed URL 등록(사이트맵)** 에 `https://<id>.blogspot.com/sitemap.xml` |
| ☐ | Claude | 같은 화면 → **수집 Seed URL 등록(RSS 피드)** 에 `https://<id>.blogspot.com/feeds/posts/default?alt=rss` |
| ☐ | Claude | 로그아웃(`/j_spring_security_logout`). 다음 블로그가 있으면 로그인 화면을 띄워 둠 |

Enter 로 제출하면 요청이 두 번 가서 "수집요청이 완료되었습니다"와 "중복 URL" 안내가 같이 뜹니다. 첫 요청이 접수된 것이라 괜찮습니다.

맞춤 robots.txt 템플릿 (`<id>` 와 인증 줄만 바꿉니다):

```
#DaumWebMasterTool:<발급 화면의 값>
User-agent: Mediapartners-Google
Disallow: 

User-agent: *
Disallow: /search
Disallow: /share-widget
Allow: /

Sitemap: https://<id>.blogspot.com/sitemap.xml
```

### Bing 웹마스터

| ✓ | 누가 | 할 일 |
|---|---|---|
| ☐ | 사람 | [Bing 웹마스터](https://www.bing.com/webmasters) 에 Microsoft 계정으로 로그인 |
| ☐ | Claude | 사이트 추가 → **Import your sites from GSC** → Import → Continue 직전까지 |
| ☐ | 사람 | Continue → **그 블로그를 가진 구글 계정** 선택 → 보기 권한 허용 |
| ☐ | Claude | 목록에서 **새 블로그만** 체크(전체 선택 해제) → Import. "Site addition unsuccessful" 이 떠도 실제로는 추가되는 경우가 있으니 사이트 목록에서 확인 |
| ☐ | Claude | 블로그마다 Sitemaps → Submit sitemap `https://<id>.blogspot.com/sitemap.xml` → "successfully submitted" 확인 |

## 5. 글 단위 (자동)

| ✓ | 누가 | 할 일 |
|---|---|---|
| ☐ | 자동 | 글별 검색 설명: 매시간 예약 작업 `gsc-index-requests`(07:50~22:50)가 색인 요청 **전에** `search_desc.py next --fresh` 로 최근 72시간 새 글의 검색 설명을 글 편집 화면에 넣습니다. 새 블로그 글도 자동으로 들어갑니다. 진행은 `python scripts/search_desc.py status`. 밀린 글이 생기면 꺼 둔 하루 작업 `blogger-search-descriptions`(12편씩)를 수동 실행 |
| ☐ | 사람 | **새 구글 계정의 블로그라면:** 위 3장의 `browser` 설정과 같습니다. 그 계정이 내장 브라우저에 로그인돼 있어야 Blogger 편집 화면이 열립니다 |

## 6. 마지막 확인

```bash
python scripts/registration_check.py --blog <id>
```

이 스크립트는 Search Console 속성·사이트맵, 네이버 메타태그, 다음 robots.txt 인증 줄, robots.txt 기본 규칙, 첫 화면 검색 설명을 확인합니다.
네이버·다음·Bing 에 사이트맵을 냈는지는 로그인해야 보여서, 위 표의 ✓ 로 확인합니다.

## 7. 나중에

| 언제 | 누가 | 할 일 |
|---|---|---|
| 2~3주 뒤 | Claude | 주간 보고·Search Console 검색어로 노출되는 하위 주제 확인 → 잘 되는 쪽으로 기획 조정 |
| 공개 글 25개↑, 운영 28일↑ | 사람 | `python scripts/adsense_check.py --blog <id>` 전부 ✅ 이면 Blogger 수익 메뉴에서 애드센스 신청 |
| 4주 뒤 | 자동 | 램프업이 두 번째 슬롯을 켬 (사람이 당기지 않습니다) |
