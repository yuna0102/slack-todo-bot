"""할 일 맨 앞의 말머리를 해석한다.

- 보낼 곳: '팀' → 팀 워크스페이스만, '같이' → 개인 + 팀, 없으면 개인만
- 마감일: '오늘'·'내일' → 마감일, '언젠가' → 마감 없음
- 말머리는 순서 상관없이 이어 붙일 수 있다 (예: "팀 오늘 QA 요청")
"""

import re
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
PREFIX = re.compile(
    r"^\s*(팀|같이|오늘|내일|언젠가)(?:은|는|까지|중에|중으로)?(?=[\s,.:~-]|$)[\s,.:~-]*"
)
OFFSET = {"오늘": 0, "내일": 1}
ROUTES = {"팀": {"team"}, "같이": {"personal", "team"}}


def parse(msg: dict) -> tuple[str, date | None, set[str]]:
    """(말머리를 뗀 내용, 마감일, 보낼 워크스페이스들)을 돌려준다. 마감일은 할 일을 올린 날 기준."""
    text = msg["text"].strip()
    due_date, route = None, {"personal"}
    while m := PREFIX.match(text):
        rest = text[m.end():].strip()
        if not rest:
            break  # '오늘'만 말한 경우엔 그대로 둔다
        word, text = m.group(1), rest
        if word in ROUTES:
            route = ROUTES[word]
        elif word in OFFSET:
            posted = datetime.fromtimestamp(float(msg["ts"]), KST).date()
            due_date = posted + timedelta(days=OFFSET[word])
    return text, due_date, route
