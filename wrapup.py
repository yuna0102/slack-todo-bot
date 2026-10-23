"""Slack 할 일 채널을 읽어서 Claude로 이번 주 랩업을 만들고, 주마다 메시지 하나를 계속 갱신한다.

- 할 일이 등록될 때(단축어 → workflow_dispatch)와 정해진 시간에 실행된다
- 주(월요일 00:00 KST 시작)마다 랩업 메시지 1개: 새로 올리고 이전 것은 삭제, 완료 목록은 스레드 댓글
- ✅ / ☑️ / ✔️ 리액션이 달린 할 일 = 완료, 나머지 = 남은 할 일 (LOOKBACK_DAYS 이내)
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
HEADER = "*TBD Wrap-up* ५✍🏼"
LIST_TITLE = "*TBD List ට ̫ ට՞ ՞*"

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_TARGET = os.environ["SLACK_TARGET"]  # 채널 ID(C…/G…) 또는 내 유저 ID(U…, 앱 DM으로 보냄)
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))
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
    return f"- ({t}) {msg['text'].strip()}"


def week_start() -> datetime:
    today = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=today.weekday())


def week_key(start: datetime) -> str:
    year, week, _ = start.isocalendar()
    return f"{year}-W{week:02d}"


def classify(messages: list[dict], since: float):
    todos = [
        m for m in messages
        # 단축어로 올린 할 일은 봇 메시지라 bot_message subtype이 붙을 수 있음
        if not is_wrapup(m)
        and m.get("subtype") in (None, "bot_message")
        and m.get("text", "").strip()
    ]
    remaining = [m for m in todos if not is_done(m)]
    done = [m for m in todos if is_done(m) and float(m["ts"]) >= since]
    return remaining, done


DONE_MARKER = "===DONE==="


def find_week_wrapups(messages: list[dict], key: str) -> list[dict]:
    return [
        m for m in messages
        if is_wrapup(m) and m["metadata"].get("event_payload", {}).get("week") == key
    ]


def delete_wrapup(channel: str, msg: dict, bot_user: str):
    # 스레드 댓글(완료 목록)을 먼저 지워야 "삭제된 메시지" 흔적이 남지 않는다
    if msg.get("reply_count"):
        replies = slack.conversations_replies(channel=channel, ts=msg["ts"])["messages"]
        for r in replies:
            if r["ts"] != msg["ts"] and r.get("user") == bot_user:
                slack.chat_delete(channel=channel, ts=r["ts"])
    slack.chat_delete(channel=channel, ts=msg["ts"])


def summarize(remaining: list[dict], done: list[dict]) -> tuple[str, str, str]:
    """(한 줄 요약, 남은 할 일 목록, 완료 목록)을 돌려준다."""
    now = datetime.now(KST).strftime("%Y-%m-%d (%a) %H:%M")
    body = (
        f"현재 시각: {now} KST\n\n"
        "## 남은 할 일\n"
        + ("\n".join(fmt(m) for m in remaining) or "(없음)")
        + "\n\n## 이번 주 완료한 할 일\n"
        + ("\n".join(fmt(m) for m in done) or "(없음)")
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
            "출력 형식:\n"
            "첫 줄: 이번 주 상황을 한 문장으로 요약 (개수는 따로 표시되니 숫자 나열은 피하기)\n"
            "빈 줄\n"
            f"{LIST_TITLE} (이 제목을 정확히 그대로, 이모지 없이)\n"
            "그 아래 남은 할 일을 • 목록 한 줄에 하나씩 쓰고, 각 항목 앞에 [카테고리]를 붙여 "
            "(예: • [업무] 견적서 보내기). 카테고리 제목 줄을 따로 만들지 마. "
            "카테고리는 할 일 내용을 보고 2~5개 정도로 직접 정하고(예: 업무, 미팅·연락, 개인, 공부), "
            "같은 카테고리끼리 이어서 나열해. "
            "메모에 '긴급'·'급함'·'ASAP'처럼 급하다는 표현이 있는 항목은 목록 맨 위에 두고 "
            "맨 앞에 🚨 를 붙여 (예: • 🚨 [업무] 견적서 보내기). 그 외 항목엔 🚨 금지. "
            "남은 할 일이 없으면 이 제목과 목록은 생략.\n"
            f"그다음 줄에 정확히 {DONE_MARKER} 만 쓰고, 그 아래에 이번 주 완료한 할 일을 "
            "• 목록으로 (카테고리 없이, 제목 없이). 완료한 게 없으면 마커 아래를 비워 둬.\n"
            "없는 할 일을 지어내지 마."
        ),
        messages=[{"role": "user", "content": body}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude가 요청을 거절했습니다.")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    main_part, _, done_part = text.partition(DONE_MARKER)
    summary, _, details = main_part.strip().partition("\n")
    return summary.strip(), details.strip(), done_part.strip()


def progress_bar(done: int, remaining: int, width: int = 10) -> str:
    total = done + remaining
    if not total:
        return "⬛" * width + " 0%"
    filled = round(width * done / total)
    return "⬜" * filled + "⬛" * (width - filled) + f" {round(100 * done / total)}%"


def main():
    channel = resolve_channel(SLACK_TARGET)
    bot_user = slack.auth_test()["user_id"]
    messages = fetch_messages(channel)
    # 레포가 공개라 로그도 공개 — ID나 이름은 찍지 않는다
    print(f"읽은 대화: {describe(channel)}, 최근 {LOOKBACK_DAYS}일 메시지 {len(messages)}건")

    start = week_start()
    key = week_key(start)
    remaining, done = classify(messages, start.timestamp())
    existing = find_week_wrapups(messages, key)

    if not remaining and not done:
        if not POST_WHEN_EMPTY and not existing:
            print("할 일이 없어 랩업을 건너뜁니다.")
            return
        summary, details, done_list = "남은 할 일이 없어요. 🎉", "", ""
    else:
        summary, details, done_list = summarize(remaining, done)

    counts = f"{progress_bar(len(done), len(remaining))}\n이번 주 DONE *{len(done)}* / TBD *{len(remaining)}*"
    text = f"{HEADER}\n{summary}\n{counts}" + (f"\n\n{details}" if details else "")
    if done_list:
        text += "\n\n_완료한 일은 스레드에 있어요 👇_"
    metadata = {"event_type": WRAPUP_EVENT, "event_payload": {"week": key}}

    # 새로 올린 뒤 지워야 실패해도 랩업이 사라지지 않는다
    posted = slack.chat_postMessage(channel=channel, text=text, metadata=metadata)
    if done_list:
        slack.chat_postMessage(
            channel=channel, thread_ts=posted["ts"], text=f"✅ *완료한 일*\n{done_list}"
        )
    for msg in existing:
        delete_wrapup(channel, msg, bot_user)
    print(f"{key} 랩업 올림, 이전 {len(existing)}개 삭제 (완료 {len(done)}건, 남은 {len(remaining)}건)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"실패: {e}", file=sys.stderr)
        raise
