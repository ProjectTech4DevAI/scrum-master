#!/usr/bin/env python3
"""
Posts the open (non-draft) PRs of the Kaapi repos to Discord, oldest first,
with the number of days each has been open. Use --dry-run to print instead.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

from kaapi_weekly_update import _execute_graphql, send_discord_message

REPOS = ["kaapi-backend", "kaapi-frontend", "kaapi-guardrails"]
BOT_NAME = "Kaapi Barista"  # overrides the webhook's default name for this flow
COFFEE_BROWN = 0x6F4E37  # embed sidebar color
# Twemoji "hot beverage" (CC-BY 4.0)
AVATAR_URL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/2615.png"

# ponytail: single page of 100; paginate if the team ever has >100 open PRs
OPEN_PRS_QUERY = """
query OpenPRs($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    nodes {
      ... on PullRequest {
        number
        title
        url
        createdAt
        author { login }
        repository { name }
      }
    }
  }
}
"""


def fetch_open_prs(org, token):
    repo_filter = " ".join(f"repo:{org}/{r}" for r in REPOS)
    data = _execute_graphql(
        OPEN_PRS_QUERY, {"q": f"{repo_filter} is:pr is:open draft:false"}, token
    )
    return [n for n in data["search"]["nodes"] if n]


def _age_icon(days):
    if days > 5:
        return "\U0001f480"  # dead
    if days >= 3:
        return "\U0001f375"  # cooling
    if days >= 1:
        return "☕"  # brewing
    return "\U0001fad8"  # fresh beans


AGE_ICONS = [_age_icon(d) for d in (6, 3, 1, 0)]  # oldest bucket first
LEGEND = "\U0001fad8 <1d · ☕ 1–2d · \U0001f375 3–5d · \U0001f480 6d+"
OPEN_PRS_FILTER = "/pulls?q=is%3Apr+is%3Aopen+draft%3Afalse"


def build_messages(prs, now=None, mention=None):
    now = now or datetime.now(timezone.utc)
    for p in prs:
        p["days"] = (now - datetime.fromisoformat(p["createdAt"].replace("Z", "+00:00"))).days
    icons = [_age_icon(p["days"]) for p in prs]
    lines = [f"# Open PRs ({len(prs)})"]
    if prs:
        lines.append(" · ".join(f"{i} {icons.count(i)}" for i in AGE_ICONS))
    for repo in REPOS:
        repo_prs = sorted(
            (p for p in prs if p["repository"]["name"] == repo),
            key=lambda p: p["createdAt"],
        )
        if not repo_prs:
            continue
        repo_url = repo_prs[0]["url"].rsplit("/pull/", 1)[0] + OPEN_PRS_FILTER
        lines.append(f"## [{repo} ({len(repo_prs)})](<{repo_url}>)")
        for p in repo_prs:
            author = (p.get("author") or {}).get("login", "ghost")
            lines.append(
                f"{_age_icon(p['days'])} [`#{p['number']}`](<{p['url']}>) {p['title']}"
                f" — {author} (**{p['days']}d**)"
            )
    if not prs:
        lines.append("_No open PRs._ \U0001f389")

    messages = embed_messages(lines, BOT_NAME, COFFEE_BROWN, LEGEND, mention)
    for m in messages:
        m["avatar_url"] = AVATAR_URL
    return messages


def embed_messages(lines, username, color, footer, mention=None):
    """Pack lines into Discord embed payloads (shared with kaapi_iteration)."""
    # Embeds (needed for the color bar) cap a description at 4096 chars;
    # pack whole lines into chunks.
    payloads, chunk = [], ""
    for line in lines:
        line = line[:4000]
        if chunk and len(chunk) + 1 + len(line) > 4000:
            payloads.append(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    payloads.append(chunk)
    messages = [
        {"username": username, "embeds": [{"description": c, "color": color}]}
        for c in payloads
    ]
    messages[-1]["embeds"][0]["footer"] = {"text": footer}
    if mention:
        # Mentions only ping from message content, never from inside an embed.
        messages[0]["content"] = mention
    return messages


def _self_check():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    prs = [
        {
            "number": n,
            "title": "x" * 150,
            "url": f"https://github.com/o/kaapi-backend/pull/{n}",
            "createdAt": f"2026-01-{n:02d}T00:00:00Z",
            "author": {"login": "dev"},
            "repository": {"name": "kaapi-backend"},
        }
        for n in list(range(9, 0, -1)) * 3
    ]
    payloads = build_messages(prs, now)
    descs = [p["embeds"][0]["description"] for p in payloads]
    text = "\n".join(descs)
    assert len(payloads) > 1 and all(len(d) <= 4096 for d in descs)
    assert text.index("#1`") < text.index("#9`"), "oldest first"
    assert "(**9d**)" in text and "(**1d**)" in text
    assert "\U0001f480 12 · \U0001f375 9 · ☕ 6 · \U0001fad8 0" in descs[0], "tally"
    assert "footer" in payloads[-1]["embeds"][0] and "footer" not in payloads[0]["embeds"][0]
    assert [_age_icon(d) for d in (0, 1, 2, 3, 5, 6)] == ["\U0001fad8", "☕", "☕", "\U0001f375", "\U0001f375", "\U0001f480"]
    assert build_messages(prs, now, "<@&1>")[0]["content"] == "<@&1>"
    assert "content" not in payloads[0]
    assert "No open PRs" in build_messages([], now)[0]["embeds"][0]["description"]
    print("self-check ok")


def main():
    parser = argparse.ArgumentParser(description="Post open Kaapi PRs to Discord.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload instead of posting")
    parser.add_argument("--self-check", action="store_true", help="Run built-in assertions and exit")
    args = parser.parse_args()

    if args.self_check:
        return _self_check()

    token = os.environ.get("GITHUB_TOKEN")
    org = os.environ.get("ORG")
    webhook_url = os.environ.get("KAAPI_DISCORD_WEBHOOK")
    if not token or not org:
        sys.exit("Error: GITHUB_TOKEN and ORG must be set.")
    if not webhook_url and not args.dry_run:
        sys.exit("Error: KAAPI_DISCORD_WEBHOOK is not set.")

    try:
        payloads = build_messages(
            fetch_open_prs(org, token), mention=os.environ.get("DISCORD_MENTION")
        )
    except (requests.HTTPError, RuntimeError) as e:
        sys.exit(f"GitHub API error: {e}")

    if args.dry_run:
        print(json.dumps(payloads, indent=2))
        return
    try:
        for payload in payloads:
            send_discord_message(webhook_url, payload)
    except requests.HTTPError as e:
        sys.exit(f"Error posting to Discord: {e}")
    print("Open PRs posted successfully.")


if __name__ == "__main__":
    main()
