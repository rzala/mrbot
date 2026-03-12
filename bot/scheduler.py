"""APScheduler setup, job management, poll execution, and maintenance jobs."""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger

from bot import config, database

logger = logging.getLogger(__name__)

SCHEDULE_PRESETS = {
    "1min": "*/1 * * * *",
    "2min": "*/2 * * * *",
    "5min": "*/5 * * * *",
    "15min": "*/15 * * * *",
    "30min": "*/30 * * * *",
    "hourly": "0 * * * *",
    "morning": "0 9 * * 1-5",
    "twice-daily": "0 9,14 * * 1-5",
}

_scheduler: BackgroundScheduler | None = None
_token_invalid = False


def _get_jobs_db_url() -> str:
    """Derive the APScheduler jobs DB path from the app DB path."""
    db_dir = os.path.dirname(config.DATABASE_PATH) or "."
    return f"sqlite:///{db_dir}/apscheduler-jobs.db"


def get_scheduler() -> BackgroundScheduler:
    """Return the global scheduler instance, creating if needed."""
    global _scheduler
    if _scheduler is None:
        jobstores = {"default": SQLAlchemyJobStore(url=_get_jobs_db_url())}
        job_defaults = {"coalesce": True, "misfire_grace_time": None}
        _scheduler = BackgroundScheduler(
            jobstores=jobstores, job_defaults=job_defaults
        )
    return _scheduler


def resolve_schedule(schedule_input: str) -> str:
    """Resolve a preset name or custom cron expression to a cron string.

    Raises ValueError if the cron expression is invalid.
    """
    if schedule_input in SCHEDULE_PRESETS:
        return SCHEDULE_PRESETS[schedule_input]
    # Handle 'custom "expr"' format
    if schedule_input.startswith("custom "):
        cron_expr = schedule_input[7:].strip().strip('"').strip("'")
    else:
        cron_expr = schedule_input
    # Validate by parsing
    CronTrigger.from_crontab(cron_expr)
    return cron_expr


def add_subscription_jobs(subscription_id: int, schedule: str, poll_interval: str, mode: str) -> None:
    """Register poll and digest jobs for a subscription.

    Poll job: runs at poll_interval, handles state tracking + lifecycle + realtime.
    Digest job: runs at schedule, posts full MR summary (only in digest mode).
    """
    scheduler = get_scheduler()

    # Always register the poll job
    poll_trigger = CronTrigger.from_crontab(poll_interval)
    scheduler.add_job(
        execute_poll,
        trigger=poll_trigger,
        args=[subscription_id],
        id=f"poll_{subscription_id}",
        replace_existing=True,
    )
    logger.info(
        "Poll job registered",
        extra={"subscription_id": subscription_id, "poll_interval": poll_interval},
    )

    # Always register digest job for scheduled summaries
    digest_trigger = CronTrigger.from_crontab(schedule)
    scheduler.add_job(
        execute_digest,
        trigger=digest_trigger,
        args=[subscription_id],
        id=f"digest_{subscription_id}",
        replace_existing=True,
    )
    logger.info(
        "Digest job registered",
        extra={"subscription_id": subscription_id, "schedule": schedule},
    )


def remove_subscription_jobs(subscription_id: int) -> None:
    """Remove all jobs for a subscription."""
    _remove_job_safe(f"poll_{subscription_id}")
    _remove_job_safe(f"digest_{subscription_id}")
    logger.info("Jobs removed", extra={"subscription_id": subscription_id})


def _remove_job_safe(job_id: str) -> None:
    """Remove a job, silently ignoring if it doesn't exist."""
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


def reschedule_subscription_jobs(
    subscription_id: int,
    schedule: str | None = None,
    poll_interval: str | None = None,
    mode: str | None = None,
) -> None:
    """Reschedule jobs after config changes."""
    scheduler = get_scheduler()

    if poll_interval:
        try:
            trigger = CronTrigger.from_crontab(poll_interval)
            scheduler.reschedule_job(f"poll_{subscription_id}", trigger=trigger)
            logger.info(
                "Poll job rescheduled",
                extra={"subscription_id": subscription_id, "poll_interval": poll_interval},
            )
        except Exception:
            pass

    if schedule:
        try:
            trigger = CronTrigger.from_crontab(schedule)
            scheduler.reschedule_job(f"digest_{subscription_id}", trigger=trigger)
            logger.info(
                "Digest job rescheduled",
                extra={"subscription_id": subscription_id, "schedule": schedule},
            )
        except Exception:
            pass


