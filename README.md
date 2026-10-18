# 할일봇 — 아이폰 음성 → Slack → 정기 랩업

```
[아이폰 액션 버튼 / 뒷면 탭 / "시리야, 할 일"]
        │  받아쓰기
        ▼
[단축어] ──POST chat.postMessage──▶ [Slack 채널 또는 앱 DM]
                                         │
              GitHub Actions (평일 09:00·18:00 KST)
                                         │ wrapup.py: 읽기 → Claude 요약
                                         ▼
                              "📋 할 일 랩업" 메시지
```

- 서버가 필요 없습니다. 할 일 등록은 단축어가 Slack에 바로 올리고, 요약은 GitHub Actions가 정해진 시간에 실행합니다.
- 끝낸 할 일에는 ✅ 리액션을 다세요. ✅가 없는 할 일은 다음 랩업에 "이월"로 계속 나옵니다(최대 7일).

---

## 1. Slack 앱 만들기 (5분)

1. https://api.slack.com/apps → **Create New App** → **From a manifest** → 워크스페이스 선택
2. `slack-app-manifest.yml` 내용을 붙여넣고 생성
3. **Install to Workspace** → 설치 후 **OAuth & Permissions**에서 `xoxb-…` **Bot User OAuth Token** 복사
4. 어디로 받을지 정하기 (아래 값이 `SLACK_TARGET`):
   - **채널**: 예) `#todo` 비공개 채널을 만들고 채널에서 `/invite @할일봇`. 채널 이름 클릭 → 맨 아래 **채널 ID**(`C…`) 복사
   - **나만 보는 DM**: Slack 내 프로필 → ⋮ → **멤버 ID 복사**(`U…`). 앱(할일봇)과의 DM으로 올라갑니다.
     > Slack의 "나에게 보내기" DM 자체는 봇이 읽을 수 없어서, 대신 봇과의 1:1 DM을 씁니다. 나만 보는 건 똑같아요.

## 2. 아이폰 단축어 만들기

**단축어 앱 → + → 이름: `할 일`** 로 만들고 동작을 이 순서대로 추가합니다.

| # | 동작 | 설정 |
|---|------|------|
| 1 | **텍스트 받아쓰기** | 언어: 한국어, 듣기 중단: *일시정지 후* |
| 2 | **URL** | `https://slack.com/api/chat.postMessage` |
| 3 | **URL의 콘텐츠 가져오기** | 방법: **POST**<br>헤더: `Authorization` = `Bearer xoxb-…(봇 토큰)`<br>요청 본문: **JSON**<br>  `channel` (텍스트) = `C…` 또는 `U…`<br>  `text` (텍스트) = *받아쓴 텍스트* 변수 |
| 4 | **알림 보기** (선택) | `✅ 할 일 등록됨: [받아쓴 텍스트]` |

**실행 방법 연결**
- **액션 버튼** (iPhone 15 Pro 이상): 설정 → 액션 버튼 → *단축어* → `할 일`
- **뒷면 탭** (모든 기종): 설정 → 손쉬운 사용 → 터치 → 뒷면 탭 → 이중 탭 → `할 일`
- **Siri**: "시리야, 할 일" (단축어 이름이 곧 명령어)
- **제어 센터 / 잠금 화면** (iOS 18+): 제어 항목 추가 → 단축어 → `할 일`

> 봇 토큰이 단축어 안에 저장되니, 단축어를 남에게 공유하지 마세요.

## 3. 정기 랩업 설정 (GitHub Actions)

1. 이 폴더를 GitHub **비공개** 저장소로 올리기
2. 저장소 → **Settings → Secrets and variables → Actions → New repository secret**에 등록:
   - `SLACK_BOT_TOKEN` = `xoxb-…`
   - `SLACK_TARGET` = `C…` 또는 `U…`
   - `ANTHROPIC_API_KEY` = https://console.anthropic.com 에서 발급
3. **Actions** 탭 → *할 일 랩업* → **Run workflow**로 바로 테스트

**시간 바꾸기**: `.github/workflows/wrapup.yml`의 `cron`은 UTC 기준입니다(KST − 9시간).

| 원하는 시간 (KST) | cron |
|---|---|
| 평일 09:00, 18:00 (기본) | `0 0,9 * * 1-5` |
| 매일 08:30 | `30 23 * * *` |
| 매일 09·13·18시 | `0 0,4,9 * * *` |

> GitHub 예약 실행은 몇 분~십수 분 늦게 돌 수 있습니다.

## 옵션 (워크플로 `env`에 추가)

- `LOOKBACK_DAYS` (기본 `7`): 미완료 할 일을 며칠 전까지 이월할지
- `POST_WHEN_EMPTY` (기본 `false`): 할 일이 없어도 "없어요" 메시지를 보낼지

## 로컬에서 테스트

```bash
pip install -r requirements.txt
SLACK_BOT_TOKEN=xoxb-… SLACK_TARGET=C… ANTHROPIC_API_KEY=sk-ant-… python wrapup.py
```
