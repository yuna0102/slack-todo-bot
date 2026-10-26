"""Slack 할 일 채널을 읽어서 Claude로 이번 주 랩업을 만들고, 주마다 메시지 하나를 계속 갱신한다.

- 할 일이 등록될 때(단축어 → workflow_dispatch)와 정해진 시간에 실행된다
- 주(월요일 00:00 KST 시작)마다 랩업 메시지 1개: 새로 올리고 이전 것은 삭제, 완료 목록은 스레드 댓글
- LINEAR_API_KEY가 있으면 할 일을 Linear 이슈로도 동기화 (linear_sync.py)
- 할 일 스레드에 댓글을 달면 마지막 댓글이 그 할 일의 수정본
- 👀 = 진행 중(Linear In Progress), ✅ / ☑️ / ✔️ 리액션이 달린 할 일 = 완료, ❌ / ❎ = 취소(목록에서 제외), 나머지 = 남은 할 일 (LOOKBACK_DAYS 이내)
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic
from slack_sdk import WebClient

import due
import linear_sync

KST = timezone(timedelta(hours=9))
WRAPUP_EVENT = "todo_wrapup"
DONE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}
CANCEL_REACTIONS = {"x", "negative_squared_cross_mark"}
IN_PROGRESS_REACTIONS = {"eyes", "hourglass_flowing_sand", "hourglass"}
HEADER = "*TBD Wrap-up* ५✍🏼"
LIST_TITLE = "*TBD List ට ̫ ට՞ ՞*"

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_TARGET = os.environ["SLACK_TARGET"]  # 채널 ID(C…/G…) 또는 내 유저 ID(U…, 앱 DM으로 보냄)
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))
POST_WHEN_EMPTY = os.environ.get("POST_WHEN_EMPTY", "false").lower() == "true"
LINEAR_ENABLED = bool(linear_sync.configured())
# 정해진 시간(09:00·18:00) 실행은 바뀐 게 없어도 채널 맨 아래로 다시 올린다
FORCE = os.environ.get("FORCE", "false").lower() == "true"

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


def has_reaction(msg: dict, names: set[str]) -> bool:
    return any(r["name"] in names for r in msg.get("reactions", []))


def is_done(msg: dict) -> bool:
    return has_reaction(msg, DONE_REACTIONS)


def is_cancelled(msg: dict) -> bool:
    return has_reaction(msg, CANCEL_REACTIONS)


def is_in_progress(msg: dict) -> bool:
    return has_reaction(msg, IN_PROGRESS_REACTIONS)


def fingerprint(key: str, todos: list[dict]) -> str:
    """랩업에 영향을 주는 상태(할 일 내용·수정본·리액션, 주, 날짜)의 지문. 같으면 다시 올릴 필요 없음."""
    state = [
        key,
        datetime.now(KST).date().isoformat(),  # 날짜가 바뀌면 '⏰ 지남' 표시가 달라진다
        [(m["ts"], m["text"], sorted(r["name"] for r in m.get("reactions", []))) for m in todos],
    ]
    return hashlib.sha256(json.dumps(state, ensure_ascii=False).encode()).hexdigest()[:16]


def fmt(msg: dict) -> str:
    t = datetime.fromtimestamp(float(msg["ts"]), KST).strftime("%m/%d %H:%M")
    text, due_date, route = due.parse(msg)
    tags = "[회사] " if "team" in route else "[개인용] "
    if is_in_progress(msg):
        tags += "[진행 중] "
    if due_date:
        overdue = " 지남" if due_date < datetime.now(KST).date() else ""
        tags += f"[마감 {due_date:%m/%d}{overdue}] "
    return f"- ({t}) {tags}{text}"


def week_start() -> datetime:
    today = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=today.weekday())


def week_key(start: datetime) -> str:
    year, week, _ = start.isocalendar()
    return f"{year}-W{week:02d}"


def todo_messages(messages: list[dict]) -> list[dict]:
    return [
        m for m in messages
        # 단축어로 올린 할 일은 봇 메시지라 bot_message subtype이 붙을 수 있음
        if not is_wrapup(m)
        and m.get("subtype") in (None, "bot_message")
        and m.get("text", "").strip()
    ]


def fetch_threads(channel: str, todos: list[dict]) -> dict[str, list[dict]]:
    """댓글이 달린 할 일의 스레드 댓글(원글 제외)을 읽는다. {할 일 ts: 댓글들}"""
    threads = {}
    for m in todos:
        if m.get("reply_count"):
            replies = slack.conversations_replies(
                channel=channel, ts=m["ts"], include_all_metadata=True
            )["messages"]
            threads[m["ts"]] = [r for r in replies if r["ts"] != m["ts"]]
    return threads


def apply_edits(todos: list[dict], threads: dict[str, list[dict]]):
    """할 일 스레드에 내가 쓴 마지막 댓글을 수정본으로 보고 할 일 내용을 바꾼다."""
    for m in todos:
        mine = [
            r for r in threads.get(m["ts"], [])
            # 봇이 단 링크 댓글 등은 제외 — 사람이 쓴 댓글엔 bot_id가 없다
            if not r.get("bot_id") and r.get("text", "").strip()
        ]
        if mine:
            m["text"] = mine[-1]["text"]
            m["edit_ts"] = mine[-1]["ts"]


def classify(messages: list[dict], since: float):
    # ❌가 달리면 완료든 아니든 목록에서 뺀다
    todos = [m for m in todo_messages(messages) if not is_cancelled(m)]
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
            "메모에 [마감 MM/DD]가 붙은 항목은 작업명 뒤에 ` · 📅 MM/DD`를 붙이고, "
            "[마감 MM/DD 지남]이면 ` · ⏰ MM/DD 지남`을 붙여. 마감이 없는 항목엔 날짜를 붙이지 마. "
            "[진행 중]이 붙은 항목은 작업명 바로 뒤(날짜 앞)에 ` · 👀`를 붙여. "
            "메모의 [회사]/[개인용] 표시는 보낼 곳이야(카테고리 아님). 각 항목 맨 앞(🚨가 있으면 🚨 바로 뒤)에 "
            "[회사]면 🏢, [개인용]이면 🛁 를 붙여 (예: • 🏢 [업무] QA 요청, • 🛁 [개인] 장보기, "
            "• 🚨 🏢 [업무] 견적서 보내기).\n"
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
        return "⬜" * width + " 0%"
    filled = round(width * done / total)
    return "⬛" * filled + "⬜" * (width - filled) + f" {round(100 * done / total)}%"


def main():
    channel = resolve_channel(SLACK_TARGET)
    bot_user = slack.auth_test()["user_id"]
    messages = fetch_messages(channel)
    # 레포가 공개라 로그도 공개 — ID나 이름은 찍지 않는다
    print(f"읽은 대화: {describe(channel)}, 최근 {LOOKBACK_DAYS}일 메시지 {len(messages)}건")

    todos = todo_messages(messages)
    threads = fetch_threads(channel, todos)
    apply_edits(todos, threads)  # messages 안의 같은 dict를 고치므로 아래 분류에도 반영된다

    start = week_start()
    key = week_key(start)
    remaining, done = classify(messages, start.timestamp())
    existing = find_week_wrapups(messages, key)

    fp = fingerprint(key, todos)
    if not FORCE and existing and existing[-1]["metadata"]["event_payload"].get("fp") == fp:
        print("바뀐 게 없어 건너뜁니다.")
        return

    linear_error = None
    if LINEAR_ENABLED:
        # Linear가 실패해도 랩업은 올리고, 실행은 마지막에 실패로 표시 (레포 공개라 이슈 번호는 안 찍음)
        try:
            print(linear_sync.sync(slack, channel, todos, threads, is_done, is_cancelled, is_in_progress))
        except Exception as e:
            linear_error = e
            print(f"Linear 동기화 실패: {e}", file=sys.stderr)

    if not remaining and not done:
        if not POST_WHEN_EMPTY and not existing:
            print("할 일이 없어 랩업을 건너뜁니다.")
            if linear_error:
                raise linear_error
            return
        summary, details, done_list = "남은 할 일이 없어요. 🎉", "", ""
    else:
        summary, details, done_list = summarize(remaining, done)

    counts = f"{progress_bar(len(done), len(remaining))}\n이번 주 DONE *{len(done)}* / TBD *{len(remaining)}*"
    text = f"{HEADER}\n{summary}\n{counts}" + (f"\n\n{details}" if details else "")
    if done_list:
        text += "\n\n_완료한 일은 스레드에 있어요_ ( •̀ •́ゞ)"
    # Linear가 실패했으면 지문을 비워 다음 실행에서 다시 시도하게 한다
    metadata = {"event_type": WRAPUP_EVENT, "event_payload": {"week": key, "fp": "" if linear_error else fp}}

    # 새로 올린 뒤 지워야 실패해도 랩업이 사라지지 않는다
    posted = slack.chat_postMessage(channel=channel, text=text, metadata=metadata)
    if done_list:
        slack.chat_postMessage(
            channel=channel, thread_ts=posted["ts"], text=f"✅ *완료한 일*\n{done_list}"
        )
    for msg in existing:
        delete_wrapup(channel, msg, bot_user)
    print(f"{key} 랩업 올림, 이전 {len(existing)}개 삭제 (완료 {len(done)}건, 남은 {len(remaining)}건)")
    if linear_error:
        raise linear_error


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"실패: {e}", file=sys.stderr)
        raise
