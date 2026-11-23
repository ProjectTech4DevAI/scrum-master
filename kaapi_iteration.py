#!/usr/bin/env python3
"""
Two iteration chores for the Kaapi project board:

  rollover  move unfinished items of the last ended iteration into the current
            one and label them carry-over
  remind    post the current iteration's open carry-over / P0 / P1 items to
            Discord

Both accept --dry-run. `self-check` runs the built-in assertions.
"""

import argparse
import json
import os
import sys

import requests

from kaapi_open_prs import embed_messages
from kaapi_weekly_update import (
    PROJECT_META_QUERY,
    _execute_graphql,
    _find_current_iteration,
    _paginate_items,
    send_discord_message,
)

CARRY_LABEL = "carry-over"
URGENT = ("P0", "P1")
BOT_NAME = "Kaapi Brewmaster"  # watches what is still brewing
# Kaapi logo colors: navy wordmark, yellow dot
NAVY = 0x1F3B73
YELLOW = 0xF2B632
# Twemoji "pouring liquid" (CC-BY 4.0)
AVATAR_URL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/1fad7.png"
LEGEND = "\U0001f525 P0 · ♨️ P1 · \U0001f501 carried over from last iteration"

MOVE_MUTATION = """
mutation MoveItem($project: ID!, $item: ID!, $field: ID!, $iteration: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: { iterationId: $iteration }
  }) { projectV2Item { id } }
}
"""


def _item(raw):
    content = raw.get("content") or {}
    fields = {}
    for v in raw["fieldValues"]["nodes"]:
        if v.get("__typename") == "ProjectV2ItemFieldIterationValue":
            fields["iteration"] = v["iterationId"]
        elif v.get("name"):
            fields[((v.get("field") or {}).get("name") or "").lower()] = v["name"]
    status = fields.get("status") or "No status"
    repo = (content.get("repository") or {}).get("nameWithOwner")  # None for drafts
    return {
        "id": raw["id"],
        "repo": repo,
        "number": content.get("number"),
        "ref": f"{repo.split('/')[-1]}#{content['number']}" if repo else "Draft",
        "title": content.get("title", "(untitled)"),
        "url": content.get("url"),
        "status": status,
        "priority": fields.get("priority"),
        "iteration": fields.get("iteration"),
        "done": status.lower() in ("closed", "done")
        or content.get("state") in ("CLOSED", "MERGED"),
        "carried": CARRY_LABEL
        in [l["name"] for l in (content.get("labels") or {}).get("nodes", [])],
        "assignees": [
            (a.get("name") or a["login"]).split()[0].title()
            for a in (content.get("assignees") or {}).get("nodes", [])
        ]
        or ["Unassigned"],
    }


def load_board(org, number, token):
    """Current + previous iteration and every project item, or None if no current iteration."""
    project = _execute_graphql(PROJECT_META_QUERY, {"org": org, "number": number}, token)[
        "organization"
    ]["projectV2"]
    fields = (project or {}).get("fields", {}).get("nodes", [])
    current = _find_current_iteration(fields)
    if current is None:
        return None
    field = next(f for f in fields if f.get("name") == current["field_name"])
    config = field["configuration"]
    earlier = [
        i
        for i in config["iterations"] + config["completedIterations"]
        if i["startDate"] < current["start"]
    ]
    return {
        "project_id": project["id"],
        "field_id": field["id"],
        "current": current,
        "previous": max(earlier, key=lambda i: i["startDate"], default=None),
        "items": [_item(r) for r in _paginate_items(org, number, token)],
    }


# ── rollover ────────────────────────────────────────────────────────────────


def to_carry(board):
    prev = board["previous"]
    return [
        i for i in board["items"] if prev and i["iteration"] == prev["id"] and not i["done"]
    ]


