"""임시 진단용: Slack 응답의 개수/상태만 출력 (공개 로그라 내용·ID는 찍지 않음)."""
import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

c = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
ch = os.environ["SLACK_TARGET"]

a = c.auth_test()
print("auth ok:", a["ok"], "| team 끝자리:", a["team_id"][-4:], "| bot user 끝자리:", a["user_id"][-4:])
print("토큰 권한:", a.headers.get("x-oauth-scopes"))

for label, kw in [
    ("옵션 없음", {}),
    ("limit=200", {"limit": 200}),
    ("메타데이터 포함", {"limit": 200, "include_all_metadata": True}),
]:
    try:
        r = c.conversations_history(channel=ch, **kw)
        warn = r.get("response_metadata", {}).get("warnings") or r.get("warning")
        print(f"[{label}] ok={r['ok']} 메시지={len(r['messages'])} has_more={r.get('has_more')} warning={warn}")
        print("   subtype 목록:", [m.get("subtype", "-") for m in r["messages"]])
    except SlackApiError as e:
        print(f"[{label}] 에러: {e.response['error']}")
