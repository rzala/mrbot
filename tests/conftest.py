"""Shared pytest fixtures for MR Notify Bot tests."""

import os
import sqlite3
import pytest
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _set_env(tmp_path, monkeypatch):
    """Set required environment variables for tests."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test-token")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test-token")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("BOT_ADMINS", "U_ADMIN_1,U_ADMIN_2")


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Initialize a fresh test database and return its path."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    from bot import config, database
    # Reset thread-local to get a fresh connection
    database._local = __import__("threading").local()
    config.setup()
    database.init_db()
    return db_path


@pytest.fixture
def mock_slack_client():
    """Return a mock Slack WebClient."""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True}
    return client


@pytest.fixture
def sample_mr():
    """Return a sample GitLab MR dict."""
    return {
        "iid": 42,
        "title": "Add new feature",
        "web_url": "https://gitlab.example.com/mygroup/myproject/-/merge_requests/42",
        "author": {"name": "Alice"},
        "created_at": "2026-03-10T10:00:00Z",
        "updated_at": "2026-03-11T14:00:00Z",
        "labels": ["bug", "urgent"],
        "draft": False,
        "target_branch": "main",
        "state": "opened",
    }


@pytest.fixture
def sample_subscription():
    """Return a sample subscription dict."""
    return {
        "id": 1,
        "gitlab_project_path": "mygroup/myproject",
        "gitlab_project_id": 123,
        "slack_channel_id": "C_TEST",
        "slack_user_id": "U_USER_1",
        "delivery_target": "channel",
        "delivery_channel_id": "C_TEST",
        "mode": "digest",
        "schedule": "0 9 * * 1-5",
        "poll_interval": "*/5 * * * *",
        "include_drafts": 0,
        "filter_labels": None,
        "filter_branch": None,
        "lifecycle_enabled": 1,
        "notify_approvals": 1,
        "suppress_empty": 0,
        "status": "active",
        "pause_reason": None,
        "consecutive_failures": 0,
        "created_by": "U_USER_1",
        "created_at": "2026-03-10T10:00:00+00:00",
        "updated_at": "2026-03-10T10:00:00+00:00",
    }