def start_scheduler(slack_client) -> None:
    """Start the scheduler and rebuild jobs from active subscriptions."""
    from bot import user_cache

    global _token_invalid
    _token_invalid = False

    scheduler = get_scheduler()
    _set_slack_client(slack_client)

    # Build Slack user name → ID cache for @mention resolution
    user_cache.refresh(slack_client)

    scheduler.start()
    logger.info("Scheduler started")

    # Rebuild jobs from active subscriptions
    subs = database.get_active_subscriptions()
    for sub in subs:
        add_subscription_jobs(
            sub["id"], sub["schedule"], sub["poll_interval"], sub["mode"]
        )
    logger.info("Jobs rebuilt from database", extra={"count": len(subs)})

    # Register maintenance pruning job (daily at 3:00 AM)
    scheduler.add_job(
        _prune_notification_state,
        trigger=CronTrigger.from_crontab("0 3 * * *"),
        id="maintenance_prune",
        replace_existing=True,
    )


# --- Backwards compat shims for app.py ---


def add_subscription_job(subscription_id: int, cron_expr: str) -> None:
    """Legacy wrapper — registers jobs using subscription's poll_interval."""
    sub = database.get_subscription_by_id(subscription_id)
    if sub:
        add_subscription_jobs(subscription_id, sub["schedule"], sub["poll_interval"], sub["mode"])


def remove_subscription_job(subscription_id: int) -> None:
    """Legacy wrapper."""
    remove_subscription_jobs(subscription_id)


def reschedule_subscription_job(subscription_id: int, cron_expr: str) -> None:
    """Legacy wrapper."""
    reschedule_subscription_jobs(subscription_id, schedule=cron_expr)


# --- Slack Client Storage ---

_slack_client = None


def _set_slack_client(client) -> None:
    global _slack_client
    _slack_client = client


def _get_slack_client():
    return _slack_client


# --- Poll Job (frequent — state tracking, lifecycle, realtime) ---


def execute_poll(subscription_id: int) -> None:
    """Frequent poll: update MR state, send lifecycle + realtime notifications."""
    from bot import gitlab_client as gl_module
    from bot.gitlab_client import (
        AuthenticationError,
        GitLabUnavailableError,
        ProjectNotFoundError,
        RateLimitError,
    )

    global _token_invalid
    if _token_invalid:
        return

    sub = database.get_subscription_by_id(subscription_id)
    if not sub or sub["status"] != "active":
        return

    logger.info("Poll started", extra={"subscription_id": subscription_id})

    _poll_approval_cache.clear()
    client = gl_module.GitLabClient()
    try:
        # Resolve project ID
        project_id = sub["gitlab_project_id"]
        if project_id is None:
            project = client.get_project_by_path(sub["gitlab_project_path"])
            project_id = project["id"]
            database.update_subscription(subscription_id, gitlab_project_id=project_id)

        # Fetch open MRs and apply filters
        mrs = client.get_open_merge_requests(project_id)
        mrs = apply_filters(mrs, sub)

        # Send per-MR notifications for new/updated MRs
        _send_realtime_notifications(sub, mrs, client)

        # Check for approval changes
        if sub.get("notify_approvals"):
            _check_approvals(sub, mrs, client)

        # Always track state for all open MRs (needed for lifecycle)
        for mr in mrs:
            approval_count = _get_cached_approval_count(sub, mr, client)
            database.upsert_notification_state(
                sub["id"], mr["iid"], "open", mr["updated_at"],
                approval_count=approval_count,
            )

        # Lifecycle detection
        if sub["lifecycle_enabled"]:
            _execute_lifecycle(sub, mrs, client)

        # Reset failure counter on success
        if sub["consecutive_failures"] > 0:
            database.update_subscription(subscription_id, consecutive_failures=0)

        logger.info(
            "Poll completed",
            extra={"subscription_id": subscription_id, "mr_count": len(mrs)},
        )

    except AuthenticationError:
        _token_invalid = True
        scheduler = get_scheduler()
        scheduler.pause()
        logger.error(
            "GitLab authentication failed — all checks paused. "
            "Replace GITLAB_TOKEN and restart the container."
        )

    except RateLimitError as e:
        logger.warning(
            "Rate limited, skipping poll",
            extra={"subscription_id": subscription_id, "retry_after": e.retry_after},
        )

    except (ProjectNotFoundError, GitLabUnavailableError) as e:
        _handle_check_failure(subscription_id, str(e))

    except Exception as e:
        logger.error(
            "Unexpected error during poll",
            extra={"subscription_id": subscription_id, "error": str(e)},
        )
        _handle_check_failure(subscription_id, str(e))


