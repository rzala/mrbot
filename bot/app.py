"""Slack Bolt app setup, Socket Mode, and command handlers."""

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from bot import config, database, formatters, parsers, scheduler
from bot.gitlab_client import (
    AuthenticationError,
    GitLabClient,
    ProjectNotFoundError,
)

logger = logging.getLogger(__name__)

app: App | None = None
_gitlab: GitLabClient | None = None


def _get_gitlab() -> GitLabClient:
    global _gitlab
    if _gitlab is None:
        _gitlab = GitLabClient()
    return _gitlab


def _register_commands(app: App) -> None:
    """Register all slash command handlers on the app."""

    # --- /mr-subscribe ---

    @app.command("/mr-subscribe")
    def handle_subscribe(ack, command, respond, client):
        ack()
        text = (command.get("text") or "").strip()
        channel_id = command["channel_id"]
        user_id = command["user_id"]

        # Check for --dm flag
        force_dm = False
        if "--dm" in text:
            force_dm = True
            text = text.replace("--dm", "").strip()

        try:
            project_path = parsers.parse_repo_url(text)
        except ValueError as e:
            respond(blocks=formatters.format_error("Invalid URL format", str(e)))
            return

        try:
            project = _get_gitlab().get_project_by_path(project_path)
        except ProjectNotFoundError:
            respond(blocks=formatters.format_error(
                "Project not found",
                f'Could not find "{project_path}" on {config.GITLAB_URL}. '
                "Check the URL and your access permissions.",
            ))
            return
        except AuthenticationError:
            respond(blocks=formatters.format_error(
                "Authentication error",
                "The GitLab token is invalid. Please contact a bot admin.",
            ))
            return
        except Exception as e:
            respond(blocks=formatters.format_error("GitLab error", str(e)))
            return

        if force_dm:
            delivery_target = "user_dm"
            delivery_channel_id = user_id
        else:
            policy = database.get_channel_policy()
            if policy == "channel":
                delivery_target = "channel"
                delivery_channel_id = channel_id
            else:
                delivery_target = "user_dm"
                delivery_channel_id = user_id

        existing = database.get_subscription(
            project_path, channel_id, user_id, delivery_target
        )
        if existing:
            respond(blocks=formatters.format_already_subscribed(project_path))
            return

        try:
            sub = database.create_subscription(
                project_path=project_path,
                project_id=project["id"],
                channel_id=channel_id,
                user_id=user_id,
                delivery_target=delivery_target,
                delivery_channel_id=delivery_channel_id,
            )
        except Exception as e:
            logger.error("Failed to create subscription", extra={"error": str(e)})
            respond(blocks=formatters.format_error(
                "Already subscribed",
                f"This {'channel' if delivery_target == 'channel' else 'user'} already has "
                f"a subscription for {project_path}. Use /mr-config to change settings.",
            ))
            return

        scheduler.add_subscription_jobs(
            sub["id"], sub["schedule"], sub["poll_interval"], sub["mode"]
        )

        schedule_display = formatters.format_schedule_display(sub["schedule"])
        poll_display = formatters.format_schedule_display(sub["poll_interval"])
        if delivery_target == "channel":
            delivery_desc = "Notifications will post to this channel."
        else:
            delivery_desc = "Notifications will be sent to your MR Notify app DM."

        respond(blocks=formatters.format_subscribe_success(
            project_path, schedule_display, poll_display, sub["mode"],
            bool(sub["include_drafts"]), bool(sub["lifecycle_enabled"]),
            delivery_desc,
        ))

        logger.info(
            "Subscription created",
            extra={
                "subscription_id": sub["id"],
                "project": project_path,
                "user": user_id,
                "delivery": delivery_target,
            },
        )

    # --- /mr-unsubscribe ---

    @app.command("/mr-unsubscribe")
    def handle_unsubscribe(ack, command, respond):
        ack()
        text = (command.get("text") or "").strip()
        channel_id = command["channel_id"]
        user_id = command["user_id"]

        # Check for --dm flag
        force_dm = False
        if "--dm" in text:
            force_dm = True
            text = text.replace("--dm", "").strip()

        try:
            project_path = parsers.parse_repo_url(text)
        except ValueError as e:
            respond(blocks=formatters.format_error("Invalid URL format", str(e)))
            return

        lookup_policy = "app" if force_dm else database.get_channel_policy()
        sub = database.get_subscription_by_project_in_context(
            project_path, channel_id, user_id, lookup_policy
        )
        if not sub:
            respond(blocks=formatters.format_error(
                "Not subscribed",
                f'No subscription found for "{project_path}" in this context.',
            ))
            return

        if user_id != sub["created_by"] and not config.is_bot_admin(user_id):
            respond(blocks=formatters.format_error(
                "Permission denied",
                f"Only the subscription creator (<@{sub['created_by']}>) "
                "or a bot admin can remove this subscription.",
            ))
            return

        scheduler.remove_subscription_job(sub["id"])
        database.delete_subscription(sub["id"])

        respond(blocks=formatters.format_success(
            f":white_check_mark: `/mr-unsubscribe` — Unsubscribed from *{project_path}*. Notifications stopped."
        ))
        logger.info(
            "Subscription removed",
            extra={"subscription_id": sub["id"], "project": project_path, "user": user_id},
        )

    # --- /mr-list ---

    @app.command("/mr-list")
    def handle_list(ack, command, respond):
        ack()
        channel_id = command["channel_id"]
        user_id = command["user_id"]
        policy = database.get_channel_policy()

        subs = database.get_subscriptions_for_context(channel_id, user_id, policy)
        global_defaults = {
            "schedule": config.DEFAULT_SCHEDULE,
            "poll_interval": config.DEFAULT_POLL_INTERVAL,
            "mode": config.DEFAULT_MODE,
        }
        if not subs:
            blocks = formatters.format_subscription_list([], policy, global_defaults)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "No subscriptions in this context. Use `/mr-subscribe` to add one."},
            })
            respond(blocks=blocks)
            return

        respond(blocks=formatters.format_subscription_list(subs, policy, global_defaults))

    # --- /mr-config ---

    @app.command("/mr-config")
    def handle_config(ack, command, respond):
        ack()
        text = (command.get("text") or "").strip()
        channel_id = command["channel_id"]
        user_id = command["user_id"]

        try:
            repo_url, options = parsers.parse_config_options(text)
            project_path = parsers.parse_repo_url(repo_url)
        except ValueError as e:
            respond(blocks=formatters.format_error("Invalid input", str(e)))
            return

        # --dm targets the user's personal DM subscription
        if options.pop("dm", False):
            lookup_policy = "app"
        else:
            lookup_policy = database.get_channel_policy()

        sub = database.get_subscription_by_project_in_context(
            project_path, channel_id, user_id, lookup_policy
        )
        if not sub:
            respond(blocks=formatters.format_error(
                "Subscription not found",
                f'No subscription for "{project_path}" in this context. '
                "Use `/mr-subscribe` to create one.",
            ))
            return

        if user_id != sub["created_by"] and not config.is_bot_admin(user_id):
            respond(blocks=formatters.format_error(
                "Permission denied",
                f"Only the subscription creator (<@{sub['created_by']}>) "
                "or a bot admin can modify this subscription.",
            ))
            return

        # Handle --resume
        if options.get("resume"):
            if sub["status"] != "paused":
                respond(blocks=formatters.format_error(
                    "Not paused", f"Subscription for {project_path} is already active."
                ))
                return
            database.resume_subscription(sub["id"])
            scheduler.add_subscription_job(sub["id"], sub["schedule"])
            respond(blocks=formatters.format_success(
                f":white_check_mark: `/mr-config --resume` — Resumed subscription for *{project_path}*. Scheduled checks re-enabled."
            ))
            logger.info(
                "Subscription resumed",
                extra={"subscription_id": sub["id"], "project": project_path},
            )
            return

        # Build update fields
        updates = {}
        reschedule_args = {}

        if "schedule" in options:
            try:
                cron_expr = scheduler.resolve_schedule(options["schedule"])
                updates["schedule"] = cron_expr
                reschedule_args["schedule"] = cron_expr
            except ValueError as e:
                respond(blocks=formatters.format_error(
                    "Invalid cron expression",
                    f"Could not parse schedule: {e}. "
                    "Use presets (hourly, morning, twice-daily) or custom \"<cron>\".",
                ))
                return

        if "poll_interval" in options:
            try:
                cron_expr = scheduler.resolve_schedule(options["poll_interval"])
                updates["poll_interval"] = cron_expr
                reschedule_args["poll_interval"] = cron_expr
            except ValueError as e:
                respond(blocks=formatters.format_error(
                    "Invalid poll interval",
                    f"Could not parse poll interval: {e}. "
                    "Use presets (5min, 15min, 30min, hourly) or custom \"<cron>\".",
                ))
                return

        if "mode" in options:
            if options["mode"] not in ("digest", "realtime"):
                respond(blocks=formatters.format_error(
                    "Invalid mode", "Mode must be 'digest' or 'realtime'."
                ))
                return
            updates["mode"] = options["mode"]
            reschedule_args["mode"] = options["mode"]

        if options.get("include_drafts"):
            updates["include_drafts"] = 1
        if options.get("exclude_drafts"):
            updates["include_drafts"] = 0
        if "labels" in options:
            updates["filter_labels"] = options["labels"] or None
        if "branch" in options:
            updates["filter_branch"] = options["branch"] or None
        if options.get("no_lifecycle"):
            updates["lifecycle_enabled"] = 0
        if options.get("lifecycle"):
            updates["lifecycle_enabled"] = 1
        if options.get("no_approvals"):
            updates["notify_approvals"] = 0
        if options.get("approvals"):
            updates["notify_approvals"] = 1
        if options.get("suppress_empty"):
            updates["suppress_empty"] = 1
        if options.get("show_empty"):
            updates["suppress_empty"] = 0

        if not updates:
            respond(blocks=formatters.format_error(
                "No changes",
                "No configuration options provided. "
                "Use `/mr-help` to see available options.\n"
                "Example: `/mr-config mygroup/myproject --mode realtime --poll-interval 15min`",
            ))
            return

        database.update_subscription(sub["id"], **updates)
        if reschedule_args:
            scheduler.reschedule_subscription_jobs(sub["id"], **reschedule_args)

        changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "updated_at")
        respond(blocks=formatters.format_success(
            f":gear: `/mr-config` — Updated *{project_path}*: {changed}"
        ))
        logger.info(
            "Subscription updated",
            extra={"subscription_id": sub["id"], "changes": updates},
        )

    # --- /mr-check ---

    @app.command("/mr-check")
    def handle_check(ack, command, respond):
        ack()
        text = (command.get("text") or "").strip()
        channel_id = command["channel_id"]
        user_id = command["user_id"]

        # Check for --dm flag
        force_dm = False
        if "--dm" in text:
            force_dm = True
            text = text.replace("--dm", "").strip()

        lookup_policy = "app" if force_dm else database.get_channel_policy()

        if text:
            try:
                project_path = parsers.parse_repo_url(text)
            except ValueError as e:
                respond(blocks=formatters.format_error("Invalid URL format", str(e)))
                return

            sub = database.get_subscription_by_project_in_context(
                project_path, channel_id, user_id, lookup_policy
            )
            if not sub:
                respond(blocks=formatters.format_error(
                    "Not subscribed",
                    f'No subscription for "{project_path}" in this context.',
                ))
                return
            subs = [sub]
        else:
            subs = database.get_subscriptions_for_context(channel_id, user_id, lookup_policy)
            if not subs:
                respond(text="No subscriptions found. Use `/mr-subscribe` to add one.")
                return

        gitlab = _get_gitlab()
        all_attachments: list[dict] = []

        for sub in subs:
            try:
                project_id = sub["gitlab_project_id"]
                if project_id is None:
                    project = gitlab.get_project_by_path(sub["gitlab_project_path"])
                    project_id = project["id"]
                    database.update_subscription(sub["id"], gitlab_project_id=project_id)

                mrs = gitlab.get_open_merge_requests(project_id)
                mrs = scheduler.apply_filters(mrs, sub)

                # Fetch approval info for each MR
                approval_data = {}
                for mr in mrs:
                    info = gitlab.get_mr_approvals(project_id, mr["iid"])
                    if info:
                        approval_data[mr["iid"]] = info

                project_web_url = f"{config.GITLAB_URL}/{sub['gitlab_project_path']}"
                attachments = formatters.format_digest(
                    sub["gitlab_project_path"],
                    mrs,
                    len(mrs),
                    project_web_url,
                    False,  # Never suppress empty for on-demand checks
                    sub,
                    approval_data=approval_data,
                )
                if attachments:
                    all_attachments.extend(attachments)
            except Exception as e:
                all_attachments.extend(formatters.format_error(
                    sub["gitlab_project_path"], str(e)
                ))

        if all_attachments:
            respond(attachments=all_attachments, response_type="ephemeral")
        else:
            respond(text="No open merge requests found.", response_type="ephemeral")

    # --- /mr-admin ---

    @app.command("/mr-admin")
    def handle_admin(ack, command, respond):
        ack()
        user_id = command["user_id"]
        text = (command.get("text") or "").strip()

        if not config.is_bot_admin(user_id):
            respond(blocks=formatters.format_error(
                "Permission denied",
                "Only bot admins can use this command. "
                "Bot admins are configured via the BOT_ADMINS environment variable.",
            ))
            return

        if "--status" in text:
            counts = database.get_subscription_counts()
            policy = database.get_channel_policy()
            respond(blocks=formatters.format_admin_status(
                policy, counts["active_count"], counts["paused_count"], config.BOT_ADMINS
            ))
            return

        if "--channel-policy" in text:
            parts = text.split()
            try:
                idx = parts.index("--channel-policy")
                new_policy = parts[idx + 1]
            except (ValueError, IndexError):
                respond(blocks=formatters.format_error(
                    "Missing value",
                    "Usage:\n"
                    "`/mr-admin --channel-policy <channel|app>` — set global default\n"
                    "`/mr-admin --channel-policy <channel|app> <repo>` — update existing subscription",
                ))
                return

            if new_policy not in ("channel", "app"):
                respond(blocks=formatters.format_error(
                    "Invalid policy",
                    f'Channel policy must be "channel" or "app", got "{new_policy}".',
                ))
                return

            # Check if a repo path was provided to update a specific subscription
            repo_arg = parts[idx + 2] if len(parts) > idx + 2 else None
            if repo_arg:
                try:
                    project_path = parsers.parse_repo_url(repo_arg)
                except ValueError as e:
                    respond(blocks=formatters.format_error("Invalid repo", str(e)))
                    return

                channel_id = command["channel_id"]
                current_policy = database.get_channel_policy()
                sub = database.get_subscription_by_project_in_context(
                    project_path, channel_id, user_id, current_policy
                )
                if not sub:
                    respond(blocks=formatters.format_error(
                        "Subscription not found",
                        f'No subscription for "{project_path}" in this context.',
                    ))
                    return

                if new_policy == "app":
                    database.update_subscription(
                        sub["id"],
                        delivery_target="user_dm",
                        delivery_channel_id=sub["created_by"],
                    )
                    delivery_desc = f"DM to <@{sub['created_by']}>"
                else:
                    database.update_subscription(
                        sub["id"],
                        delivery_target="channel",
                        delivery_channel_id=sub["slack_channel_id"],
                    )
                    delivery_desc = f"<#{sub['slack_channel_id']}>"

                respond(blocks=formatters.format_success(
                    f":gear: `/mr-admin --channel-policy` — *{project_path}* delivery changed to *{new_policy}* ({delivery_desc})."
                ))
                logger.info(
                    "Subscription delivery updated",
                    extra={"subscription_id": sub["id"], "policy": new_policy, "admin": user_id},
                )
                return

            # No repo — update global default
            database.update_channel_policy(new_policy)
            respond(blocks=formatters.format_success(
                f":gear: `/mr-admin --channel-policy` — Global policy set to *{new_policy}*. "
                "Existing subscriptions are unaffected — use `/mr-admin --channel-policy <policy> <repo>` to update specific subscriptions."
            ))
            logger.info("Channel policy updated", extra={"policy": new_policy, "admin": user_id})
            return

        respond(text=(
            "Usage:\n"
            "`/mr-admin --status` — show bot status\n"
            "`/mr-admin --channel-policy <channel|app>` — set global default\n"
            "`/mr-admin --channel-policy <channel|app> <repo>` — update existing subscription"
        ))

    # --- /mr-help ---

    @app.command("/mr-help")
    def handle_help(ack, command, respond):
        ack()
        respond(blocks=formatters.format_help(), response_type="ephemeral")


# --- Entry Point ---


def main() -> None:
    """Start the MR Notify bot."""
    global app
    config.setup()
    database.init_db()

    app = App(token=config.SLACK_BOT_TOKEN, name="MR Notify")
    _register_commands(app)

    handler = SocketModeHandler(app, config.SLACK_APP_TOKEN)
    scheduler.start_scheduler(app.client)

    logger.info("MR Notify Bot started")
    handler.start()


if __name__ == "__main__":
    main()
