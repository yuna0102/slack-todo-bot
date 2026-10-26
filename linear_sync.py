"""Slack 할 일을 Linear 이슈로 동기화한다. 상태는 Slack이 기준.

- 워크스페이스: 개인(LINEAR_API_KEY / LINEAR_TEAM_KEY), 회사(LINEAR_TEAM_WS_API_KEY / LINEAR_TEAM_WS_TEAM_KEY)
  말머리 '회사' → 회사만, '같이' → 둘 다, 없으면 개인만 (due.py)
- 아직 이슈가 없는 남은 할 일 → 이슈 생성 + 할 일 스레드에 이슈 링크 댓글(메타데이터로 이슈 ID 기록)
- 할 일 스레드 댓글로 수정하면 이슈 제목·설명도 갱신, 말머리를 바꿔 보낼 곳을 바꾸면 빠진 쪽 이슈는 Canceled
- ✅ → Done, ❌ → Canceled, 리액션을 떼면 완료/취소된 이슈를 다시 Todo로
- 키가 없는 워크스페이스는 건너뛴다
"""

import json
import os
import re
import urllib.error
import urllib.request

import anthropic
from slack_sdk import WebClient

import due

LINK_EVENT = "todo_linear"
URGENT = re.compile(r"긴급|급함|급해|asap", re.IGNORECASE)
WORKSPACES = [
    # (이름, 링크 댓글 표시, API 키 환경변수, 팀 키 환경변수)
    ("personal", "Linear", "LINEAR_API_KEY", "LINEAR_TEAM_KEY"),
    ("team", "회사 Linear", "LINEAR_TEAM_WS_API_KEY", "LINEAR_TEAM_WS_TEAM_KEY"),
]


def configured() -> list[tuple]:
    return [w for w in WORKSPACES if os.environ.get(w[2]) and os.environ.get(w[3])]