# --- Digest Job (scheduled — full MR summary) ---


def execute_digest(subscription_id: int) -> None:
    """Scheduled digest: post full MR summary."""
    from bot import formatters, gitlab_client as gl_module
    from bot.gitlab_client import (
        AuthenticationError,
        GitLabUnavailableError,
        ProjectNotFoundError,
        RateLimitError,
    )

    global _token_invalid
    if _token_invalid:
        return

    sub = database.get_subscription_by_id(subscription_id)
    if not sub or sub["status"] != "active":
        return

    logger.info("Digest started", extra={"subscription_id": subscription_id})

    client = gl_module.GitLabClient()
    try:
        project_id = sub["gitlab_project_id"]
        if project_id is None:
            project = client.get_project_by_path(sub["gitlab_project_path"])
            project_id = project["id"]
            database.update_subscription(subscription_id, gitlab_project_id=project_id)

        mrs = client.get_open_merge_requests(project_id)
        mrs = apply_filters(mrs, sub)

        # Fetch approval info for each MR
        approval_data = {}
        for mr in mrs:
            info = client.get_mr_approvals(project_id, mr["iid"])
            if info:
                approval_data[mr["iid"]] = info

        project_web_url = f"{config.GITLAB_URL}/{sub['gitlab_project_path']}"
        attachments = formatters.format_digest(
            sub["gitlab_project_path"],
            mrs,
            len(mrs),
            project_web_url,
            bool(sub["suppress_empty"]),
            sub,
            approval_data=approval_data,
        )

        # Include recently resolved MRs
        states = database.get_notification_states(sub["id"])
        resolved_mrs = []
        for iid, state in states.items():
            if state["mr_state"] in ("merged", "closed") and state.get("resolved_at"):
                resolved_mrs.append({
                    "mr": {"title": f"MR !{iid}", "web_url": "", "iid": iid},
                    "new_state": state["mr_state"],
                    "merge_user": None,
                })

        if attachments is not None:
            if resolved_mrs:
                attachments.extend(formatters.format_digest_resolved_section(resolved_mrs))
            _deliver_notification(sub, attachments)

        logger.info(
            "Digest completed",
            extra={"subscription_id": subscription_id, "mr_count": len(mrs)},
        )

    except AuthenticationError:
        _token_invalid = True
        scheduler = get_scheduler()
        scheduler.pause()
        logger.error(
            "GitLab authentication failed — all checks paused. "
            "Replace GITLAB_TOKEN and restart the container."
        )

    except RateLimitError as e:
        logger.warning(
            "Rate limited, skipping digest",
            extra={"subscription_id": subscription_id, "retry_after": e.retry_after},
        )

    except (ProjectNotFoundError, GitLabUnavailableError) as e:
        _handle_check_failure(subscription_id, str(e))

    except Exception as e:
        logger.error(
            "Unexpected error during digest",
            extra={"subscription_id": subscription_id, "error": str(e)},
        )
        _handle_check_failure(subscription_id, str(e))


# --- Realtime Notifications ---


