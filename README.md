# scrum-master

Posts a weekly iteration update from a GitHub Projects v2 board to Discord. Runs every Friday 5 PM IST via GitHub Actions, with a manual dry-run option.

## What it does

1. Reads the **current iteration** from the project's iteration field (the one in use when the script runs).
2. Pulls every item in that iteration and filters to the tracked members.
3. Posts to Discord:
   - **Summary embed** — iteration title + date range, color-coded progress bar, overall status breakdown, per-member one-line stats.
   - **One message per member** — a Markdown heading + a list of their items (each with a status-colored dot, `repo#number`, title, and age since the issue/PR was created).

## Example output

```
Weekly Update: Iteration 34 (12th April - 25th April) - Kaapi-dev
🟩🟩🟩🟩🟩🟩🟩⬜⬜⬜ 79%
9 Closed 🟢, 1 In Review 🔍, 6 In Progress 🟡, 7 To Do ⚪

Akhilesh: 2 closed, 3 to do
Ayush: 1 closed, 1 in progress, 1 to do
...

## Akhilesh (2 closed, 3 to do)
🟢 kaapi-backend#753 Security: Resolve vulnerabilities in repo (9 days)
🟢 kaapi-frontend#39 Evaluation UI: Show cost (12 days)
⚪ kaapi-backend#693 Evaluation: Clear error message (4 days)
...
```

## Setup

### 1. GitHub PAT

You need a token that can read org-level Projects v2. The workflow's built-in `GITHUB_TOKEN` is scoped to the repo and can't read org projects.

**Classic PAT** — https://github.com/settings/tokens

- Scopes: `read:project`, `read:org`
- If the org enforces SAML SSO, click **Configure SSO** on the token and authorize it for `ProjectTech4DevAI`.

**Fine-grained PAT** (preferred) — https://github.com/settings/personal-access-tokens/new

- Resource owner: `ProjectTech4DevAI`
- Organization permissions → **Projects: Read-only**
- An org admin may need to approve it.

### 2. Discord webhook

In the target Discord channel: **Edit Channel → Integrations → Webhooks → New Webhook**. Copy the URL.

### 3. Local env

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.template .env
```

Fill in `.env`:

```
GITHUB_TOKEN=           # PAT from step 1
ORG=ProjectTech4DevAI
PROJECT_NUMBER=3
KAAPI_DISCORD_WEBHOOK=  # webhook URL from step 2
```

### 4. Test locally

```bash
# Print the payload, don't post
python kaapi_weekly_update.py --dry-run

# Post for real
python kaapi_weekly_update.py
```

### 5. Repo secrets

For the scheduled workflow, set these under **Settings → Secrets and variables → Actions**:

| Name | Value |
|---|---|
| `PROJECTS_READ_TOKEN` | Your PAT |
| `ORG` | `ProjectTech4DevAI` |
| `PROJECT_NUMBER` | `3` |
| `KAAPI_DISCORD_WEBHOOK` | Webhook URL |

## Schedule

`.github/workflows/weekly_update.yml` runs on cron `30 3 * * 1,5` — every Monday and Friday at 03:30 UTC = **9:00 AM IST** (one hour before the 10 AM standup).

Manual runs from the Actions tab (**Run workflow**) accept a `dry_run` input — set it to `true` to print the payload in the job logs without posting.

GitHub Actions cron drifts by 5–15 minutes during peak load and occasionally skips runs under platform saturation. If you need exact timing, use an external scheduler that calls `workflow_dispatch` via the API.