def rollover(board, token, dry_run):
    items = to_carry(board)
    for i in items:
        print(f"{'would carry' if dry_run else 'carrying'} {i['ref']} [{i['status']}] {i['title']}")
        if dry_run:
            continue
        # Label first: if the move then fails, the next run retries the whole item.
        if i["repo"]:  # draft issues have no labels
            requests.post(
                f"https://api.github.com/repos/{i['repo']}/issues/{i['number']}/labels",
                json={"labels": [CARRY_LABEL]},  # GitHub creates the label if missing
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            ).raise_for_status()
        _execute_graphql(
            MOVE_MUTATION,
            {
                "project": board["project_id"],
                "item": i["id"],
                "field": board["field_id"],
                "iteration": board["current"]["id"],
            },
            token,
        )
    print(
        f"{len(items)} item(s) {'to carry' if dry_run else 'carried'} "
        f"from {(board['previous'] or {}).get('title')} to {board['current']['title']}."
    )


# ── reminder ────────────────────────────────────────────────────────────────


def _icon(item):
    heat = {"P0": "\U0001f525", "P1": "♨️"}.get(item["priority"], "")
    return heat + ("\U0001f501" if item["carried"] else "")


def build_reminder(board, mention=None):
    current = board["current"]
    picked = sorted(
        (
            i
            for i in board["items"]
            if i["iteration"] == current["id"]
            and not i["done"]
            and (i["carried"] or i["priority"] in URGENT)
        ),
        key=lambda i: (i["priority"] not in URGENT, i["priority"] or "", not i["carried"]),
    )
    days_left = current["total_days"] - current["days_elapsed"]
    countdown = f"{days_left} day{'s' * (days_left != 1)} left" if days_left else "last day"
    lines = [
        f"# Still Brewing · {current['title']}",
        f"⏳ {countdown} · "
        + " · ".join(
            f"{icon} {sum(1 for i in picked if test(i))} {name}"
            for icon, name, test in (
                ("\U0001f525", "P0", lambda i: i["priority"] == "P0"),
                ("♨️", "P1", lambda i: i["priority"] == "P1"),
                ("\U0001f501", "carried", lambda i: i["carried"]),
            )
        ),
    ]
    for person in sorted({a for i in picked for a in i["assignees"]}):
        mine = [i for i in picked if person in i["assignees"]]
        lines.append(f"## {person} ({len(mine)})")
        for i in mine:
            ref = f"[`{i['ref']}`](<{i['url']}>)" if i["url"] else f"`{i['ref']}`"
            lines.append(f"{_icon(i)} {ref} {i['title']} — *{i['status']}*")
    if not picked:
        lines.append("_Nothing left brewing. Sip slow._ ☕")
    # Yellow (the logo's dot) for the final stretch of the iteration.
    messages = embed_messages(
        lines, BOT_NAME, YELLOW if days_left <= 2 else NAVY, LEGEND, mention
    )
    for m in messages:
        m["avatar_url"] = AVATAR_URL
    return messages


# ── CLI ─────────────────────────────────────────────────────────────────────