def _send_realtime_notifications(sub: dict, mrs: list[dict], client) -> None:
    """Send individual notifications for new or updated MRs.

    On the first poll (no prior state exists), silently seeds state without
    sending notifications to avoid flooding the channel with historical MRs.
    """
    from bot import formatters

    states = database.get_notification_states(sub["id"])

    # First poll for this subscription — seed state, skip notifications
    if not states:
        logger.info(
            "First poll — seeding state without notifications",
            extra={"subscription_id": sub["id"], "mr_count": len(mrs)},
        )
        return

    for mr in mrs:
        iid = mr["iid"]
        existing = states.get(iid)
        is_new = existing is None
        is_updated = (
            existing is not None
            and existing["mr_state"] == "open"
            and mr["updated_at"] != existing["mr_updated_at"]
        )
        if is_new or is_updated:
            approval_info = client.get_mr_approvals(
                sub["gitlab_project_id"], iid
            )
            blocks = formatters.format_realtime_notification(
                sub["gitlab_project_path"], mr, approval_info
            )
            _deliver_notification(sub, blocks)


# --- Approval Notifications ---

# Cache approval counts within a single poll cycle to avoid duplicate API calls
_poll_approval_cache: dict[tuple[int, int], int] = {}


def _check_approvals(sub: dict, mrs: list[dict], client) -> None:
    """Check for new approvals and send notifications."""
    from bot import formatters

    states = database.get_notification_states(sub["id"])
    if not states:
        return  # First poll — state not seeded yet

    for mr in mrs:
        iid = mr["iid"]
        existing = states.get(iid)
        if existing is None:
            continue  # New MR — will get a new-MR notification instead

        approval_info = client.get_mr_approvals(sub["gitlab_project_id"], iid)
        if approval_info is None:
            continue  # Approvals API not available (CE)

        current_count = approval_info["approval_count"]
        # Cache for upsert later
        _poll_approval_cache[(sub["id"], iid)] = current_count

        prev_count = existing.get("last_approval_count", 0)
        if current_count > prev_count and current_count > 0:
            blocks = formatters.format_approval_notification(
                sub["gitlab_project_path"], mr, approval_info
            )
            _deliver_notification(sub, blocks)


def _get_cached_approval_count(sub: dict, mr: dict, client) -> int:
    """Get approval count from poll cache or fetch fresh."""
    key = (sub["id"], mr["iid"])
    if key in _poll_approval_cache:
        count = _poll_approval_cache.pop(key)
        return count
    # Not cached (approvals not checked or not available)
    return 0


# --- Lifecycle Detection ---


def _execute_lifecycle(sub: dict, current_mrs: list[dict], client) -> None:
    """Detect merged/closed MRs and send lifecycle notifications."""
    from bot import formatters

    states = database.get_notification_states(sub["id"])
    current_iids = {mr["iid"] for mr in current_mrs}

    resolved_mrs = []
    for iid, state in states.items():
        if state["mr_state"] != "open":
            continue
        if iid in current_iids:
            continue
        # MR disappeared from open list — fetch to determine state
        try:
            mr = client.get_merge_request(sub["gitlab_project_id"], iid)
            new_state = mr.get("state", "closed")
            if new_state in ("merged", "closed"):
                merge_user = None
                if new_state == "merged" and mr.get("merge_user"):
                    merge_user = mr["merge_user"].get("name")
                database.update_notification_state_resolved(
                    sub["id"], iid, new_state
                )
                resolved_mrs.append({
                    "mr": mr,
                    "new_state": new_state,
                    "merge_user": merge_user,
                })
        except Exception as e:
            logger.warning(
                "Failed to fetch MR for lifecycle",
                extra={"subscription_id": sub["id"], "mr_iid": iid, "error": str(e)},
            )

    if not resolved_mrs:
        return

    # Always send lifecycle notifications immediately (both modes)
    for item in resolved_mrs:
        blocks = formatters.format_lifecycle_notification(
            sub["gitlab_project_path"],
            item["mr"],
            item["new_state"],
            item["merge_user"],
        )
        _deliver_notification(sub, blocks)


# --- Filters ---


def apply_filters(mrs: list[dict], sub: dict) -> list[dict]:
    """Apply subscription filters to a list of MRs."""
    filtered = mrs

    # Draft filter
    if not sub["include_drafts"]:
        filtered = [mr for mr in filtered if not mr.get("draft", False)]

    # Label filter (OR logic)
    if sub.get("filter_labels"):
        labels = {l.strip().lower() for l in sub["filter_labels"].split(",")}
        filtered = [
            mr for mr in filtered
            if any(l.lower() in labels for l in mr.get("labels", []))
        ]

    # Branch filter
    if sub.get("filter_branch"):
        branch = sub["filter_branch"]
        filtered = [
            mr for mr in filtered
            if mr.get("target_branch") == branch
        ]

    return filtered


