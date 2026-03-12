# MR Notify — Development Guide

## Tech Stack

- Python 3.11+
- slack-bolt (Slack Bolt SDK with Socket Mode)
- requests (GitLab REST API v4 client)
- APScheduler 3.10+ (cron-based job scheduling)
- SQLite with WAL mode (app database)
- SQLAlchemy (APScheduler job store only)

## Project Structure

```
bot/
├── app.py            # Slack Bolt app, slash command handlers, entry point
├── config.py         # Environment config, .env loader, logging
├── database.py       # SQLite schema, CRUD, per-thread connections
├── gitlab_client.py  # GitLab REST API v4 wrapper
├── scheduler.py      # APScheduler jobs (poll + digest)
├── formatters.py     # Slack Block Kit message builders
├── parsers.py        # URL and config option parsing
├── user_cache.py     # GitLab → Slack user name resolution
└── __main__.py       # python -m bot entry point
tests/
└── conftest.py       # Shared pytest fixtures
```

## Commands

```bash
# Run locally
python -m bot

# Run tests
pytest

# Lint
ruff check .
```

## Key Patterns

- App creation is deferred to `main()` — not at module level (avoids import-time crashes before env is loaded)
- Two APScheduler jobs per subscription: `poll_{id}` (frequent) and `digest_{id}` (scheduled)
- First poll silently seeds state — no flood of historical MRs on cold start
- `_deliver_notification()` uses Slack attachments with colored sidebars
- GitLab author names are resolved to Slack @mentions via `user_cache`
- Approvals API gracefully degrades (works on CE and Premium)