def _self_check():
    def item(n, iteration, status="To Do", priority=None, carried=False, who="dev"):
        return {
            "id": f"I{n}", "repo": "o/kaapi-backend", "number": n, "ref": f"kaapi-backend#{n}",
            "title": f"t{n}", "url": f"https://x/{n}", "status": status, "priority": priority,
            "iteration": iteration, "done": status == "Closed", "carried": carried,
            "assignees": [who],
        }  # fmt: skip

    board = {
        "current": {"id": "cur", "title": "Iteration 2", "total_days": 14, "days_elapsed": 8},
        "previous": {"id": "prev", "title": "Iteration 1"},
        "items": [
            item(1, "prev"),
            item(2, "prev", status="Closed"),
            item(3, "cur", priority="P1"),
            item(4, "cur", priority="P0", who="amy"),
            item(5, "cur", priority="P2"),
            item(6, "cur", carried=True),
            item(7, "cur", priority="P0", status="Closed"),
            item(8, "old", priority="P0"),
        ],
    }
    assert [i["number"] for i in to_carry(board)] == [1], "only unfinished previous-iteration items"
    assert to_carry({**board, "previous": None}) == []
    messages = build_reminder(board)
    text = messages[0]["embeds"][0]["description"]
    assert all(f"#{n}`" in text for n in (3, 4, 6)) and not any(
        f"#{n}`" in text for n in (1, 2, 5, 7, 8)
    ), "open carry-over/P0/P1 of current iteration only"
    assert build_reminder(board, "<@&1>")[0]["content"] == "<@&1>" and "content" not in messages[0]
    assert "6 days left" in text and "\U0001f525 1 P0 · ♨️ 1 P1 · \U0001f501 1 carried" in text
    assert text.index("#3`") < text.index("#6`"), "priority before plain carry-over"
    assert text.index("## amy (1)") < text.index("## dev (2)"), "grouped by assignee"
    assert messages[0]["embeds"][0]["color"] == NAVY and messages[0]["username"] == BOT_NAME
    board["current"]["days_elapsed"] = 14
    last = build_reminder(board)[0]["embeds"][0]
    assert last["color"] == YELLOW and "last day" in last["description"]
    board["items"] = []
    assert "Nothing left brewing" in build_reminder(board)[0]["embeds"][0]["description"]
    raw = {
        "id": "X",
        "content": {
            "number": 9, "state": "OPEN", "title": "t", "url": "u",
            "repository": {"nameWithOwner": "o/r"},
            "labels": {"nodes": [{"name": CARRY_LABEL}]},
            "assignees": {"nodes": [{"login": "amy", "name": "Amy Z"}]},
        },
        "fieldValues": {"nodes": [
            {"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": "P0", "field": {"name": "Priority"}},
            {"__typename": "ProjectV2ItemFieldIterationValue", "iterationId": "cur"},
            {"__typename": "ProjectV2ItemFieldTextValue"},
        ]},
    }  # fmt: skip
    parsed = _item(raw)
    assert (parsed["ref"], parsed["priority"], parsed["iteration"], parsed["carried"], parsed["done"]) == ("r#9", "P0", "cur", True, False)
    assert parsed["assignees"] == ["Amy"] and parsed["status"] == "No status"
    raw["content"]["state"] = "CLOSED"
    assert _item(raw)["done"]
    print("self-check ok")


def main():
    parser = argparse.ArgumentParser(description="Kaapi iteration rollover and reminder.")
    parser.add_argument("command", choices=["rollover", "remind", "self-check"])
    parser.add_argument("--dry-run", action="store_true", help="Print instead of writing/posting")
    args = parser.parse_args()

    if args.command == "self-check":
        return _self_check()

    token = os.environ.get("GITHUB_TOKEN")
    org = os.environ.get("ORG")
    number = os.environ.get("PROJECT_NUMBER")
    webhook_url = os.environ.get("KAAPI_DISCORD_WEBHOOK")
    if not token or not org or not number:
        sys.exit("Error: GITHUB_TOKEN, ORG and PROJECT_NUMBER must be set.")
    if args.command == "remind" and not webhook_url and not args.dry_run:
        sys.exit("Error: KAAPI_DISCORD_WEBHOOK is not set.")

    try:
        board = load_board(org, int(number), token)
        if board is None:
            print("No current iteration found. Nothing to do.")
            return
        if args.command == "rollover":
            return rollover(board, token, args.dry_run)
    except (requests.HTTPError, RuntimeError) as e:
        sys.exit(f"GitHub API error: {e}")

    payloads = build_reminder(board, os.environ.get("DISCORD_MENTION"))
    if args.dry_run:
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
        return
    try:
        for payload in payloads:
            send_discord_message(webhook_url, payload)
    except requests.HTTPError as e:
        sys.exit(f"Error posting to Discord: {e}")
    print("Reminder posted successfully.")


if __name__ == "__main__":
    main()