def gql(api_key: str, query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        # 개인 API 키는 Bearer 없이 그대로 넣는다
        headers={"Authorization": api_key, "Content-Type": "application/json"},
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


def load_team(api_key: str, key: str) -> dict:
    # 한 쿼리에 중첩 목록을 넣으면 "Query too complex"가 나서 나눠서 조회
    nodes = gql(
        api_key,
        """query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes { id } } }""",
        {"key": key},
    )["teams"]["nodes"]
    if not nodes:
        raise RuntimeError("팀 키에 해당하는 Linear 팀이 없습니다.")
    team = gql(
        api_key,
        """query($id: String!) { team(id: $id) {
          id
          states { nodes { id type position } }
          labels(first: 100) { nodes { id name } }
        } }""",
        {"id": nodes[0]["id"]},
    )["team"]

    def first_state(kind: str) -> str:
        states = sorted(
            (s for s in team["states"]["nodes"] if s["type"] == kind), key=lambda s: s["position"]
        )
        return states[0]["id"]

    return {
        "api_key": api_key,
        "id": team["id"],
        "states": {k: first_state(k) for k in ("unstarted", "completed", "canceled")},
        "labels": {l["name"]: l["id"] for l in team["labels"]["nodes"]},
    }


def label_id(team: dict, name: str) -> str:
    if name not in team["labels"]:
        data = gql(
            team["api_key"],
            """mutation($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) { issueLabel { id } }
            }""",
            {"input": {"name": name, "teamId": team["id"]}},
        )
        team["labels"][name] = data["issueLabelCreate"]["issueLabel"]["id"]
    return team["labels"][name]


def find_link(replies: list[dict], ws: str) -> dict | None:
    """할 일 스레드에서 봇이 단 그 워크스페이스의 이슈 링크 댓글을 찾는다."""
    for r in replies:
        meta = r.get("metadata", {})
        # 회사 연동 전에 단 링크 댓글엔 ws가 없다 → 개인
        if meta.get("event_type") == LINK_EVENT and meta["event_payload"].get("ws", "personal") == ws:
            return r
    return None


def draft_issues(todos: list[dict]) -> dict[str, dict]:
    """받아쓰기 메모를 이슈 제목/카테고리로 다듬는다. {ts: {title, category}}"""
    # 말머리('회사'·'오늘' 등)는 따로 처리하니 제목엔 넣지 않는다
    items = "\n".join(f"{m['ts']}\t{due.parse(m)[0]}" for m in todos)
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


def issue_fields(team: dict, msg: dict, draft: dict | None) -> dict:
    raw = msg["text"].strip()
    text, due_date, _ = due.parse(msg)
    title = (draft or {}).get("title") or text
    fields = {
        "title": title[:200],
        "description": f"Slack에서 음성으로 등록한 할 일\n\n> {raw}",
        "priority": 1 if URGENT.search(raw) else 0,
        # 수정으로 말머리가 빠지면 None으로 보내 마감일을 지운다
        "dueDate": due_date.isoformat() if due_date else None,
    }
    if draft and draft.get("category"):
        fields["labelIds"] = [label_id(team, draft["category"])]
    return fields


def create_issue(team: dict, msg: dict, draft: dict | None) -> dict:
    # 지정 안 하면 팀 기본값(보통 Backlog)으로 가서 Todo(첫 unstarted 상태)로 만든다
    issue = {"teamId": team["id"], "stateId": team["states"]["unstarted"], **issue_fields(team, msg, draft)}
    data = gql(
        team["api_key"],
        """mutation($input: IssueCreateInput!) {
          issueCreate(input: $input) { issue { id identifier url } }
        }""",
        {"input": issue},
    )
    return data["issueCreate"]["issue"]


def link_payload(ws: str, issue_id: str, msg: dict) -> dict:
    # applied_edit: 이슈에 반영한 마지막 수정 댓글 ts — 같은 수정을 두 번 반영하지 않기 위함
    return {
        "event_type": LINK_EVENT,
        "event_payload": {"ws": ws, "issue_id": issue_id, "applied_edit": msg.get("edit_ts", "")},
    }


def sync(slack: WebClient, channel: str, todos: list[dict], threads: dict, is_done, is_cancelled) -> str:
    """동기화하고 로그용 한 줄 요약을 돌려준다. todos의 text는 스레드 수정본이 반영된 상태."""
    workspaces = configured()
    names = {w[0] for w in workspaces}
    wanted = {m["ts"]: due.parse(m)[2] for m in todos}
    if any(r - names for r in wanted.values()):
        print("회사 Linear 키가 없어 '회사'·'같이' 할 일 일부를 건너뜁니다.")

    link_msgs = {
        ws: {m["ts"]: r for m in todos if (r := find_link(threads.get(m["ts"], []), ws))}
        for ws, *_ in workspaces
    }
    # 스레드로 수정했는데 아직 이슈에 반영 안 된 할 일 (보낼 곳이 바뀐 경우도 여기서 잡힌다)
    def is_stale(ws, m):
        link = link_msgs[ws].get(m["ts"])
        return link and m.get("edit_ts") and link["metadata"]["event_payload"].get("applied_edit") != m["edit_ts"]

    new = {
        ws: [
            m for m in todos
            if ws in wanted[m["ts"]] and m["ts"] not in link_msgs[ws]
            and not is_done(m) and not is_cancelled(m)
        ]
        for ws, *_ in workspaces
    }
    edited = {ws: [m for m in todos if ws in wanted[m["ts"]] and is_stale(ws, m)] for ws, *_ in workspaces}
    to_draft = {m["ts"]: m for ms in (*new.values(), *edited.values()) for m in ms}
    drafts = draft_issues(list(to_draft.values())) if to_draft else {}

    created = updated = changed = 0
    for ws, label, key_env, team_env in workspaces:
        team = load_team(os.environ[key_env], os.environ[team_env])

        for m in new[ws]:
            issue = create_issue(team, m, drafts.get(m["ts"]))
            slack.chat_postMessage(
                channel=channel,
                thread_ts=m["ts"],
                text=f"🔗 {label} <{issue['url']}|{issue['identifier']}>",
                unfurl_links=False,
                metadata=link_payload(ws, issue["id"], m),
            )
            created += 1

        links = {ts: r["metadata"]["event_payload"]["issue_id"] for ts, r in link_msgs[ws].items()}
        for m in edited[ws]:
            gql(
                team["api_key"],
                """mutation($id: String!, $input: IssueUpdateInput!) {
                  issueUpdate(id: $id, input: $input) { success }
                }""",
                {"id": links[m["ts"]], "input": issue_fields(team, m, drafts.get(m["ts"]))},
            )
            updated += 1
        # 수정 반영 표시 — 보낼 곳에서 빠진 이슈도 같은 수정을 다시 보지 않게 갱신
        for m in todos:
            if is_stale(ws, m):
                link = link_msgs[ws][m["ts"]]
                slack.chat_update(
                    channel=channel, ts=link["ts"], text=link["text"],
                    metadata=link_payload(ws, links[m["ts"]], m),
                )

        # Slack 리액션·보낼 곳에 맞춰 상태 변경
        if not links:
            continue
        data = gql(
            team["api_key"],
            """query($ids: [ID!]) {
              issues(filter: {id: {in: $ids}}, first: 100) { nodes { id state { type } } }
            }""",
            {"ids": list(links.values())},
        )
        current = {n["id"]: n["state"]["type"] for n in data["issues"]["nodes"]}
        for m in todos:
            iid = links.get(m["ts"])
            if not iid or iid not in current:
                continue  # Linear에서 이슈를 지운 경우
            if is_cancelled(m) or ws not in wanted[m["ts"]]:
                want = "canceled"  # ❌ 또는 말머리를 바꿔 이 워크스페이스에서 뺀 경우
            elif is_done(m):
                want = "completed"
            elif current[iid] in ("completed", "canceled"):
                want = "unstarted"  # 리액션을 뗐으면 다시 열기
            else:
                continue  # 진행 중 등 Linear에서 바꾼 상태는 건드리지 않음
            if current[iid] != want:
                gql(
                    team["api_key"],
                    """mutation($id: String!, $state: String!) {
                      issueUpdate(id: $id, input: {stateId: $state}) { success }
                    }""",
                    {"id": iid, "state": team["states"][want]},
                )
                changed += 1

    return f"Linear({len(workspaces)}곳) 이슈 생성 {created}건, 내용 수정 {updated}건, 상태 변경 {changed}건"
