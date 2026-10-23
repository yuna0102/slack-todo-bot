"""Slack 할 일 채널을 읽어서 Claude로 랩업 요약을 만들어 같은 채널에 올린다.

- 마지막 랩업 이후 올라온 할 일 = "새 할 일"
- 그 이전에 올라왔지만 ✅ 리액션이 없는 할 일 = "이월된 할 일" (LOOKBACK_DAYS 이내)
- ✅ / ☑️ / ✔️ 리액션이 달린 할 일 = 완료
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic
from slack_sdk import WebClient

KST = timezone(timedelta(hours=9))
WRAPUP_EVENT = "todo_wrapup"
DONE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
HEADER = "*TBD Wrap-up (୨ˊ ³ `)ᐣ✎ ﹏*"
LIST_TITLE = "*TBD List ට ̫ ට՞ ՞*"

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_TARGET = os.environ["SLACK_TARGET"]  # 채널 ID(C…/G…) 또는 내 유저 ID(U…, 앱 DM으로 보냄)
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
POST_WHEN_EMPTY = os.environ.get("POST_WHEN_EMPTY", "false").lower() == "true"

slack = WebClient(token=SLACK_BOT_TOKEN)


def resolve_channel(target: str) -> str:
    if target.startswith("U"):
        if target == slack.auth_test()["user_id"]:
            raise ValueError(
                "SLACK_TARGET이 봇 자신의 ID입니다. todo-bot이 아니라 내 프로필에서 멤버 ID를 복사하세요."
            )
        return slack.conversations_open(users=target)["channel"]["id"]
    return target


def describe(channel: str) -> str:
    # conversations.info는 추가 권한이 필요해서 ID 접두어로만 구분
    kind = "앱 DM" if channel.startswith("D") else "채널"
    return f"{kind}(ID 끝자리 …{channel[-4:]})"


def fetch_messages(channel: str) -> list[dict]:
    oldest = int(time.time() - LOOKBACK_DAYS * 86400)
    messages, cursor = [], None
    while True:
        resp = slack.conversations_history(
            channel=channel,
            oldest=str(oldest),
            limit=200,
            include_all_metadata=True,
            cursor=cursor,
        )
        messages.extend(resp["messages"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return sorted(messages, key=lambda m: float(m["ts"]))


def is_wrapup(msg: dict) -> bool:
    return msg.get("metadata", {}).get("event_type") == WRAPUP_EVENT


def is_done(msg: dict) -> bool:
    return any(r["name"] in DONE_REACTIONS for r in msg.get("reactions", []))


def fmt(msg: dict) -> str:
    t = datetime.fromtimestamp(float(msg["ts"]), KST).strftime("%m/%d %H:%M")
    mark = "[완료]" if is_done(msg) else "[미완료]"
    return f"- {mark} ({t}) {msg['text'].strip()}"


def classify(messages: list[dict]):
    wrapups = [m for m in messages if is_wrapup(m)]
    last_ts = float(wrapups[-1]["ts"]) if wrapups else 0.0
    todos = [
        m for m in messages
        if not is_wrapup(m) and not m.get("subtype") and m.get("text", "").strip()
    ]
    new = [m for m in todos if float(m["ts"]) > last_ts]
    carried = [m for m in todos if float(m["ts"]) <= last_ts and not is_done(m)]
    return new, carried


def summarize(new: list[dict], carried: list[dict]) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d (%a) %H:%M")
    body = (
        f"현재 시각: {now} KST\n\n"
        f"## 지난 랩업 이후 새로 올라온 할 일\n"
        + ("\n".join(fmt(m) for m in new) or "(없음)")
        + "\n\n## 이전부터 남아있는 미완료 할 일\n"
        + ("\n".join(fmt(m) for m in carried) or "(없음)")
    )

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=(
            "너는 사용자가 음성으로 던져놓은 할 일 메모를 정리해주는 비서야. "
            "메모는 받아쓰기라 오타나 구어체가 섞여 있으니 의도를 살려 간결하게 다듬어. "
            "출력은 Slack mrkdwn 형식(굵게는 *텍스트*, 목록은 •)으로, 한국어로 작성해. "
            "구성: 1) 한 줄 요약 "
            f"2) 미완료 할 일 목록 — 섹션 제목은 정확히 {LIST_TITLE} 로 쓰고 이모지를 붙이지 마. "
            "비슷한 것끼리 묶되, 메모에 '긴급'·'급함'·'ASAP'처럼 급하다는 표현이 있는 항목은 "
            "목록 맨 위에 두고 항목 앞에 🚨 를 붙여 (예: • 🚨 견적서 보내기). 그 외 항목엔 🚨 금지. "
            "3) ✅ 완료한 것. "
            "비어 있는 섹션은 생략하고, 없는 할 일을 지어내지 마."
        ),
        messages=[{"role": "user", "content": body}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude가 요청을 거절했습니다.")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def main():
    channel = resolve_channel(SLACK_TARGET)
    messages = fetch_messages(channel)
    # 레포가 공개라 로그도 공개 — ID나 이름은 찍지 않는다
    print(f"읽은 대화: {describe(channel)}, 최근 {LOOKBACK_DAYS}일 메시지 {len(messages)}건")
    new, carried = classify(messages)

    if not new and not carried:
        if POST_WHEN_EMPTY:
            text = f"{HEADER}\n남은 할 일이 없어요. 🎉"
        else:
            print("할 일이 없어 랩업을 건너뜁니다.")
            return
    else:
        text = f"{HEADER}\n" + summarize(new, carried)

    slack.chat_postMessage(
        channel=channel,
        text=text,
        metadata={"event_type": WRAPUP_EVENT, "event_payload": {"count": len(new) + len(carried)}},
    )
    print(f"랩업 전송 완료 (새 {len(new)}건, 이월 {len(carried)}건)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"실패: {e}", file=sys.stderr)
        raise
