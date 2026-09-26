# Blogger 블로그 함대 자동화

여러 개의 Blogger 블로그를 **블로그마다 다른 주제로**, GitHub Actions 가 사람 없이 운영합니다.
글감 기획 → 참고 기사 수집 → 글 작성 → AI 검수 → 공개까지 자동이고, 발행량은 블로그·계정 나이에 맞춰 스스로 늘어납니다.
사람은 **월요일 아침 주간 보고 메일**과, 문제가 생겼을 때 오는 **경고 이슈**만 보면 됩니다.

```
GitHub Actions (공개 저장소 · 무료)

 fleet.yml           10분마다 예약 ─▶ src/fleet_run.py  "슬롯 시각이 지났고 오늘 아직 안 돈" 슬롯을 차례로
                                        └▶ src/main.py --blog <id>   슬롯 하나 = 글 한 건
                                             ① 글감 기획  Sonnet 5   블로그 고유 주제(subject·pillars) 안에서
                                             ② 참고 기사  구글 뉴스 검색 (제목·요약·출처만)
                                             ③ 작성      Fable 5.1  블로그별 문체(persona) + 같은 블로그 글 내부 링크
                                             ④ 검수      Sonnet 5   publish / hold / reject
                                             ⑤ 발행      안전장치를 통과하면 공개, 아니면 임시저장
 manager.yml         매일 23:35 KST ─▶ src/manager.py   블로그별 7일 지표 → 중지/재개/메모 → 경고 있으면 이슈
 weekly_report.yml   월 09:20 KST   ─▶ scripts/weekly_report.py  주간 운영·수익 보고 → 이슈(메일)
```

**수익 구조** — 글이 쌓여 검색 유입이 생기면 **구글 애드센스** 광고 수익이 납니다. 검수관이 '구매 의도가 있는 주제'로 본
글에는 **쿠팡파트너스** 상품 링크를 넣을 수 있습니다(키를 등록했을 때만. 지금은 등록 안 됨 → 꺼져 있음).
새 블로그가 애드센스 승인과 의미 있는 유입을 얻기까지는 보통 몇 달이 걸리고, 그동안은 API 비용만 나갑니다.

---

## 지금 운영 중인 함대 (2026-09-26)

| 계정 | 상태 | 블로그 (고유 주제) |
|---|---|---|
| `second` | 운영 중 · 2026-09-21 시작 | 가가남블로그 `gaganam1` (자동차 운전과 차량 관리) · 가가남씨블로그 `gaganamc1` (온라인 쇼핑과 소비자 권리) · 가가남소식통 `gaganamissue` (해외여행 준비와 출입국 절차, 09-25 시작) · 가가남의블로그 `gaganams1` (집안 살림과 가전·생활용품 관리, 09-25 시작) |
| `default` | ⛔ 은퇴 · 비상정지 | 픽토픽 · 이슈캐치 · 발빠른토픽 · 지금이슈 · 이슈픽 — 2026-09-20 Blogger API 쓰기 차단 이후 계정을 바꾸고 멈춤 (이력은 남아 있음) |

발행량은 램프업이 자동으로 올립니다. `second` 계정은 **~10/18 하루 4건**(블로그당 1건) → **10/19~11/15 6건** →
**11/16~ 8건**(블로그당 2건)입니다. 지금 켜진 슬롯은 언제든 이 명령으로 봅니다(괄호 = 램프업 대기).

```bash
python scripts/fleet_cli.py list
```

---

## 사람이 하는 일

