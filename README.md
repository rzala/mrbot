# MR Notify

Slack bot that monitors GitLab merge requests on a self-hosted instance and delivers notifications to Slack channels or DMs.

Uses **Socket Mode** (outbound WebSocket) — no public URL or inbound ports required.

## Features

- **Poll-based monitoring** — checks GitLab for open MRs on a configurable interval (default: every 5 minutes)
- **Scheduled digests** — posts a full summary of all open MRs on a cron schedule (default: weekdays 9 AM)
- **Instant notifications** — notifies about new or updated MRs as they're detected each poll, with deduplication
- **Approval alerts** — notifies when an MR receives a new approval, with who approved (on by default, toggle with `--approvals` / `--no-approvals`)
- **Lifecycle alerts** — detects when MRs are merged or closed, and by whom (on by default, toggle with `--lifecycle` / `--no-lifecycle`)
- **Filters** — filter by labels (OR), target branch, and draft status (drafts excluded by default)
- **Personal DM subscriptions** — `--dm` flag for personal notifications without channel spam
- **On-demand checks** — `/mr-check` to see open MRs immediately without waiting for the schedule
- **Auto-pause** — subscriptions pause after 5 consecutive failures, with notification to the creator
- **Self-hosted GitLab** — works with internal CA certificates (auto-detected or via `REQUESTS_CA_BUNDLE`)

## How Notifications Work

The bot **polls** GitLab on a cron schedule — it is not webhook-driven. Every subscription automatically delivers **both** instant per-MR notifications **and** scheduled digest summaries.

### What You Get

| Notification | Trigger | Content |
|-------------|---------|---------|
| **New MR** | Poll detects a new open MR | MR title, author, branch, labels, approval status |
| **Updated MR** | Poll detects `updated_at` changed | Same as new MR |
| **Approval** | Poll detects new approval(s) | MR title, approval count, who approved |
| **Merged/Closed** | MR disappears from open list | MR title, state, who merged/closed |
| **Digest** | Fires on digest schedule | Full list of all open MRs with approval status |

### Two Timers Per Subscription

Each subscription has two independent schedules:

| Timer | Purpose | Default | Config flag |
|-------|---------|---------|-------------|
| **Poll interval** | How often to check for new/updated/merged MRs | `*/5 * * * *` (every 5 min) | `--poll-interval` |
| **Digest schedule** | When to post the full MR summary | `0 9 * * 1-5` (weekdays 9 AM) | `--schedule` |

The **poll job** runs frequently in the background — it tracks MR state, sends instant notifications for new/updated MRs, detects approvals, and catches merges/closures. The **digest job** fires on the digest schedule and posts a full summary of all open MRs (with approval status).

### Where Notifications Are Delivered

By default, notifications go to the Slack channel where `/mr-subscribe` was run. Individual users can override this with `--dm` to receive notifications in their personal MR Notify app DM instead:

```bash
# Subscribe with DM delivery — only you see the notifications
/mr-subscribe mygroup/myproject --dm
```

The global default can be changed by an admin:

| Policy | Delivery target |
|--------|----------------|
| **channel** (default) | The Slack channel where `/mr-subscribe` was run |
| **app** | The subscribing user's DM with the MR Notify app |

Use `/mr-admin --channel-policy <channel\|app>` to change the default for new subscriptions.
Use `/mr-admin --channel-policy <channel\|app> <repo>` to update an existing subscription's delivery.

### Schedule Presets

These presets work for both `--schedule` and `--poll-interval`:

| Preset | Cron | Frequency |
|--------|------|-----------|
| `5min` | `*/5 * * * *` | Every 5 minutes |
| `15min` | `*/15 * * * *` | Every 15 minutes |
| `30min` | `*/30 * * * *` | Every 30 minutes |
| `hourly` | `0 * * * *` | Every hour |
| `morning` | `0 9 * * 1-5` | Weekdays at 9 AM |
| `twice-daily` | `0 9,14 * * 1-5` | Weekdays at 9 AM and 2 PM |
| `custom "<cron>"` | any | Your own cron expression |

### Defaults

| Setting | Default |
|---------|---------|
| Digest schedule | Weekdays at 9 AM (`0 9 * * 1-5`) |
| Poll interval | Every 5 minutes (`*/5 * * * *`) |
| Drafts | excluded |
| Lifecycle alerts | enabled |
| Approval alerts | enabled |
| Suppress empty | off (posts "all clear" when no MRs) |
| Channel policy | `channel` (notifications go to the channel) |

## Slash Commands

| Command | Description |
|---------|-------------|
| `/mr-subscribe <repo> [--dm]` | Subscribe to MR notifications (`--dm` = deliver to your DM only) |
| `/mr-unsubscribe <repo> [--dm]` | Remove a subscription (`--dm` = your DM subscription) |
| `/mr-list` | List subscriptions and global settings |
| `/mr-config <repo> [--dm] [options]` | Configure schedule, filters, alerts (`--dm` = your DM subscription) |
| `/mr-check [repo] [--dm]` | Immediately check for open MRs (with approval status) |
| `/mr-admin [options]` | Bot administration (admins only) |
| `/mr-help` | Show help with commands and config options |

Repo can be SSH URL, HTTPS URL, or bare path (e.g. `mygroup/myproject`).

### Config Options

