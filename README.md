# 실시간 이슈 블로그 자동화

실시간 검색어를 모아 글을 쓰고, **AI 가 검수한 뒤 스스로 공개**합니다.
사람은 매일 볼 것이 없고, 월요일 아침에 오는 **주간 보고 메일 한 통**만 읽으면 됩니다.

```
 실시간 검색어 5개 소스        필터            Claude Opus       Claude Sonnet         Blogger
 ┌────────────────┐     ┌───────────┐     ┌──────────┐     ┌──────────────┐     ┌──────────┐
 │ 구글 트렌드     │     │ 14일 중복  │     │ 리서치    │     │ 사실 대조     │ 통과 │ 공개      │──▶ 애드센스
 │ 시그널          │─합산▶│ 민감 주제  │─상위▶│ 글 작성   │────▶│ 법적 위험     │────▶│ (하루 ≤3) │    (트래픽 광고)
 │ 네이트          │ 점수 │ 기사 부족  │ 4개 │ 제목/태그 │     │ 광고 정책     │     ├──────────┤
 │ 네이버 뉴스     │     └───────────┘     └──────────┘     │ 구매 의도 판단 │ 보류 │ 임시저장  │
 │ 구글 뉴스       │                                        └──────┬───────┘     └──────────┘
 └────────────────┘                                               │ 구매 의도 있음
                                                                   ▼
                                                            쿠팡파트너스 상품 링크 삽입 (제휴 수수료)

 매일 04:30 KST   실시간 이슈 글 4개 작성 → 검수 → 공개          (.github/workflows/draft.yml)
 매주 월 05:40    장수 해설 글 2개 작성 → 검수 → 공개            (.github/workflows/evergreen.yml)
 매주 월 09:20    주간 운영·수익 보고서 → GitHub 이슈 → 메일     (.github/workflows/weekly_report.yml)
```

**수익 구조**는 두 갈래입니다.