| 언제 | 할 일 |
|---|---|
| 매주 월요일 | GitHub 이슈로 오는 **주간 보고** 메일을 읽습니다. `✅ 이상 없음` 이면 끝, `⚠️ 확인이 필요한 항목` 이 있으면 그것만 봅니다. |
| `⛔ 계정 비상정지` 이슈가 오면 | Blogger 가 발행을 403 으로 거부했다는 뜻입니다. 그 계정 블로그는 이미 전부 멈췄습니다. Blogger 에 로그인해 정책 알림을 확인하고, 풀린 뒤에 `python scripts/account_cli.py resume <계정> --yes` → 커밋·푸시. |
| `함대 관리` 이슈가 오면 | 매일 밤 관리 판단에서 조치나 경고가 나온 날만 옵니다. 경고 항목만 확인합니다. 다음 날 볼 것이 없으면 자동으로 닫힙니다. |
| GitHub 에서 실패 메일이 오면 | 대부분 토큰 만료나 일시 장애입니다. [문제가 생기면](#8-문제가-생기면) 표를 보세요. |
| 블로그 운영 4주 · 공개 글 25개 이후 | `python scripts/adsense_check.py` 로 확인 → Blogger 관리 화면 **수익** 에서 애드센스 신청. `second` 계정 블로그는 10월 하순부터 해당합니다. 승인 뒤 광고 표시를 켜면 템플릿이 광고를 자동 배치합니다. |
| 블로그·계정을 늘릴 때 | [4. 블로그·계정 늘리기](#4-블로그계정-늘리기) |
| (선택) | [쿠팡파트너스](https://partners.coupang.com) API 키 → GitHub Secrets `COUPANG_ACCESS_KEY` · `COUPANG_SECRET_KEY` |

그 외에는 보지 않아도 됩니다.

- **검수에서 보류된 글**은 각 블로그의 임시저장함에 쌓입니다. 봐도 되고 안 봐도 됩니다.
- **색인**: Blogger 는 구글 서비스라 새 글이 자동으로 구글에 전달됩니다.
- **60일 무활동 자동 중지**: 실행마다 이력을 커밋하므로 해당 없습니다.
- **refresh token 만료**: OAuth 앱이 '프로덕션' 상태면 만료되지 않습니다.

---

## 1. 동작 방식

### 슬롯 — 슬롯 하나에 글 한 건
블로그마다 하루에 글을 올리는 시각(`slots`)이 있고, 슬롯 하나가 글 한 건입니다. 하루 2건이면 아침·오후처럼 4시간 이상
벌려 둡니다. 함대 전체의 모든 슬롯은 서로 20분 이상 떨어져 있어야 합니다(`validate` 가 검사).

`fleet.yml` 은 10분마다 예약돼 있지만, GitHub 은 부하가 많으면 예약 실행을 건너뜁니다. 2026-09 에는 실제로 **3~5시간에
한 번꼴**로 돌았습니다. 그래서 실행기는 그날 밀린 슬롯을 모두 따라잡고, 같은 계정의 글 사이는 30분 이상 벌립니다.
**슬롯 시각은 '이 시각 이후 첫 실행'** 이라는 뜻이고, 실제 발행은 몇 시간 늦을 수 있습니다. 하루 글 수는 그대로입니다.

슬롯이 실패하면(예: 모델 응답 오류) 그날 한 번 더 시도하고, 두 번째도 실패하면 그날은 넘어갑니다.
Anthropic 사용 한도나 인증 문제는 모든 블로그가 똑같이 실패하므로, 실행을 바로 멈추고 슬롯을 처리했다고 기록하지 않습니다.
한도를 올리면 그날 안에 이어서 돕니다.

### 글감 기획 — 블로그마다 고유 주제
블로그마다 **겹치지 않는 고유 주제**(`subject`)와 하위 축(`pillars`)이 있고, [`src/planner.py`](src/planner.py) 가
그 안에서만 검색될 만한 글감을 8개 뽑습니다. 최근 14일 안에 쓴 글감과 비슷한 것은 버리고, 남은 것부터 씁니다.
실시간 검색어를 쫓지 않으므로 블로그끼리 같은 사건을 쓰는 일이 설계상 없고, 글 수명이 길어 몇 달 뒤에도 검색 유입이 남습니다.
주제가 비었거나 다른 블로그와 비슷하면 `fleet_cli.py validate` 가 막습니다.

```yaml
  - id: gaganam1
    subject: "자동차 운전과 차량 관리"          # 다른 블로그와 겹치면 validate 가 거부
    pillars: [운전면허 취득·갱신과 적성검사, 자동차보험 가입과 사고 처리 절차, ...]
    audience: "차를 사거나 몰면서 생기는 절차·비용을 검색하는 일반 운전자"
    persona: >-                                 # 이 블로그의 문체·관점 (작성 모델에게 전달)
      운전자가 실제로 처리해야 하는 일을 순서대로 알려주는 글입니다. ...
    labels: [자동차생활]                        # 고정 라벨. 글마다 여기에 하위 축 라벨 하나가 붙습니다
```

### 작성과 검수
- **참고 기사**: 글감으로 구글 뉴스를 검색해 제목·요약·출처만 모읍니다(본문을 긁지 않음). 2건 미만이면 그 글감은 건너뜁니다.
- **작성**(Fable 5.1): 참고 자료에 있는 사실만 쓰고, 금액·기한·법 조항은 자료에 그대로 있을 때만 적습니다. 첫 문단부터 답을 주고,
  같은 블로그의 이미 공개된 글이 문맥에 맞으면 내부 링크를 최대 2개 겁니다.
- **검수**(Sonnet 5): 작성과 다른 세션에서 참고 자료와 본문을 대조합니다.
  - `reject` — 사생활·의혹, 자료에 없는 사실이나 지어낸 링크, 비하·선정 표현, 투자·의료·법률의 직접 권유 → 올리지 않음
  - `hold` — 근거 약한 문장, 과장 제목, 분량 채우기, 깨진 HTML, 정치적 편향 등 → 임시저장
  - `publish` 이고 **80점 이상**이어야 공개합니다(`review.min_score`).

### 발행 안전장치 (2026-09-20 사고 이후)
[`src/guard.py`](src/guard.py) 가 매 발행 직전에 **Blogger 서버에 직접** 오늘 몇 건 올렸는지 물어 상한을 계산합니다.
로컬 이력 파일이 깨지거나 같은 날 여러 번 실행돼도 상한을 넘을 수 없고, 서버 조회가 실패하면 **발행하지 않습니다**(fail-closed).

| 장치 | 값 | 무엇을 막나 |
|---|---|---|
| `publish.max_live_per_day` | 2 | 블로그 하나의 하루 공개 수 (서버 기준) |
| `fleet.account_ramp` | 4 → 6 → 8 | 계정 전체의 오늘 상한. 계정 나이 4주마다 한 단계 (구글이 보는 단위는 계정) |
| `max_live_per_day_account` | 기본 6 · `second` 8 | 계정 상한의 최종값. 램프업이 이 값을 넘지 않음 |
| `fleet.blog_ramp` | 1 → 2 | 블로그 나이 4주 전에는 첫 슬롯만. 계정 상한을 넘으면 가장 어린 블로그의 슬롯부터 꺼짐 |
| `fleet.account_gap_minutes` | 30 (+0~10분) | 같은 계정 블로그들이 밀린 슬롯을 연달아 올리는 것 |
| `publish.min_gap_minutes` | 45 | 같은 블로그가 몇 분 사이에 연속 공개 (걸리면 임시저장) |
| `fleet.max_blogs_per_account` | 6 | 한 계정이 막혔을 때 함께 멈추는 블로그 수 |
| 계정 비상정지 | — | Blogger 가 403 을 돌려주면 그 계정 전체를 즉시 멈추고(글 작성 비용도 안 씀) `⛔` 이슈를 엶 |
| 이력 손상 감지 | — | 깨진 이력을 '이력 없음'으로 넘겨 중복 방지가 풀리던 문제 (사고의 직접 원인) |
| 동시 실행 방지 | `concurrency: fleet` | 실행이 겹쳐 이력 파일이 충돌하는 것 (진행 중이면 다음 실행은 대기) |

### 매일 밤 관리 — `src/manager.py`
매일 23:35 KST 에 블로그별 7일 지표(공개/보류/거부, 비용, 검수 평균, 연속 실패, Blogger 글 수, Search Console 클릭)를 모아
Sonnet 5 에게 넘기고, **중지 / 재개 / 메모** 세 가지 행동 안에서만 판단을 받습니다. 코드가 규칙(연속 3회 실패 → 중지,
하루 변경 5건까지, 사람이 끈 블로그는 안 건드림)으로 다시 걸러 `data/fleet/manager_state.json` 에 적용합니다.
모델 호출이 실패해도 규칙 기반 최소 조치는 적용됩니다.
**발행량을 올리는 권한은 없습니다.** 2026-09-25 에 관리 모델이 만 4일 된 블로그를 하루 2건으로 올리려 해서 없앴고, 증량은 램프업만 합니다.

### 주간 보고 — `scripts/weekly_report.py`
월요일 09:20 KST 에 블로그별 이번 주 작성·공개·보류·거부·비용·검수 평균, 켜진 슬롯으로 계산한 기대 글 수,
Blogger 누적 글 수, 검색 유입, 계정별 애드센스 수익을 한 장으로 모아 이슈로 올립니다(지난주 이슈는 자동으로 닫힘).
경고 기준은 블로그마다 켜진 슬롯에 맞춰 잡습니다 — 기대 글 수의 절반도 공개되지 않았거나, 글 1건당 $0.8 를 넘게 썼거나,
보류·거부가 절반을 넘거나, Search Console 에 등록되지 않은 블로그가 있으면 `⚠️` 로 올라옵니다. 꺼진 블로그는 슬롯표에만 나옵니다.

---

## 2. 로컬 명령 모음

```bash
pip install -r requirements.txt
cp .env.example .env        # 값 채우기 (Windows: copy .env.example .env)
```

한국어 Windows 콘솔에서 글자가 깨지면 앞에 `PYTHONUTF8=1` 을 붙이거나(Git Bash) PowerShell 에서 `$env:PYTHONUTF8=1` 을 먼저 실행하세요.

| 하고 싶은 일 | 명령 | 비용 |
|---|---|---|
| 로직 테스트 (설정·코드를 고친 뒤 꼭) | `python scripts/test_logic.py` | 0 · 네트워크 불필요 |
| 슬롯표 · 켜진 슬롯 | `python scripts/fleet_cli.py list` | 0 |
| 블로그별 오늘 완료 / 7일 성과 / 마지막 결과 | `python scripts/fleet_cli.py status` | 0 |
| 설정 검사 (주제 중복·슬롯 간격·계정당 블로그 수) | `python scripts/fleet_cli.py validate` | 0 |
| 지금 돌 차례인 슬롯 | `python -m src.fleet_run --dry-run` | 0 |
| 오늘 밤 관리 판단과 같은 지표·경고 | `python -m src.manager --dry-run --no-model` | 0 |
| 계정별 자격증명·상한 현황 / 토큰 확인 | `python scripts/account_cli.py list` · `check <계정>` | 0 |
| 블로그 켜기/끄기 (사람 조치로 기록) | `python scripts/fleet_cli.py enable <id>` · `disable <id>` → 커밋·푸시 | 0 |
| 주간 보고 미리 보기 | `python scripts/weekly_report.py` | 0 |
| 애드센스 준비 점검 | `python scripts/adsense_check.py [--blog <id>]` | 0 |
| 자동화가 모델에 보내는 요청 원문 보기 | `python scripts/agent_brief.py planner --blog <id>` | 0 |
| **글 1건 시험** — Blogger 대신 `out/` 에 HTML 저장 | `python -m src.fleet_run --blog <id> --target local` | 약 $0.4 |

`--target local` 시험은 이력·실행 기록·선점(`data/`)을 남기지 않아 실제 슬롯과 중복 방지에 영향이 없습니다.
**실제 발행은 GitHub Actions 에만 맡기세요.** 로컬에서 `--target local` 없이 돌리면 진짜로 글이 올라가고, 같은 날 Actions 가
기록을 모른 채 또 돌 수 있습니다. `enable`/`disable` 처럼 `data/` 를 바꾸는 명령은 먼저 `git pull` 하고, 끝나면 커밋·푸시합니다.

GitHub 에서 직접 돌리려면 Actions 탭 → **블로그 함대 실행** → Run workflow (`blog` 에 id 를 넣으면 그 블로그만, `dry_run` 은 확인만).
슬롯을 무시하고 도는 수동 실행도 안전장치(하루·계정 상한, 발행 간격)는 그대로 거칩니다. 다만 그 블로그의 **첫 슬롯을 쓴 것으로
기록**되므로, 그날 그 슬롯은 예약 실행에서 다시 돌지 않습니다.

---

## 3. Claude Code 서브에이전트 — `.claude/agents/`

매일 글을 쓰고 올리는 것은 GitHub Actions 가 실행하는 파이썬 코드(`src/`)입니다. 같은 역할을 Claude Code 안에서
사람이 불러 쓸 수 있게 [공식 서브에이전트 형식](https://code.claude.com/docs/en/sub-agents)(프론트매터가 있는 마크다운)으로 정의해 두었습니다.

| 에이전트 | 모델 | 자동화에서 같은 일을 하는 코드 | 이렇게 부릅니다 |
|---|---|---|---|
| `fleet-manager` | Sonnet 5 | `src/manager.py` · `scripts/*_cli.py` | "함대 상태 봐줘", "블로그 추가해" |
| `topic-planner` | Sonnet 5 | `src/planner.py` | "gaganam1 다음 글감 뽑아줘" |
| `post-writer` | Fable 5.1 | `src/writer.py` | "이 글감으로 초안 써줘" |
| `post-reviewer` | Sonnet 5 | `src/reviewer.py` | "방금 쓴 초안 검수해줘" |

- **자동화는 에이전트와 무관하게 돕니다.** 워크플로는 `.claude/` 를 읽지 않습니다.
- **규칙은 한 곳에만 있습니다.** 에이전트는 [`scripts/agent_brief.py`](scripts/agent_brief.py) 로 자동화가 모델에 보내는 지시문 원문을
  받아 씁니다. `src/writer.py` 의 프롬프트를 고치면 에이전트도 같은 기준으로 움직입니다.
- **에이전트는 발행하지 않습니다.** 초안·검수 결과·미리보기는 `out/agents/<블로그>/<시각>/` 에만 남고(깃에 안 올라감) `data/` 도 건드리지 않습니다.
- 에이전트의 모델·effort 는 `config.yaml` 과 맞춰 두었습니다. 한쪽만 바꾸면 `test_logic.py` 가 알려 줍니다.

---

## 4. 블로그·계정 늘리기

**블로그를 늘리는 방법은 '계정에 더 붙이기'가 아니라 '새 계정을 만들어 그 계정의 램프업을 새로 시작하기'입니다.**
계정 상한이 먼저 걸리므로 한 계정에 블로그를 더 붙여도 하루 글 수는 늘지 않고, 새 블로그는 자리가 날 때까지 기다립니다.
한 계정에는 켜진 블로그를 6개까지만 둘 수 있습니다(`max_blogs_per_account`). **저장소는 복사하지 않습니다** — 한 `fleet/blogs.yaml`
안에서 여러 구글 계정을 함께 운영하고, 블로그마다 `account:` 로 어느 계정의 자격증명을 쓸지 정합니다.

### 새 구글 계정 추가 (처음 세팅도 같은 순서)

```bash
python scripts/account_cli.py add third     # blogs.yaml 의 accounts 에 추가하고 아래 순서를 출력합니다
```

| 단계 | 누가 | 내용 |
|---|---|---|
| 1 | 사람 | 새 구글 계정으로 [blogger.com](https://www.blogger.com) 에서 블로그 생성. **처음엔 2개만**, 나머지는 1주 이상 간격을 두고 |
| 2 | 사람 | **그 계정으로 새 Google Cloud 프로젝트** → Blogger API v3 사용 설정(주간 보고용으로 Search Console API · AdSense Management API 도) |
| 3 | 사람 | Google 인증 플랫폼(OAuth 동의 화면) → User Type **외부** → 브랜딩(앱 이름, 지원 이메일, 홈페이지 URL, 개인정보처리방침 URL) → 대상 → **앱 게시(프로덕션)** |
| 4 | 사람 | 사용자 인증 정보 → OAuth 클라이언트 ID → 유형 **데스크톱 앱** |
| 5 | 사람 | `.env` 에 `BLOGGER_CLIENT_ID_THIRD` · `BLOGGER_CLIENT_SECRET_THIRD` 입력 |
| 6 | 사람 | `python scripts/get_blogger_token.py --full --from-env --write-env --account third` → 브라우저에서 **그 계정으로** 승인 |
| 7 | 자동 | `python scripts/account_cli.py check third` → `import third` (블로그를 함대에 등록, 슬롯 자동 배정) |
| 8 | 사람 | `blogs.yaml` 에서 새 블로그마다 `subject`·`pillars`·`audience`·`persona`·`labels`·`since` 를 채움 → `python scripts/fleet_cli.py validate` |
| 9 | 자동 | `python scripts/setup_pages.py --blog <id>` (소개·개인정보처리방침 페이지. `.env` 의 `CONTACT_EMAIL` 필요) |
| 10 | 사람 | [Search Console](https://search.google.com/search-console) 에 **그 계정으로** 로그인 → 블로그 주소로 속성 추가 (Blogger 블로그는 소유권이 자동 확인됨). 빠뜨리면 주간 보고가 경고합니다 |
| 11 | 사람 | GitHub Secrets 에 3개 등록 (`python scripts/copy_secret.py` 가 값을 화면에 띄우지 않고 클립보드로 복사) |
| 12 | 사람 | 워크플로 세 곳(`fleet.yml` · `manager.yml` · `weekly_report.yml`)의 env 에 세 줄씩 추가 → `python scripts/test_logic.py` |
| 13 | 사람 | 커밋·푸시. 다음 슬롯부터 자동으로 돕니다 |

- 환경변수 이름: `default` 계정만 접미사 없는 표준 이름(`BLOGGER_REFRESH_TOKEN`), 그 외는 `_<계정이름대문자>`(`BLOGGER_REFRESH_TOKEN_SECOND`).
- 저장소가 공개라 워크플로는 시크릿을 한꺼번에 넘기지 않고 필요한 것만 적어 둡니다. 한 곳이라도 빠뜨리면 `test_logic.py` 가 실패합니다.
- **기존 Cloud 프로젝트를 재사용하지 마세요.** 제한은 계정에 붙는데, 같은 프로젝트의 OAuth 클라이언트를 쓰면 새 블로그가 제한된 계정과 엮입니다.
- **앱을 '프로덕션'으로 게시하지 않으면** refresh token 이 7일 뒤 만료됩니다. 게시해도 '확인되지 않은 앱' 경고는 뜨는데,
  본인만 쓰는 앱이라 **고급 → (앱 이름)으로 이동** 을 누르면 됩니다.
- 브랜딩에 넣을 홈페이지·개인정보처리방침 URL 은 블로그 페이지로 만들면 됩니다. 템플릿이 [`pages/`](pages) 에 있고 애드센스 심사에도 필요합니다.
- `redirect_uri_mismatch` 오류는 클라이언트를 '웹 애플리케이션' 유형으로 만든 경우입니다. 데스크톱 앱 유형으로 하나 더 만들거나,
  그 클라이언트의 승인된 리디렉션 URI 에 `http://localhost:8080` 을 넣고 `get_blogger_token.py --port 8080` 으로 실행합니다.

### 기존 계정에 블로그 추가

```bash
python scripts/fleet_cli.py add --name "이름" --blog-id <ID> --subject "고유 주제" --account second
python scripts/fleet_cli.py discover --account second [--add]   # 그 계정의 Blogger 블로그 조회 / 미등록 블로그 일괄 등록
```

슬롯은 자동 배정(블로그당 2개)되고, 두 번째 슬롯은 램프업이 4주 뒤에 켭니다. 새 블로그에는 `since:`(첫 글 날짜)를 적고,
같은 계정에서 여러 개를 동시에 시작하지 말고 `since` 를 1주 이상 벌리세요(미래 날짜면 그날부터 돕니다).
비상정지된 계정에는 블로그를 추가할 수 없습니다. 등록한 뒤에는 위 표의 8~10단계(주제 채우기 → `validate`,
`setup_pages.py --blog <id>`, Search Console 속성 추가)를 하고 커밋·푸시합니다.

### 계정 멈추기 · 갈아타기

```bash
python scripts/account_cli.py retire default    # 그 계정 블로그를 전부 멈춤 (이력·설정은 남음)
python scripts/fleet_cli.py enable <블로그id>    # 되살리기
python scripts/account_cli.py resume <계정> --yes   # 비상정지 해제 — 사람이 원인을 확인한 뒤에만. 램프업은 처음 단계부터
```

---

## 5. 비용

| 항목 | 비용 |
|---|---|
| Blogger · GitHub Actions(공개 저장소) · 뉴스 검색 | 무료 |
| **Claude API** | 글 1건 약 **$0.35~0.40** (Fable 작성 + Sonnet 기획·검수, 2026-09 실측) |

- 지금(하루 4건) 약 $1.5/일 · **월 $45 안팎**. `second` 계정이 11/16 에 하루 8건이 되면 월 $90 안팎입니다.
- 블로그 하나가 램프업을 마치면(하루 2건) 월 약 $24 입니다. 10개면 월 $240, 100개면 월 $2,400 수준입니다.
- 블로그별로 `overrides: {writer: {model: claude-sonnet-5}}` 를 두면 그 블로그의 비용이 약 1/3 로 줍니다(품질은 조금 낮아짐).
- [console.anthropic.com](https://console.anthropic.com) 에서 **월 사용 한도**를 예상 비용의 두 배쯤으로 걸어 두세요.
  한도에 걸리면 함대 실행은 슬롯을 기록하지 않고 멈추므로, 한도를 올리면 그날 안에 이어서 돕니다.
- 저장소가 **공개**라 GitHub Actions 는 무료·무제한입니다(코드와 이력만 공개되고 시크릿은 노출되지 않습니다).
  비공개로 바꾸면 월 2,000분 한도가 생기니 그 전에 사용량을 확인하세요.

---

## 6. 설정

모든 블로그에 같은 것은 [`config.yaml`](config.yaml), 블로그마다 다른 것은 [`fleet/blogs.yaml`](fleet/blogs.yaml) 에 있습니다.
함대로 돌 때는 `config.yaml` 위에 `fleet.defaults` → 블로그별 `overrides` 가 차례로 덧씌워집니다. 고친 뒤에는
`python scripts/fleet_cli.py validate` 와 `python scripts/test_logic.py` 를 돌리고 커밋·푸시합니다.

| 설정 | 파일 | 의미 |
|---|---|---|
| `writer.model` · `effort` · `target_length` | config | 작성 모델(Fable 5.1) · 생각 깊이 · 최소 글자 수(1,900자) |
| `review.min_score` | config | 공개 기준 점수(80). 낮추면 더 많이 공개되고 위험도 올라갑니다 |
| `review.model` · `planner.model` · `manager.model` | config | 검수·기획·관리 모델(Sonnet 5) |
| `publish.mode` | config | `auto` 검수 통과 시 공개 / `draft` 전부 임시저장 (불안하면 draft) |
| `publish.max_live_per_day` · `min_gap_minutes` | config | 블로그 하루 공개 상한(2) · 같은 블로그 연속 공개 간격(45분). 상한 2는 테스트로 고정돼 있습니다 |
| `dedupe.history_days` | config | 같은 글감을 다시 쓰지 않을 기간(14일) |
| `monetize.coupang.enabled` | config | 쿠팡 상품 링크 (키가 없으면 자동으로 건너뜀) |
| `slots` · `since` · `account` · `enabled` | blogs | 블로그의 발행 시각들 · 첫 글 날짜(램프업 기준) · 계정 · 켜짐 |
| `subject` · `pillars` · `audience` · `persona` · `labels` | blogs | 고유 주제 · 하위 축(라벨이 됨) · 독자 · 문체 · 고정 라벨 |
| `overrides` | blogs | 이 블로그만 `config.yaml` 값을 바꿈 (예: 작성 모델) |
| `fleet.blog_ramp` · `account_ramp` | blogs | 램프업 표 `[[나이(일), 값], ...]` |
| `accounts.<이름>.since` · `max_live_per_day_account` | blogs | 계정 램프업 기준일 · 계정 최종 상한 |
| `fleet.account_gap_minutes` · `run_budget_minutes` | blogs | 같은 계정 발행 간격(30분) · 한 실행이 간격을 기다리며 쓸 최대 시간(150분) |

관리 판단(중지/재개)은 `data/fleet/manager_state.json`, 계정 비상정지는 `data/fleet/account_state.json` 에 저장되고 실행할 때
`blogs.yaml` 위에 덧씌워집니다. 두 파일과 `data/blogs/<id>/history.json`·`runs.json`, `data/fleet/claims.json` 은 반드시 커밋됩니다.

---

## 7. 왜 이렇게 만들었나 — 2026-09-20 사고

처음에는 모든 블로그가 같은 실시간 검색어를 보고 하루 여러 건을 썼습니다. 2026-09-20 에 `default` 계정의
Blogger API 쓰기가 정책 위반으로 차단됐습니다(403). 원인은 셋이 겹친 것이었습니다.

1. PC 스케줄러(`scripts/fleet_dispatch.ps1`)와 GitHub 예약이 겹쳐 하루 17회 실행 → 동시 실행이 이력 파일에 깃 충돌 마커를 남김
2. 깨진 이력을 '이력 없음'으로 읽어 중복 방지와 상한이 풀림 → 한 블로그 하루 8건, **계정 합계 하루 16건**
3. 새 블로그 4개가 20분 안에 12건을 올림 — 구글의 '대량 생산 콘텐츠(scaled content abuse)' 신호

그래서 지금 구조가 됐습니다: 블로그마다 고유 주제(같은 사건을 여러 블로그가 쓰지 않음), 슬롯 하나에 글 한 건,
서버 기준 상한, 계정 단위 상한과 램프업, 403 비상정지, 이력 손상 감지, 동시 실행 방지.
AI 가 썼다는 것 자체는 정책 위반이 아니지만 검색 순위만 노린 대량 생산은 위반이므로, 발행량은 천천히 늘리고
**검수관이 따로 사실·링크·위험 표현을 대조**합니다. 그래도 검수관도 AI 라 틀린 사실이 공개될 확률이 0은 아닙니다.
불안하면 `publish.mode: draft` 로 두면 임시저장까지만 하고 사람이 공개합니다.

> `scripts/fleet_dispatch.ps1`(PC 가 GitHub 실행을 깨우는 알람 시계)과 `scripts/fleet_local.ps1`(PC 에서 직접 실행)은
> 기본적으로 꺼 둡니다. 작업 스케줄러의 `TrendBlogFleetDispatch` 는 비활성 상태가 정상입니다. PC 실행으로 옮길 때는
> `fleet.yml` 의 schedule 을 지워 두 쪽이 동시에 돌지 않게 해야 합니다.

---

## 8. 문제가 생기면

| 증상 | 원인과 해결 |
|---|---|
| `⛔ 계정 비상정지` 이슈 | Blogger 가 발행을 403 으로 거부. Blogger 에 로그인해 정책 알림 확인 → 해결된 뒤 `account_cli.py resume <계정> --yes` → 커밋·푸시. 제한이 풀리지 않으면 새 계정으로 갈아탑니다([4](#4-블로그계정-늘리기)) |
| `액세스 토큰 발급 실패` | refresh token 만료·폐기. OAuth 앱이 **프로덕션**인지 확인 → `get_blogger_token.py --full --from-env --write-env --account <계정>` → `.env` 와 GitHub Secret 교체 |
| `계정 'x' 자격증명 없음` | GitHub Secrets 이름 오타(대소문자 구분)이거나 워크플로 env 에 그 계정 세 줄이 없음. `test_logic.py` 로 확인 |
| `이력 파일 손상` | `data/blogs/<id>/history.json` 이 깨져 그 블로그가 멈춤(발행하지 않음). 깃 기록에서 복구 |
| 글이 보류·거부만 됨 | Actions 실행 화면의 Summary 에 검수 사유가 있습니다. 같은 유형이 반복되면 블로그 `persona` 나 `src/writer.py` 의 지시문을 고칩니다 |
| `참고 기사 부족` 으로 건너뜀 | 뉴스가 거의 없는 글감. 계속되면 `pillars` 를 검색 수요가 있는 쪽으로 조정 |
| 슬롯보다 몇 시간 늦게 올라감 | GitHub 예약 지연입니다(정상). 하루 글 수는 같습니다 |
| Claude API 한도·잔액 오류 | 함대 실행이 슬롯을 기록하지 않고 멈춤. Console 에서 한도를 올리면 이어서 돕니다 |
| 예약 실행이 아예 멈춤 | 60일 무활동이면 GitHub 이 비활성화합니다. Actions 탭에서 `Enable workflow` |
| 설정을 고쳤더니 전체가 멈춤 | `blogs.yaml` 검증 실패(주제 없음·중복, 슬롯 간격 등)면 실행기가 아무것도 하지 않습니다. `fleet_cli.py validate` 로 확인 |

---

## 구조

```
config.yaml                    공통 설정 (모델·검수 기준·상한)
fleet/blogs.yaml               함대: 계정, 블로그별 주제·슬롯·램프업
.claude/agents/                Claude Code 서브에이전트 — 관리·기획·작성·검수 (3 참고)
src/
  fleet_run.py                 함대 실행기 — 돌 차례인 슬롯을 계정 간격을 지키며 차례로 실행
  main.py                      블로그 한 곳: 글감 → 리서치 → 작성 → 검수 → 판정 → 발행
  fleet.py                     함대 설정·슬롯·램프업·계정 자격증명·비상정지·선점
  guard.py                     발행 안전장치 (Blogger 서버 기준 하루·계정 상한, 발행 간격)
  planner.py                   글감 기획 — 블로그 고유 주제 안에서 (Sonnet)
  research.py                  참고 기사 수집 · 내부 링크 후보
  writer.py                    글 작성 (Fable)
  reviewer.py                  검수 — publish / hold / reject + 구매 의도 판단 (Sonnet)
  manager.py                   매일 밤 관리 — 중지/재개/메모 (Sonnet)
  state.py                     작성 이력 · 중복 방지 · 이력 손상 감지
  llm.py · net.py · config.py  모델 호출 공통(재시도·잘림 감지) · HTTP · 설정 로딩
  publishers/                  blogger.py (공개/임시저장) · local.py (시험용 HTML 저장)
  monetize/coupang.py          쿠팡파트너스 상품 카드
  trends/ · filters.py         실시간 검색어 수집·필터 (옛 trend 모드)
  evergreen.py                 장수 해설 글 (함대에서는 꺼 둠)
scripts/
  fleet_cli.py                 블로그 목록·추가·켜기/끄기·검증·현황
  account_cli.py               구글 계정 추가·확인·등록·은퇴·비상정지 해제
  get_blogger_token.py         계정별 OAuth refresh token 발급 (--full: 보고서용 권한 포함)
  copy_secret.py               .env 값을 화면에 띄우지 않고 클립보드로 (Secrets 등록용)
  setup_pages.py               소개·개인정보처리방침 페이지 생성 (pages/ 템플릿)
  adsense_check.py             애드센스 심사 준비 점검 · 라벨 정리
  weekly_report.py             주간 운영·수익 보고서
  agent_brief.py               서브에이전트용 작업 지시서 (자동화가 모델에 보내는 요청 원문)
  test_logic.py                네트워크 없이 도는 로직 테스트
  selftest.py · check_blogger.py   실제 사이트 연결 점검 (옛 단일 블로그용)
  fleet_local.ps1 · fleet_dispatch.ps1   PC 에서 돌리기 · PC 알람 시계 (기본 꺼 둠)
pages/                         about.html · privacy-policy.html 템플릿
.github/workflows/
  fleet.yml                    함대 실행 (10분마다 예약, 수동 실행 가능)
  manager.yml                  매일 23:35 KST 관리 → 이슈
  weekly_report.yml            매주 월 09:20 KST 주간 보고 → 이슈
data/                          (커밋됨) 실행 상태
  blogs/<id>/history.json      블로그별 작성 이력 — 중복 방지 · 내부 링크 후보
  blogs/<id>/runs.json         블로그별 슬롯 실행 기록
  fleet/manager_state.json     관리 판단(중지/재개) · 사람 조치
  fleet/account_state.json     계정 비상정지
  fleet/claims.json            블로그 간 글감 선점
  history.json                 옛 단일 블로그 이력
```

`data/blogs/<id>/last_run.md`, `data/fleet/last_fleet_run.md`, `manager_report.md` 같은 실행 보고서는 깃에 올라가지 않고
Actions 실행 화면의 Summary 에 붙습니다. `out/` 은 로컬 시험·서브에이전트 결과물입니다.
