#!/usr/bin/env python3
"""
Fetches the current iteration from a GitHub Projects v2 board and posts a
formatted summary to Discord. Runs via cron every Monday at 9 AM IST, or
manually with --dry-run.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_API_URL = "https://api.github.com/graphql"

# ── GitHub GraphQL ──────────────────────────────────────────────────────────

PROJECT_META_QUERY = """
query ProjectMeta($org: String!, $number: Int!) {
  organization(login: $org) {
    projectV2(number: $number) {
      id
      title
      fields(first: 50) {
        nodes {
          __typename
          ... on ProjectV2IterationField {
            id
            name
            configuration {
              iterations {
                id
                title
                startDate
                duration
              }
              completedIterations {
                id
                title
                startDate
                duration
              }
            }
          }
        }
      }
    }
  }
}
"""

PROJECT_ITEMS_QUERY = """
query ProjectItems($org: String!, $number: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $number) {
      items(first: 50, after: $after) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          content {
            __typename
            ... on Issue {
              number
              title
              url
              repository { nameWithOwner }
              assignees(first: 10) { nodes { login name } }
            }
            ... on PullRequest {
              number
              title
              url
              repository { nameWithOwner }
              assignees(first: 10) { nodes { login name } }
            }
            ... on DraftIssue {
              title
              assignees(first: 10) { nodes { login name } }
            }
          }
          fieldValues(first: 20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field {
                  ... on ProjectV2SingleSelectField { name }
                }
              }
              ... on ProjectV2ItemFieldIterationValue {
                title
                startDate
                duration
                iterationId
                field {
                  ... on ProjectV2IterationField { name }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _execute_graphql(query, variables, token):
    """Execute a GraphQL query against the GitHub API."""
    response = requests.post(
        GITHUB_API_URL,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if "errors" in body:
        raise RuntimeError(f"GitHub GraphQL errors: {body['errors']}")
    return body["data"]


def _find_current_iteration(project_fields, iteration_field_name="Weekly Tasks"):
    """Pick today's iteration from the named iteration field."""
    today = date.today()
    target_name_lower = iteration_field_name.lower()

    iteration_field = None
    for field in project_fields:
        if field.get("__typename") != "ProjectV2IterationField":
            continue
        if field["name"].lower() == target_name_lower:
            iteration_field = field
            break

    # Fall back to the first iteration field if the named one isn't found
    if iteration_field is None:
        for field in project_fields:
            if field.get("__typename") == "ProjectV2IterationField":
                iteration_field = field
                break

    if iteration_field is None:
        return None

    config = iteration_field.get("configuration") or {}
    for iteration in config.get("iterations", []):
        start = datetime.strptime(iteration["startDate"], "%Y-%m-%d").date()
        end = start + timedelta(days=iteration["duration"])
        if start <= today < end:
            return {
                "field_name": iteration_field["name"],
                "id": iteration["id"],
                "title": iteration["title"],
                "start": start.isoformat(),
                "end": (end - timedelta(days=1)).isoformat(),
                "total_days": iteration["duration"],
                "days_elapsed": (today - start).days + 1,
            }
    return None


def _paginate_items(org, number, token):
    """Paginate through all items in a project (50 at a time)."""
    all_items = []
    cursor = None
    while True:
        variables = {"org": org, "number": number}
        if cursor:
            variables["after"] = cursor
        data = _execute_graphql(PROJECT_ITEMS_QUERY, variables, token)
        items_data = data["organization"]["projectV2"]["items"]
        all_items.extend(items_data["nodes"])
        page_info = items_data["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return all_items


def _extract_field_values(raw_field_values):
    """Flatten a project item's fieldValues into {status, iteration_id}."""
    status = None
    iteration_id = None
    for value in raw_field_values:
        typename = value.get("__typename")
        field_info = value.get("field") or {}
        field_name = (field_info.get("name") or "").lower()
        if typename == "ProjectV2ItemFieldSingleSelectValue" and field_name == "status":
            status = value.get("name")
        elif typename == "ProjectV2ItemFieldIterationValue":
            iteration_id = value.get("iterationId")
    return status, iteration_id


def fetch_current_iteration(org, project_number, token):
    """Fetch current iteration metadata and all items belonging to it."""
    meta = _execute_graphql(
        PROJECT_META_QUERY, {"org": org, "number": project_number}, token
    )
    project = meta["organization"]["projectV2"]
    if project is None:
        return None

    fields = project["fields"]["nodes"]
    current = _find_current_iteration(fields)
    if current is None:
        return None

    raw_items = _paginate_items(org, project_number, token)

    items = []
    for item in raw_items:
        content = item.get("content") or {}
        status, iteration_id = _extract_field_values(
            item["fieldValues"]["nodes"]
        )
        if iteration_id != current["id"]:
            continue

        assignees = [
            a.get("name") or a.get("login")
            for a in (content.get("assignees", {}) or {}).get("nodes", [])
        ]
        identifier = (
            f"{content['repository']['nameWithOwner'].split('/')[-1]}#{content['number']}"
            if content.get("__typename") in ("Issue", "PullRequest")
            else "Draft"
        )
        items.append(
            {
                "identifier": identifier,
                "title": content.get("title", "(untitled)"),
                "url": content.get("url"),
                "status": status or "No status",
                "assignees": assignees or ["Unassigned"],
            }
        )

    return {
        "project_title": project["title"],
        "iteration_title": current["title"],
        "starts_at": current["start"],
        "ends_at": current["end"],
        "progress": min(current["days_elapsed"] / current["total_days"], 1.0),
        "items": items,
    }


# ── Discord embed builder ──────────────────────────────────────────────────

TRACKED_MEMBERS = ["akhilesh", "nishika", "prajna", "prashant", "ayush"]

STATE_DISPLAY_ORDER = ["Closed", "In Review", "In Progress", "To Do"]

STATE_NAME_MAP = {
    "closed": "Closed",
    "done": "Closed",
    "in review": "In Review",
    "in progress": "In Progress",
    "to do": "To Do",
    "todo": "To Do",
}

STATE_ICONS = {
    "Closed": "\U0001f7e2",
    "In Review": "\U0001f50d",
    "In Progress": "\U0001f7e1",
    "To Do": "⚪",
}

STATE_SORT_ORDER = {s: i for i, s in enumerate(STATE_DISPLAY_ORDER)}


def _display_state(state_name):
    return STATE_NAME_MAP.get(state_name.lower(), state_name)


def _matched_member(assignees):
    """Return the tracked-member key if any assignee matches, else None."""
    for assignee in assignees:
        lower = assignee.lower()
        for member in TRACKED_MEMBERS:
            if member in lower:
                return assignee
    return None


def _progress_bar(progress, length=10):
    filled = round(progress * length)
    empty = length - filled
    pct = round(progress * 100)
    if progress >= 0.70:
        filled_char = "🟩"  # green square
    elif progress >= 0.30:
        filled_char = "🟨"  # yellow square
    else:
        filled_char = "🟥"  # red square
    return f"{filled_char * filled}{'⬜' * empty} {pct}%"


def _progress_color(progress):
    if progress >= 0.70:
        return 0x2ECC71
    if progress >= 0.30:
        return 0xF1C40F
    return 0xE74C3C


def _truncate(text, max_len=40):
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _ordinal(day):
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _format_iteration_range(start_iso, end_iso):
    start = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end = datetime.strptime(end_iso, "%Y-%m-%d").date()
    return f"{_ordinal(start.day)} {start.strftime('%B')} - {_ordinal(end.day)} {end.strftime('%B')}"


def build_messages(iteration_data):
    all_items = iteration_data["items"]

    by_member = defaultdict(list)
    for item in all_items:
        match = _matched_member(item["assignees"])
        if match:
            by_member[match].append(item)

    tracked_items = [i for items in by_member.values() for i in items]
    state_totals = defaultdict(int)
    for i in tracked_items:
        state_totals[_display_state(i["status"])] += 1
    totals_line = ", ".join(
        f"{state_totals[s]} {s} {STATE_ICONS.get(s, '')}".strip()
        for s in STATE_DISPLAY_ORDER
    )

    total_count = len(tracked_items)
    closed_count = state_totals.get("Closed", 0)
    progress = closed_count / total_count if total_count else 0.0

    summary_lines = []
    for name in sorted(by_member.keys()):
        stats = defaultdict(int)
        for i in by_member[name]:
            stats[_display_state(i["status"])] += 1
        parts = []
        for s in STATE_DISPLAY_ORDER:
            if stats.get(s):
                parts.append(f"{stats[s]} {s.lower()}")
        for s, count in stats.items():
            if s not in STATE_DISPLAY_ORDER:
                parts.append(f"{count} {s.lower()}")
        first_name = name.split("@")[0].split()[0].title()
        summary_lines.append(f"**{first_name}**: {', '.join(parts)}")

    iteration_range = _format_iteration_range(
        iteration_data["starts_at"], iteration_data["ends_at"]
    )
    iteration_label = f"{iteration_data['iteration_title']} ({iteration_range})"

    summary_embed = {
        "title": f"Weekly Update: {iteration_label} - {iteration_data['project_title']}",
        "description": (
            f"{_progress_bar(progress)}\n"
            f"{totals_line}\n\n"
            + ("\n".join(summary_lines) if summary_lines else "_No tracked-member items in this iteration._")
        ),
        "color": _progress_color(progress),
    }

    payloads = [{"embeds": [summary_embed]}]

    for name in sorted(by_member.keys()):
        member_items = by_member[name]
        member_items.sort(
            key=lambda i: STATE_SORT_ORDER.get(_display_state(i["status"]), 99)
        )

        first_name = name.split("@")[0].split()[0].title()
        stats = defaultdict(int)
        for i in member_items:
            stats[_display_state(i["status"])] += 1
        subtitle_parts = []
        for s in STATE_DISPLAY_ORDER:
            if stats.get(s):
                subtitle_parts.append(f"{stats[s]} {s.lower()}")

        lines = [f"## {first_name} ({', '.join(subtitle_parts)})"]
        for i in member_items:
            display = _display_state(i["status"])
            icon = STATE_ICONS.get(display, "⚪")
            lines.append(f"{icon} `{i['identifier']}` {i['title']}")

        content = "\n".join(lines)
        if len(content) > 1990:
            content = content[:1987] + "..."
        payloads.append({"content": content})

    return payloads


# ── Discord sender ──────────────────────────────────────────────────────────


def send_discord_message(webhook_url, payload):
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()


# ── CLI ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Post GitHub Projects v2 iteration progress to Discord."
    )
    parser.add_argument("--org", type=str, help="GitHub org (overrides ORG)")
    parser.add_argument(
        "--project-number",
        type=int,
        help="GitHub project number (overrides PROJECT_NUMBER)",
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        help="Discord webhook URL (overrides KAAPI_DISCORD_WEBHOOK)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Discord payload to stdout instead of posting",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    org = args.org or os.environ.get("ORG")
    project_number_raw = (
        args.project_number or os.environ.get("PROJECT_NUMBER")
    )
    webhook_url = args.webhook_url or os.environ.get("KAAPI_DISCORD_WEBHOOK")

    if not token:
        print("Error: GITHUB_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)
    if not org:
        print("Error: ORG is not set and --org not provided.", file=sys.stderr)
        sys.exit(1)
    if not project_number_raw:
        print(
            "Error: PROJECT_NUMBER is not set and --project-number not provided.",
            file=sys.stderr,
        )
        sys.exit(1)
    project_number = int(project_number_raw)

    if not webhook_url and not args.dry_run:
        print(
            "Error: KAAPI_DISCORD_WEBHOOK is not set and --webhook-url not provided.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        iteration_data = fetch_current_iteration(org, project_number, token)
    except requests.HTTPError as e:
        print(f"Error calling GitHub API: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"GitHub API error: {e}", file=sys.stderr)
        sys.exit(1)

    if iteration_data is None:
        print(
            f"No current iteration found for {org}/projects/{project_number}. Nothing to report."
        )
        sys.exit(0)

    payloads = build_messages(iteration_data)

    if args.dry_run:
        print(json.dumps(payloads, indent=2))
    else:
        try:
            for payload in payloads:
                send_discord_message(webhook_url, payload)
            print("Discord update posted successfully.")
        except requests.HTTPError as e:
            print(f"Error posting to Discord: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