| 수익원 | 방식 | 코드에서 하는 일 | 사람이 1회 해야 하는 일 |
|---|---|---|---|
| **구글 애드센스** | 방문자에게 광고 노출 | 글을 꾸준히 공개해 트래픽을 만듦 | 공개 글 20~30개 쌓이면 Blogger → 수익 메뉴에서 신청 (5분) |
| **쿠팡파트너스** | 글 속 상품 링크로 구매 시 수수료 | 검수관이 '구매 의도 있는 주제'로 본 글에만 상품 카드 삽입 | [partners.coupang.com](https://partners.coupang.com) 가입 → API 키 발급 → Secrets 등록 |

돈이 되는 정도는 트래픽에 달려 있고, 이 시스템은 트래픽을 **만들어 볼 기회를 매일 자동으로 만드는 것**까지입니다.
보통 실시간 이슈 블로그는 3~6개월 지나야 애드센스 승인과 유의미한 유입이 시작됩니다. 그 전에는 API 비용(월 $15 안팎)만 나갑니다.

---

## 1. 준비 (최초 1회, 30~40분)

> 이미 세팅이 끝난 저장소라면 이 절은 건너뛰고 [2. 남은 사람 일](#2-남은-사람-일)로 가세요.

### 1단계 — Blogger 블로그 만들기 (5분, 무료)

[blogger.com](https://www.blogger.com) 에서 구글 계정으로 블로그를 만듭니다.
주소는 `원하는이름.blogspot.com` 이 됩니다. 나중에 개인 도메인으로 바꿔도
주소가 자동 연결되니 지금은 아무 이름이나 괜찮습니다.

### 2단계 — Blogger API 권한 받기 (15분)

[Google Cloud Console](https://console.cloud.google.com) 에서:

1. 새 프로젝트 생성
2. **API 및 서비스 → 라이브러리** → "Blogger API v3" 검색 → **사용 설정**
   - 주간 보고서에 검색 유입·광고 수익까지 표시하려면 **Google Search Console API** 와
     **AdSense Management API** 도 같이 사용 설정하세요 (선택).
3. **OAuth 동의 화면**(최신 콘솔에서는 *Google 인증 플랫폼*) → User Type **외부** → 앱 이름 아무거나 → 저장
   → 그다음 **대상(Audience)** 페이지에서 **앱 게시 → 프로덕션으로 전환**

   게시하려면 **브랜딩** 페이지에 아래가 모두 채워져 있어야 합니다.
   앱 이름 / 사용자 지원 이메일 / 개발자 연락처 이메일 /
   **애플리케이션 홈페이지 URL** / **개인정보처리방침 URL**

   뒤의 두 URL 은 블로그에 페이지를 만들어 쓰면 됩니다. 붙여넣기용 템플릿을
   [`pages/`](pages) 에 넣어뒀습니다 (`about.html`, `privacy-policy.html`).
   개인정보처리방침은 **애드센스 심사에도 어차피 필수**라 미리 만들어 두면 두 번 일하지 않습니다.

   > **이 단계를 건너뛰지 마세요.** 게시 상태가 '테스트'인 동안 발급된 refresh token 은
   > **7일 뒤 만료**됩니다([구글 문서](https://developers.google.com/identity/protocols/oauth2)).
   > 그러면 매주 인증을 다시 해야 해서 자동화가 무너집니다. 프로덕션으로 바꾸면 만료되지 않습니다.
   >
   > 게시해도 구글 심사를 받는 게 아니라서 로그인할 때 '확인되지 않은 앱' 경고는 계속 뜹니다.
   > 본인만 쓰는 용도라 그대로 진행하면 됩니다(미인증 앱은 사용자 100명까지 허용).
4. **사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID**
   → 애플리케이션 유형 **데스크톱 앱** → 만들기
   > 기본값인 '웹 애플리케이션'으로 만들면 인증 단계에서 `400 오류: redirect_uri_mismatch`
   > 가 납니다. 구글은 임의 포트로 돌아오는 주소를 **데스크톱 앱 유형에만** 허용합니다.
   > 이미 웹 유형으로 만드셨다면 [아래](#redirect_uri_mismatch-오류가-난다면) 참고.
5. 나온 **클라이언트 ID** 와 **클라이언트 보안 비밀번호**를 복사

그다음 이 폴더에서:

```bash
pip install -r requirements.txt
python scripts/get_blogger_token.py          # Blogger 권한만
python scripts/get_blogger_token.py --full   # + Search Console·애드센스 읽기 권한 (주간 보고서용, 권장)
```

브라우저가 열리면 블로그 소유 계정으로 승인하세요.
끝나면 **GitHub Secrets 에 넣을 값 4개**가 터미널에 출력됩니다.

> 이 값들은 비밀번호와 같습니다. 채팅창이나 공개 저장소에 붙여넣지 마세요.

#### `redirect_uri_mismatch` 오류가 난다면

클라이언트를 '웹 애플리케이션' 유형으로 만든 경우입니다. 둘 중 하나로 해결합니다.

- **(권장) 데스크톱 앱 유형으로 새로 만들기** — 사용자 인증 정보에서 클라이언트 ID를
  하나 더 만들면 됩니다. OAuth 동의 화면은 다시 설정하지 않아도 됩니다.
- **지금 클라이언트를 그대로 쓰기** — 해당 클라이언트를 열어 **승인된 리디렉션 URI**에
  `http://localhost:8080` 을 추가하고 저장한 뒤 (반영에 1~2분 걸릴 수 있습니다):

  ```bash
  python scripts/get_blogger_token.py --port 8080
  ```

#### "Google에서 확인하지 않은 앱입니다" 경고

본인이 만든 테스트 앱이라 정상입니다. **고급** → **{앱이름}(으)로 이동(안전하지 않음)**.

#### `403 오류: access_denied` / "앱이 Google의 인증 절차를 완료하지 않았습니다"

앱이 아직 **테스트** 상태이고 로그인하려는 계정이 테스트 사용자 목록에 없습니다.
**테스트 사용자를 추가하지 말고, 2단계 3번대로 앱을 프로덕션으로 게시하세요.**
게시한 *뒤에* 토큰 발급 스크립트를 다시 실행해야 만료되지 않는 토큰을 받습니다.

### 3단계 — Claude API 키 발급 (5분)

[console.anthropic.com](https://console.anthropic.com) → **API Keys** → 새 키 생성.
결제 수단을 등록하고 **사용량 한도(Usage limit)를 월 $30 정도로 걸어두세요.**
설정 실수로 비용이 폭주하는 걸 막아줍니다.

### 4단계 — GitHub 저장소에 올리기 (10분)

```bash
git init
git add .
git commit -m "feat: 실시간 이슈 블로그 자동화"
```

GitHub 에서 **비공개(Private)** 저장소를 만들고 푸시합니다.
(비공개여도 Actions 무료 한도 월 2,000분 안에서 충분히 돌아갑니다. 이 작업은 월 40분 정도 씁니다.)

그다음 저장소 **Settings → Secrets and variables → Actions** 에서
`New repository secret` 으로 등록합니다:

| 이름 | 값 | 필수 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 3단계에서 만든 키 | ✅ |
| `BLOGGER_CLIENT_ID` | 2단계 출력값 | ✅ |
| `BLOGGER_CLIENT_SECRET` | 2단계 출력값 | ✅ |
| `BLOGGER_REFRESH_TOKEN` | 2단계 출력값 | ✅ |
| `BLOGGER_BLOG_ID` | 2단계 출력값 | ✅ |
| `COUPANG_ACCESS_KEY` | 쿠팡파트너스 API 키 | 선택 — 없으면 제휴 링크 없이 동작 |
| `COUPANG_SECRET_KEY` | 쿠팡파트너스 API 시크릿 | 선택 |

### 5단계 — 첫 실행 확인

저장소 **Actions** 탭 → `실시간 이슈 글 작성·공개` → **Run workflow** → `count` 에 `1`.
3~4분 뒤 블로그에 글이 하나 공개돼 있으면 (또는 검수에서 보류돼 임시저장함에 있으면) 성공입니다.
각 실행의 **Summary** 에 어떤 키워드를 골랐고, 검수 점수가 몇 점이었고, 왜 보류됐는지가 다 적힙니다.

이후로는 **매일 04:30 KST 에 자동 실행**됩니다. PC는 꺼져 있어도 됩니다.

> **왜 6시가 아니라 4시 30분인가** — GitHub 무료 예약 실행은 지연이 큽니다.
> 실제로 06:00 예약이 **08:02 에 돈 적**이 있습니다. 정시(`0분`)는 전 세계가 몰리는
> 가장 혼잡한 슬롯이라 더 밀립니다. 그래서 정시를 피하고 1시간 30분 여유를 뒀습니다.

---

## 2. 남은 사람 일

자동화가 대신할 수 없는 것은 **계정과 돈에 관한 결정**뿐입니다. 시간순으로:

| 시점 | 할 일 | 소요 |
|---|---|---|
| 지금 | (선택) [쿠팡파트너스](https://partners.coupang.com) 가입 → **링크 생성 → API 키 발급** → GitHub Secrets 에 `COUPANG_ACCESS_KEY`, `COUPANG_SECRET_KEY` 등록 | 15분 |
| 지금 | (선택) `python scripts/get_blogger_token.py --full` 로 토큰 재발급 → `BLOGGER_REFRESH_TOKEN` Secret 교체. 주간 보고서에 검색 유입이 표시됩니다 | 5분 |
| 공개 글 20~30개 (약 1~2주 뒤) | Blogger 관리 화면 → **수익** → 애드센스 신청. 주간 보고서가 "신청 가능" 이라고 알려줍니다 | 5분 + 심사 1~4주 |
| 애드센스 승인 후 | Blogger → 수익 → 광고 표시 켜기. 그러면 템플릿이 광고를 자동 배치합니다 | 2분 |
| 유입이 붙기 시작하면 (선택) | 개인 도메인 구입(연 1.5~2만원) → Blogger 설정 → 맞춤 도메인. 애드센스 승인률과 SEO 에 유리하지만 **트래픽이 확인된 뒤에** 사세요 | 20분 |
| 매주 월요일 | GitHub 이슈로 오는 **주간 보고** 메일 읽기. `✅ 이상 없음` 이면 끝. `⚠️ 확인 필요` 항목이 있으면 그것만 봅니다 | 2분 |

주간 보고가 경고를 띄우는 조건은 `scripts/weekly_report.py` 맨 위에 있습니다
(주간 API 비용 $6 초과 / 공개 5건 미만 / 보류·거부 절반 초과 / 이력 없음 = 실행 중단 의심).

### 그 외에는 정말 안 봐도 되나

- **실행 실패** → GitHub 이 자동으로 메일을 보냅니다.
- **검수에서 보류된 글** → 임시저장함에 쌓입니다. 봐도 되고 안 봐도 됩니다. 15개 넘으면 주간 보고가 알려주니 그때 한 번에 지우면 됩니다.
- **색인** → Blogger 는 구글 서비스라 새 글이 자동으로 구글에 전달됩니다. 따로 요청할 필요 없습니다.
- **60일 무활동 자동 중지** → 매 실행마다 이력을 커밋하므로 해당 없습니다.
- **refresh token 만료** → OAuth 앱이 프로덕션 상태면 만료되지 않습니다. 만약 실패 메일에 `액세스 토큰 발급 실패` 가 보이면 [7. 문제가 생기면](#7-문제가-생기면).

---

## 2-1. 블로그 함대 — 같은 로직으로 블로그 100개까지

한 저장소에서 여러 Blogger 블로그를 **같은 파이프라인, 다른 시각**에 운영합니다. 목록은
[`fleet/blogs.yaml`](fleet/blogs.yaml) 하나에 있고, 블로그마다 이력·보고서가 `data/blogs/<id>/` 에 따로 쌓입니다.

```
 fleet.yml (10분마다)  ─▶  src/fleet_run.py  ─▶  "슬롯이 지났고 오늘 아직 안 돈" 블로그를 슬롯 순서대로
                                                    └▶ src/main.py --blog <id>   (수집 → 작성 → 검수 → 공개)
 manager.yml (매일 23:35) ─▶ src/manager.py   ─▶  블로그별 지표 → Sonnet 5 판단 → 중지/재개/글 수 조정 → 이슈
```

| 하고 싶은 일 | 명령 |
|---|---|
| 블로그 추가 (슬롯 자동 배정) | `python scripts/fleet_cli.py add --name "이름" --blog-id <Blogger ID> --niche "경제·재테크" --persona "..."` |
| 계정의 블로그 전부 등록 | `python scripts/fleet_cli.py discover --add` |
| 슬롯표 / 상태 | `python scripts/fleet_cli.py list` · `status` |
| 켜기/끄기 | `python scripts/fleet_cli.py enable <id>` · `disable <id>` |
| 지금 돌 차례 확인 | `python -m src.fleet_run --dry-run` |
| 한 블로그 테스트 | `python -m src.fleet_run --blog <id> --target local --count 1` |
| 관리 에이전트 판단 미리 보기 | `python -m src.manager --dry-run` |

Claude Code 안에서는 **`fleet-manager` 에이전트**([.claude/agents/fleet-manager.md](.claude/agents/fleet-manager.md))에게
"블로그 5개 추가해", "함대 상태 봐줘", "어느 블로그가 안 돌아?" 처럼 말하면 위 명령을 대신 실행하고 판단해 줍니다.

### 시간 배정
첫 슬롯 04:30 KST, 10분 간격으로 자동 배정됩니다(100개면 04:30 ~ 21:00). 실행기는 10분마다 깨어나
"슬롯이 지났는데 오늘 아직 안 돈" 블로그를 **슬롯 순서대로 하나씩** 돌립니다. GitHub 예약이 밀려도
(자주 밀립니다) 그날 안에는 반드시 처리되고, 밀린 블로그들도 각각 3~4분씩 순서대로 돌기 때문에 블로그 간
간격은 유지됩니다.

### 블로그가 많아질 때 꼭 알아야 할 세 가지

1. **같은 글을 100번 찍으면 안 됩니다.** 기본값 `share_topics: false` 는 같은 날 다른 블로그가 쓴 키워드(유사
   포함)를 건너뜁니다(`data/fleet/claims.json`). 그리고 블로그마다 `niche` / `persona` / `include_patterns` 를
   **다르게** 채우세요 — 경제 블로그, 스포츠 블로그, 연예 블로그처럼 나누는 것이 구글 '대량 생성 콘텍츠'
   판정을 피하는 가장 확실한 방법입니다. 관리 에이전트가 비슷한 제목이 여러 블로그에서 나오면 경고합니다.
2. **비용은 블로그 수에 비례합니다.** 블로그 1개 = 하루 약 $1.1 (Fable 작성 4개 + Sonnet 검수). 100개면
   **월 약 $3,300** 입니다. 블로그별 `overrides: {writer: {model: claude-sonnet-5}}` 로 두면 1/3 로 줍니다.
   Anthropic Console 의 사용량 한도를 함대 규모에 맞게 올려두세요. 한도에 걸리면 그날 이후 블로그는 전부 실패합니다.
3. **GitHub Actions 무료 한도(비공개 저장소 월 2,000분)는 블로그 10개 근처에서 넘습니다.** 블로그당 하루 약
   4분 + 10분마다 도는 실행기 오버헤드 월 약 1,500분. 셋 중 하나를 고르세요:
   - 저장소를 **공개(Public)** 로 전환 — Actions 무제한 (코드·이력만 공개되고 시크릿은 노출되지 않습니다)
   - GitHub 유료 플랜/추가 분 구매
   - PC 에서 돌리기 — `scripts/fleet_local.ps1` 을 Windows 작업 스케줄러에 10분 간격으로 등록 (PC 가 켜져 있어야 함)

### 관리 에이전트가 하는 일
매일 23:35 KST 에 블로그별 7일 지표(공개/보류/거부, 비용, 검수 평균, 연속 실패, Blogger 글 수, Search Console
클릭)를 모아 Sonnet 5 에게 넘기고, **정해진 네 가지 행동**(중지 / 재개 / 하루 글 수 ±1 / 메모) 안에서만 결정을
받습니다. 코드가 규칙(연속 3회 실패 → 중지, 글 수 1~4, 하루 최대 5건 변경, 사람이 끈 블로그는 안 건드림)으로
다시 걸러 `data/fleet/manager_state.json` 에 적용합니다. 조치나 경고가 있으면 GitHub 이슈(`fleet` 라벨)가 열려
메일이 옵니다. 모델 호출이 실패해도 규칙 기반 최소 조치는 적용됩니다.

---

## 3. 왜 이렇게 만들었나 (정책과 위험)

구글은 **"검색 순위만 노린 대량 생산 콘텐츠"** 를 정책 위반(scaled content abuse)으로 봅니다.
AI 로 썼다는 것 자체는 문제가 아니지만, 요약 글을 하루 수십 개씩 올리면 색인 제외나 애드센스 반려로 이어집니다.
그래서 이 시스템은:

- **하루 공개 상한 3개** (`publish.max_live_per_run`). 4개를 써서 검수 점수 순이 아니라 통과 순으로 3개까지만 공개합니다.
- **AI 검수관이 별도로 봅니다** (`src/reviewer.py`). 작성 모델과 다른 세션에서 참고 기사와 본문을 대조해
  지어낸 사실·링크, 사생활·의혹, 낚시 제목, 분량 채우기를 잡습니다. 80점 미만이거나 `hold` 면 공개하지 않습니다.
- **사건사고·사생활·의혹 키워드는 작성 전에 차단**합니다 (`config.yaml` 의 `sensitive_patterns`).
- 글에 **배경·용어·FAQ** 를 넣어 기사에 없는 값을 더하고, 참고 기사를 **출처로 명시**합니다.
- **장수 글**(`--mode evergreen`)을 주 2개 따로 씁니다. 실시간 글은 유입 수명이 1~3일이라
  이것만으로는 광고 수익이 매일 0에서 시작합니다. 해설·안내 글이 바닥을 만듭니다.
- 쿠팡 링크에는 법이 요구하는 **고지 문구**를 항상 붙이고, `rel="sponsored"` 를 씁니다.

그래도 남는 위험은 **검수관도 AI 라는 점**입니다. 틀린 사실이 공개될 확률이 0은 아닙니다.
불안하면 `publish.mode: draft` 로 바꾸면 예전처럼 임시저장까지만 하고 사람이 공개합니다.

---

## 4. 비용

| 항목 | 비용 |
|---|---|
| Blogger / GitHub Actions / 검색어 수집 | 무료 |
| 도메인 | 선택 — 연 1.5~2만원 |
| **Claude API** | **아래 표 참고** |

기본 설정(실시간 4개/일 + 장수 2개/주) 기준. 검수(Sonnet) 비용 포함.

| 작성 모델 | 글 1개 (작성+검수) | 월 예상 |
|---|---|---|
| `claude-opus-5` (기본) | 약 $0.13 | **약 $17** |
| `claude-sonnet-5` | 약 $0.07 | **약 $9** |

비용을 줄이려면 `config.yaml` 에서:

```yaml
run:
  posts_per_run: 3        # 하루 4개 → 3개
writer:
  model: claude-sonnet-5  # 품질을 조금 낮추고 비용 절반
```

---

## 5. 로컬에서 직접 돌려보기

```bash
cp .env.example .env     # 값 채우기 (Windows: copy .env.example .env)

# 로직 테스트 — 네트워크도 API 키도 필요 없음 (몇 초). config.yaml 고친 뒤 먼저 돌려보세요
python scripts/test_logic.py

# Blogger 인증만 확인 (글 안 씀, 비용 0원)
python scripts/check_blogger.py

# 실제 사이트까지 붙여서 파이프라인 전체 점검 (API 키 불필요)
python scripts/selftest.py

# 오늘 어떤 키워드가 뽑히는지만 확인 (비용 0원)
python -m src.main --dry-run

# 글 1개를 써서 검수까지 하고 Blogger 대신 out/ 폴더에 저장 (약 $0.13)
python -m src.main --target local --count 1

# 실제로 Blogger 에 (검수 통과 시) 공개
python -m src.main --count 1

# 검수 결과와 무관하게 임시저장만
python -m src.main --count 1 --draft

# 장수 해설 글
python -m src.main --mode evergreen --count 1

# 주간 보고서 미리 보기
python scripts/weekly_report.py
```

> **로컬에서 돌릴 때는 반드시 `git pull` 을 먼저 하세요.** 예약 실행과 겹치면
> 서로의 작성 이력을 모른 채 같은 주제로 글을 두 번 쓰게 됩니다.

---

## 6. 설정 조정

전부 `config.yaml` 에 있습니다. 자주 만질 만한 것들:

| 설정 | 의미 |
|---|---|
| `run.posts_per_run` | 하루에 쓸 글 개수 |
| `publish.mode` | `auto` 검수 통과 시 공개 / `draft` 전부 임시저장 |
| `publish.max_live_per_run` | 하루 자동 공개 상한. **3을 넘기지 마세요** |
| `review.min_score` | 공개 기준 점수(기본 80). 낮추면 더 많이 공개되고 위험도 올라감 |
| `review.enabled` | `false` 면 검수 없이 바로 공개. 권장하지 않음 |
| `monetize.coupang.enabled` | 쿠팡 상품 링크 삽입 여부 |
| `evergreen.posts_per_run` | 주 1회 장수 글 개수 |
| `trends.sources` | 소스별 가중치. `0` 으로 두면 그 소스를 끕니다 |
| `filters.sensitive_patterns` | 차단 키워드 |
| `dedupe.history_days` | 같은 주제를 다시 안 쓸 기간 (기본 14일) |
| `writer.model` | `claude-opus-5` / `claude-sonnet-5` |

어떤 키워드가 왜 걸러졌고 검수가 왜 보류했는지는 실행마다 `data/last_run.md` 와 Actions Summary 에 적힙니다.

---

## 7. 문제가 생기면

| 증상 | 원인과 해결 |
|---|---|
| `액세스 토큰 발급 실패` | refresh token 만료. OAuth 앱이 **프로덕션으로 게시**돼 있는지 확인(테스트 상태면 7일마다 만료). `python scripts/get_blogger_token.py --full` 재실행 후 Secret 갱신 |
| `환경변수 ... 설정되지 않았습니다` | GitHub Secrets 이름 오타 확인 (대소문자 구분) |
| `키워드 수집 부족` | 소스 사이트가 일시적으로 막힌 경우. 다음 실행에 대개 복구됩니다 |
| 글이 전부 보류됨 | Summary 의 검수 사유 확인. 특정 유형이 반복되면 `writer.py` 의 SYSTEM 프롬프트에 규칙 추가 |
| 글이 하나도 안 써짐 | 필터가 다 걸러낸 경우. Summary 의 '걸러낸 키워드' 확인 |
| 주간 보고에 Search Console 권한 없음 | `get_blogger_token.py --full` 로 재발급. Google Cloud 에 Search Console API 사용 설정 필요 |
| 쿠팡 API 오류 | 키 확인. 쿠팡파트너스는 **최근 실적이 없으면 API 를 막기도** 합니다. 실패해도 글은 정상 공개됩니다 |
| 예약 실행이 멈춤 | 60일 무활동으로 비활성화됨. Actions 탭에서 `Enable workflow` |
| 매번 같은 키워드 | `data/history.json` 이 커밋되는지 확인 (Actions 권한 `contents: write`) |

---

## 구조

```
config.yaml                 설정 (여기만 만지면 됩니다)
src/
  main.py                   전체 흐름: 후보 → 리서치 → 작성 → 검수 → 판정 → 발행
  trends/                   실시간 검색어 수집기 5종
  filters.py                민감 주제·부적합 키워드 차단
  state.py                  작성 이력 / 중복 방지
  research.py               키워드별 참고 기사 수집
  writer.py                 Claude 글 작성 (실시간 / 장수 프롬프트)
  reviewer.py               Claude 검수 — publish / hold / reject + 구매 의도 판단
  evergreen.py              장수 해설 주제 생성
  monetize/
    coupang.py              쿠팡파트너스 상품 검색·카드 삽입
  publishers/
    blogger.py              Blogger 공개/임시저장
    local.py                로컬 HTML 저장 (테스트용)
scripts/
  get_blogger_token.py      최초 1회 인증 (--full: 보고서용 권한 포함)
  weekly_report.py          주간 운영·수익 보고서
  check_blogger.py          Blogger 연결만 확인
  test_logic.py             네트워크 없이 도는 로직 테스트
  selftest.py               실제 사이트까지 붙여서 파이프라인 점검
.github/workflows/
  draft.yml                 매일 04:30 KST 실시간 글
  evergreen.yml             매주 월 05:40 KST 장수 글
  weekly_report.yml         매주 월 09:20 KST 보고서 → 이슈
data/
  history.json              작성 이력 (커밋됨)
  last_run.md               마지막 실행 보고서
```
