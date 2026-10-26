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
- 끝낸 할 일에는 ✅ 리액션을 다세요. ✅가 없는 할 일은 랩업에 계속 남습니다(최대 14일).
- 필요 없어진 할 일에는 ❌ 리액션을 다세요. 남은 목록과 완료 목록 모두에서 빠집니다.
- 할 일을 **"오늘"**로 시작하면 마감일이 올린 날, **"내일"**은 다음 날로 잡히고(랩업에 📅, Linear 마감일), **"언젠가"**로 시작하면 마감 없이 올라갑니다. 말머리는 제목에서 빠집니다.
- 내용을 고치려면 할 일 메시지에 **스레드 댓글**로 수정본을 쓰세요. 마지막 댓글이 그 할 일의 내용이 됩니다(Linear 이슈 제목도 갱신).

---

## 1. Slack 앱 만들기 (5분)

1. https://api.slack.com/apps → **Create New App** → **From a manifest** → 워크스페이스 선택
2. 입력 창 위 **YAML** 탭을 고르고 `slack-app-manifest.yml` 내용을 붙여넣고 생성
   > 봇 이름(`display_name`)은 영어 소문자·숫자·`-`·`_`·`.`만 됩니다. 한글은 앱 이름(`name`)에만 쓰세요.
3. **Install to Workspace** → 설치 후 **OAuth & Permissions**에서 `xoxb-…` **Bot User OAuth Token** 복사
4. 어디로 받을지 정하기 (아래 값이 `SLACK_TARGET`):
   - **채널**: 예) `#todo` 비공개 채널을 만들고 채널에서 `/invite @todo-bot`. 채널 이름 클릭 → 맨 아래 **채널 ID**(`C…`) 복사
   - **나만 보는 DM**: Slack 내 프로필 → ⋮ → **멤버 ID 복사**(`U…`). 앱(할일봇)과의 DM으로 올라갑니다.
     > Slack의 "나에게 보내기" DM 자체는 봇이 읽을 수 없어서, 대신 봇과의 1:1 DM을 씁니다. 나만 보는 건 똑같아요.

## 2. 아이폰 단축어 만들기

**단축어 앱 → + → 이름: `할 일`** 로 만들고 동작을 이 순서대로 추가합니다.

| # | 동작 | 설정 |
|---|------|------|
| 1 | **텍스트 받아쓰기** | 언어: 한국어, 듣기 중단: *탭할 때* (긴 문장이 잘리지 않게) |
| 1-1 | **입력 요청** (선택) | 프롬프트: `할 일 확인`, 기본 답변: *받아쓴 텍스트* → 올리기 전에 고치고, 취소하면 안 올라감. 아래 `text`에는 *제공된 입력*을 넣기 |
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

## 4. 할 일 등록 즉시 랩업 갱신 (선택)

랩업은 **주마다 메시지 1개**이고, 실행될 때마다 이전 것을 지우고 채널 맨 아래에 새로 올립니다. 완료한 일은 랩업의 스레드 댓글로 달립니다.
단축어가 Slack에 올린 직후 GitHub Actions도 실행하게 하면 바로 반영됩니다.

1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate
   - Repository access: **Only select repositories** → `slack-todo-bot`
   - Permissions → Repository → **Actions: Read and write** (나머지는 건드리지 않기)
2. 단축어에서 Slack 전송 다음에 **URL 콘텐츠 가져오기** 동작을 하나 더 추가
   - URL: `https://api.github.com/repos/yuna0102/slack-todo-bot/actions/workflows/wrapup.yml/dispatches`
   - 방법: POST
   - 헤더: `Authorization` = `Bearer github_pat_…`, `Accept` = `application/vnd.github+json`
   - 요청 본문: JSON, `ref` = `main`

✅·❌ 리액션만 단 경우엔 다음 할 일 등록 때나 정해진 시간 실행 때 반영됩니다.

## 5. Linear 연동 (선택)

할 일을 Linear 이슈로도 만들고, 상태는 Slack 리액션 기준으로 맞춥니다. Linear 쪽 Slack 연동은 필요 없습니다.

1. Linear → Settings → Account → Security & access → **Personal API keys**에서 키 발급
2. 저장소 Secrets에 등록
   - `LINEAR_API_KEY` = 발급한 키
   - `LINEAR_TEAM_KEY` = 팀 Identifier (이슈 번호 `ABC-1`의 `ABC`)

동작:
- 남은 할 일 → Todo 상태로 이슈 생성 (Claude가 제목을 다듬고 카테고리를 라벨로, 긴급 메모는 우선순위 Urgent), 할 일 스레드에 이슈 링크 댓글
- ✅ → Done, ❌ → Canceled, 리액션을 떼면 다시 Todo
- Linear에서 In Progress 등으로 바꾼 상태는 ✅/❌ 전까지 건드리지 않습니다

**팀 워크스페이스 (선택)**: 팀 워크스페이스에서 발급한 키와 팀 Identifier를 `LINEAR_TEAM_WS_API_KEY`, `LINEAR_TEAM_WS_TEAM_KEY`로 등록하면 말머리로 보낼 곳을 고릅니다.
- **"팀 …"** → 팀 워크스페이스만, **"같이 …"** → 개인 + 팀, 말머리 없음 → 개인만 (날짜 말머리와 함께 "팀 오늘 …"도 가능)
- 이미 올린 할 일은 스레드에 "팀 …" 수정 댓글을 달면 옮겨집니다 (빠진 쪽 이슈는 Canceled)
- 랩업에는 팀 할 일 뒤에 👥가 붙습니다

## 옵션 (워크플로 `env`에 추가)

- `LOOKBACK_DAYS` (기본 `14`): 미완료 할 일을 며칠 전까지 이월할지
- `POST_WHEN_EMPTY` (기본 `false`): 할 일이 없어도 "없어요" 메시지를 보낼지

## 로컬에서 테스트

```bash
pip install -r requirements.txt
SLACK_BOT_TOKEN=xoxb-… SLACK_TARGET=C… ANTHROPIC_API_KEY=sk-ant-… python wrapup.py
```
