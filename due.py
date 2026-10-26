"""할 일 맨 앞의 날짜 말머리를 해석한다. '오늘'·'내일' → 마감일, '언젠가' → 마감 없음."""

import re
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
PREFIX = re.compile(r"^\s*(오늘|내일|언젠가)(?:은|는|까지|중에|중으로)?(?=[\s,.:~-]|$)[\s,.:~-]*")
OFFSET = {"오늘": 0, "내일": 1}


def parse(msg: dict) -> tuple[str, date | None]:
    """(말머리를 뗀 내용, 마감일)을 돌려준다. 마감일은 할 일을 올린 날 기준."""
    text = msg["text"].strip()
    m = PREFIX.match(text)
    if not m:
        return text, None
    rest = text[m.end():].strip() or text  # '오늘'만 말한 경우엔 그대로 둔다
    word = m.group(1)
    if word not in OFFSET:
        return rest, None
    posted = datetime.fromtimestamp(float(msg["ts"]), KST).date()
    return rest, posted + timedelta(days=OFFSET[word])
