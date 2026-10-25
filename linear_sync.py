"""Slack 할 일을 Linear 이슈로 동기화한다. 상태는 Slack이 기준.

- 아직 이슈가 없는 남은 할 일 → 이슈 생성 + 할 일 스레드에 이슈 링크 댓글(메타데이터로 이슈 ID 기록)
- ✅ → Done, ❌ → Canceled, 리액션을 떼면 완료/취소된 이슈를 다시 Todo로
- LINEAR_API_KEY / LINEAR_TEAM_KEY가 없으면 아무것도 하지 않는다
"""

import json
import os
import re
import urllib.error
import urllib.request

import anthropic
from slack_sdk import WebClient

LINK_EVENT = "todo_linear"
URGENT = re.compile(r"긴급|급함|급해|asap", re.IGNORECASE)


def gql(query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        # 개인 API 키는 Bearer 없이 그대로 넣는다
        headers={"Authorization": os.environ["LINEAR_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        # 쿼리 오류도 400으로 오니 본문의 메시지를 꺼낸다
        body = json.loads(e.read() or b"{}")
        if not body.get("errors"):
            raise
    if body.get("errors"):
        raise RuntimeError(f"Linear API 오류: {body['errors'][0].get('message')}")
    return body["data"]


def load_team(key: str) -> dict:
    data = gql(
        """query($key: String!) {
          teams(filter: {key: {eq: $key}}) { nodes {
            id
            states { nodes { id type position } }
            labels(first: 250) { nodes { id name } }
          } }
        }""",
        {"key": key},
    )
    nodes = data["teams"]["nodes"]
    if not nodes:
        raise RuntimeError("LINEAR_TEAM_KEY에 해당하는 팀이 없습니다.")
    team = nodes[0]

    def first_state(kind: str) -> str:
        states = sorted(
            (s for s in team["states"]["nodes"] if s["type"] == kind), key=lambda s: s["position"]
        )
        return states[0]["id"]

    return {
        "id": team["id"],
        "states": {k: first_state(k) for k in ("unstarted", "completed", "canceled")},
        "labels": {l["name"]: l["id"] for l in team["labels"]["nodes"]},
    }


def label_id(team: dict, name: str) -> str:
    if name not in team["labels"]:
        data = gql(
            """mutation($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) { issueLabel { id } }
            }""",
            {"input": {"name": name, "teamId": team["id"]}},
        )
        team["labels"][name] = data["issueLabelCreate"]["issueLabel"]["id"]
    return team["labels"][name]


def find_link(slack: WebClient, channel: str, msg: dict) -> str | None:
    """할 일 스레드에 달린 이슈 링크 댓글에서 이슈 ID를 찾는다."""
    if not msg.get("reply_count"):
        return None
    replies = slack.conversations_replies(
        channel=channel, ts=msg["ts"], include_all_metadata=True
    )["messages"]
    for r in replies:
        meta = r.get("metadata", {})
        if meta.get("event_type") == LINK_EVENT:
            return meta["event_payload"]["issue_id"]
    return None


def draft_issues(todos: list[dict]) -> dict[str, dict]:
    """받아쓰기 메모를 이슈 제목/카테고리로 다듬는다. {ts: {title, category}}"""
    items = "\n".join(f"{m['ts']}\t{m['text'].strip()}" for m in todos)
    response = anthropic.Anthropic().beta.messages.create(
        model="claude-opus-5-5",
        max_tokens=4000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=(
            "음성 받아쓰기로 적은 할 일 메모를 이슈 트래커 제목으로 다듬어. "
            "입력은 한 줄에 하나씩 'ID<탭>메모' 형식이야. "
            "오타·구어체를 의도대로 고쳐 짧은 한국어 제목으로 만들고, '긴급' 같은 급함 표현은 제목에서 빼. "
            "카테고리는 업무, 미팅·연락, 개인, 공부 같은 짧은 한국어 단어 하나로 정해. "
            '다른 말 없이 JSON 배열만 출력: [{"id": "...", "title": "...", "category": "..."}]'
        ),
        messages=[{"role": "user", "content": items}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        parsed = json.loads(text[text.index("[") : text.rindex("]") + 1])
        return {d["id"]: d for d in parsed if d.get("title")}
    except (ValueError, KeyError, TypeError):
        return {}  # 다듬기에 실패하면 원문 그대로 이슈를 만든다


def create_issue(team: dict, msg: dict, draft: dict | None) -> dict:
    raw = msg["text"].strip()
    title = (draft or {}).get("title") or raw
    issue = {
        "teamId": team["id"],
        "title": title[:200],
        "description": f"Slack에서 음성으로 등록한 할 일\n\n> {raw}",
        "priority": 1 if URGENT.search(raw) else 0,
    }
    if draft and draft.get("category"):
        issue["labelIds"] = [label_id(team, draft["category"])]
    data = gql(
        """mutation($input: IssueCreateInput!) {
          issueCreate(input: $input) { issue { id identifier url } }
        }""",
        {"input": issue},
    )
    return data["issueCreate"]["issue"]


def sync(slack: WebClient, channel: str, todos: list[dict], is_done, is_cancelled) -> str:
    """동기화하고 로그용 한 줄 요약을 돌려준다."""
    team = load_team(os.environ["LINEAR_TEAM_KEY"])
    links = {m["ts"]: find_link(slack, channel, m) for m in todos}

    # 1) 이슈가 없는 남은 할 일 → 생성 (이미 끝냈거나 취소한 할 일은 만들지 않음)
    new = [m for m in todos if not links[m["ts"]] and not is_done(m) and not is_cancelled(m)]
    drafts = draft_issues(new) if new else {}
    for m in new:
        issue = create_issue(team, m, drafts.get(m["ts"]))
        slack.chat_postMessage(
            channel=channel,
            thread_ts=m["ts"],
            text=f"🔗 Linear <{issue['url']}|{issue['identifier']}>",
            unfurl_links=False,
            metadata={"event_type": LINK_EVENT, "event_payload": {"issue_id": issue["id"]}},
        )

    # 2) 이미 있는 이슈 → Slack 리액션에 맞춰 상태 변경
    linked = {ts: iid for ts, iid in links.items() if iid}
    changed = 0
    if linked:
        data = gql(
            """query($ids: [ID!]) {
              issues(filter: {id: {in: $ids}}, first: 250) { nodes { id state { type } } }
            }""",
            {"ids": list(linked.values())},
        )
        current = {n["id"]: n["state"]["type"] for n in data["issues"]["nodes"]}
        for m in todos:
            iid = linked.get(m["ts"])
            if not iid or iid not in current:
                continue  # Linear에서 이슈를 지운 경우
            if is_cancelled(m):
                want = "canceled"
            elif is_done(m):
                want = "completed"
            elif current[iid] in ("completed", "canceled"):
                want = "unstarted"  # 리액션을 뗐으면 다시 열기
            else:
                continue  # 진행 중 등 Linear에서 바꾼 상태는 건드리지 않음
            if current[iid] != want:
                gql(
                    """mutation($id: String!, $state: String!) {
                      issueUpdate(id: $id, input: {stateId: $state}) { success }
                    }""",
                    {"id": iid, "state": team["states"][want]},
                )
                changed += 1

    return f"Linear 이슈 생성 {len(new)}건, 상태 변경 {changed}건"