# --- Notification Delivery ---


def _deliver_notification(sub: dict, attachments: list[dict]) -> bool:
    """Post a notification to the subscription's delivery target.

    Args:
        attachments: List of Slack attachment dicts with 'color' and 'blocks' keys.
    """
    from slack_sdk.errors import SlackApiError

    slack_client = _get_slack_client()
    if slack_client is None:
        logger.error("Slack client not available for delivery")
        return False

    try:
        slack_client.chat_postMessage(
            channel=sub["delivery_channel_id"],
            attachments=attachments,
            text="MR Notify notification",  # Fallback text
        )
        logger.info(
            "Notification delivered",
            extra={"subscription_id": sub["id"], "channel": sub["delivery_channel_id"]},
        )
        return True
    except SlackApiError as e:
        error_code = e.response.get("error", "") if e.response else ""
        if error_code in ("channel_not_found", "is_archived"):
            _handle_channel_deleted(sub, error_code)
        else:
            logger.error(
                "Slack delivery error",
                extra={"subscription_id": sub["id"], "error": error_code},
            )
            _handle_check_failure(sub["id"], f"Slack error: {error_code}")
        return False


# --- Error Handling ---


def _handle_check_failure(subscription_id: int, error_msg: str) -> None:
    """Increment failure counter and auto-pause if threshold reached."""
    new_count = database.increment_consecutive_failures(subscription_id)
    if new_count >= 5:
        reason = f"5 consecutive failures: {error_msg}"
        database.pause_subscription(subscription_id, reason)
        remove_subscription_jobs(subscription_id)
        logger.warning(
            "Subscription auto-paused",
            extra={"subscription_id": subscription_id, "reason": reason},
        )
        _notify_pause(subscription_id, reason)


def _handle_channel_deleted(sub: dict, error_code: str) -> None:
    """Auto-pause subscription when target channel is deleted/archived."""
    reason = f"Target channel deleted/archived ({error_code})"
    database.pause_subscription(sub["id"], reason)
    remove_subscription_jobs(sub["id"])
    logger.warning(
        "Subscription auto-paused — channel unavailable",
        extra={"subscription_id": sub["id"], "error": error_code},
    )

    # Try to DM the creator
    slack_client = _get_slack_client()
    if slack_client:
        try:
            slack_client.chat_postMessage(
                channel=sub["created_by"],
                text=f"Your MR Notify subscription for *{sub['gitlab_project_path']}* "
                     f"has been paused because the target channel is no longer available "
                     f"({error_code}). Use `/mr-config {sub['gitlab_project_path']} --resume` "
                     f"to re-enable it after resolving the channel issue.",
            )
        except Exception:
            logger.warning(
                "Failed to DM creator about paused subscription",
                extra={"subscription_id": sub["id"], "creator": sub["created_by"]},
            )


def _notify_pause(subscription_id: int, reason: str) -> None:
    """Notify the subscription owner about auto-pause."""
    sub = database.get_subscription_by_id(subscription_id)
    if not sub:
        return

    slack_client = _get_slack_client()
    if not slack_client:
        return

    target = sub["delivery_channel_id"]
    try:
        slack_client.chat_postMessage(
            channel=target,
            text=f"Subscription for *{sub['gitlab_project_path']}* has been paused: {reason}. "
                 f"Use `/mr-config {sub['gitlab_project_path']} --resume` to re-enable.",
        )
    except Exception:
        # If channel delivery fails, try DM to creator
        try:
            slack_client.chat_postMessage(
                channel=sub["created_by"],
                text=f"Your subscription for *{sub['gitlab_project_path']}* has been paused: "
                     f"{reason}. Use `/mr-config {sub['gitlab_project_path']} --resume` to re-enable.",
            )
        except Exception:
            logger.warning(
                "Failed to notify about paused subscription",
                extra={"subscription_id": subscription_id},
            )


# --- Maintenance ---


def _prune_notification_state() -> None:
    """Prune resolved notification states older than 30 days."""
    count = database.prune_resolved_states(days=30)
    logger.info("Notification state pruned", extra={"pruned_count": count})