| Option | Values | Description |
|--------|--------|-------------|
| `--schedule <preset\|cron>` | `morning`, `twice-daily`, `hourly`, `custom "<cron>"` | When to post the digest summary |
| `--poll-interval <preset\|cron>` | `5min`, `15min`, `30min`, `hourly`, `custom "<cron>"` | How often to check GitLab for changes |
| `--include-drafts` | flag | Include draft/WIP merge requests in notifications |
| `--exclude-drafts` | flag | Exclude draft MRs (default) |
| `--labels "<csv>"` | comma-separated | Only show MRs matching at least one label (OR logic) |
| `--branch <name>` | branch name | Only show MRs targeting this branch |
| `--lifecycle` / `--no-lifecycle` | flag | Toggle merge/close notifications (default: on) |
| `--approvals` / `--no-approvals` | flag | Toggle approval notifications (default: on) |
| `--suppress-empty` / `--show-empty` | flag | Toggle "all clear" messages (default: shown) |
| `--dm` | flag | Target your personal DM subscription (for subscribe, config, check, unsubscribe) |
| `--resume` | flag | Resume a paused subscription |

#### Examples

```bash
# Change digest to twice daily, keep polling at 5min
/mr-config mygroup/myproject --schedule twice-daily

# Poll every 15 minutes instead of 5
/mr-config mygroup/myproject --poll-interval 15min

# Only show MRs labeled "bug" or "hotfix" targeting main
/mr-config mygroup/myproject --labels "bug,hotfix" --branch main

# Include draft MRs and suppress empty digests
/mr-config mygroup/myproject --include-drafts --suppress-empty

# Disable approval notifications for a noisy repo
/mr-config mygroup/myproject --no-approvals

# Use a custom cron: digest every weekday at 8:30 AM
/mr-config mygroup/myproject --schedule custom "30 8 * * 1-5"

# Resume a subscription that was auto-paused after failures
/mr-config mygroup/myproject --resume

# Personal DM subscription — only you get notified
/mr-subscribe mygroup/myproject --dm

# Configure your DM subscription separately from the channel one
/mr-config mygroup/myproject --dm --poll-interval 15min

# Unsubscribe your personal DM subscription
/mr-unsubscribe mygroup/myproject --dm
```

## Quick Start

### 1. Create the Slack App

Go to [api.slack.com/apps](https://api.slack.com/apps) > **Create New App** > **From an app manifest** > paste `slack-app-manifest.json` and create.

Then:
1. **Basic Information** > App-Level Tokens > **Generate Token** with `connections:write` scope — this is `SLACK_APP_TOKEN`
2. **OAuth & Permissions** > **Install to Workspace** > copy Bot User OAuth Token — this is `SLACK_BOT_TOKEN`

### 2. Create a GitLab Token

GitLab > User Settings > Access Tokens > create with `read_api` scope.

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` with your tokens:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SLACK_BOT_TOKEN` | Yes | — | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | — | App-Level Token (`xapp-...`) |
| `GITLAB_TOKEN` | Yes | — | GitLab personal access token |
| `GITLAB_URL` | No | `https://gitlab.example.com` | GitLab instance base URL |
| `REQUESTS_CA_BUNDLE` | No | auto-detects `ca-bundle.crt` | Path to CA bundle for internal certs |
| `DATABASE_PATH` | No | `./data/mr-notify.db` | SQLite database path |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `DEFAULT_SCHEDULE` | No | `0 9 * * 1-5` | Default digest schedule |
| `DEFAULT_POLL_INTERVAL` | No | `*/5 * * * *` | Default poll interval for change detection |
| `DEFAULT_MODE` | No | `digest` | Default mode (`digest` or `realtime`) |
| `BOT_ADMINS` | No | — | Comma-separated Slack user IDs |

### 4. Run

**Docker (recommended):**

```bash
docker compose up -d
docker compose logs -f mr-notify
```

**Local:**

```bash
pip install -r requirements.txt
python -m bot
```

### 5. Verify

1. Invite the bot to a channel: `/invite @MR Notify`
2. `/mr-subscribe mygroup/myproject`
3. `/mr-check` — see open MRs immediately
4. `/mr-list` — verify subscription is active

## Architecture

```
bot/
├── app.py            # Slack Bolt app, slash command handlers, entry point
├── config.py         # Environment config, .env loader, logging, token redaction
├── database.py       # SQLite with WAL mode, per-thread connections
├── gitlab_client.py  # GitLab REST API v4 client with typed exceptions
├── scheduler.py      # APScheduler jobs, digest/realtime/lifecycle logic
├── formatters.py     # Slack Block Kit message builders
├── parsers.py        # URL and config option parsing
└── __main__.py       # python -m bot entry point
```

- **SQLite** with WAL mode for the app database, separate SQLite for APScheduler job store
- **Socket Mode** — no public endpoint required, connects outbound via WebSocket
- **Structured JSON logging** with automatic GitLab token redaction
- **Graceful degradation** — GitLab Approvals API detected at runtime (works on CE and Premium)
- **Internal CA support** — auto-detects `ca-bundle.crt` in working directory, or set `REQUESTS_CA_BUNDLE`

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| SSL certificate verify failed | Internal CA not trusted | Place your CA bundle in project root as `ca-bundle.crt` or set `REQUESTS_CA_BUNDLE` |
| "Project not found" | Token lacks project access | Check token scope and project visibility |
| Bot doesn't respond | Socket Mode not connected | Verify `SLACK_APP_TOKEN` and Socket Mode is enabled |
| No scheduled notifications | Scheduler not running | Check logs for scheduler startup message |
| "channel_not_found" in logs | Bot not in the channel | `/invite @MR Notify` in the channel |
| Subscription auto-paused | 5 consecutive check failures | Fix the issue, then `/mr-config <repo> --resume` |
