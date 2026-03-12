"""Slack Block Kit message builders for notifications and command responses."""

from datetime import datetime, timezone

from bot import user_cache

_MAX_DISPLAY_MRS = 50
_MAX_BLOCKS = 48  # Reserve 2 for header/footer

SCHEDULE_PRESETS_DISPLAY = {
    "*/1 * * * *": "every minute",
    "*/2 * * * *": "every 2 minutes",
    "*/5 * * * *": "every 5 minutes",
    "*/15 * * * *": "every 15 minutes",
    "*/30 * * * *": "every 30 minutes",
    "0 * * * *": "every hour",
    "0 9 * * 1-5": "weekdays at 9:00 AM (morning)",
    "0 9,14 * * 1-5": "weekdays at 9:00 AM and 2:00 PM (twice-daily)",
}

# GitLab-themed colors for attachment sidebars
_COLOR_BLUE = "#1F78D1"       # New/updated MR
_COLOR_GREEN = "#1AAA55"      # Approved
_COLOR_PURPLE = "#6E49CB"     # Merged
_COLOR_RED = "#DB3B21"        # Closed
_COLOR_ORANGE = "#FC6D26"     # Digest / GitLab brand
_COLOR_GRAY = "#999999"       # All clear


def _truncate_text(text: str, max_len: int = 3000) -> str:
    """Truncate text to fit Block Kit limits."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _mr_age(created_at: str) -> str:
    """Compute human-readable age from ISO timestamp."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            return f"{hours}h" if hours > 0 else "< 1h"
        if days == 1:
            return "1 day"
        return f"{days} days"
    except Exception:
        return "?"


def _resolve_author(mr: dict) -> str:
    """Resolve MR author to Slack @mention or plain name."""
    author = mr.get("author", {})
    name = author.get("name", "Unknown")
    username = author.get("username", "")
    # Try full name → username → first name
    result = user_cache.resolve(name)
    if not result.startswith("<@") and username:
        result = user_cache.resolve(username)
    if not result.startswith("<@"):
        first = name.split()[0] if name else ""
        if first:
            result = user_cache.resolve(first)
    return result


def _attachment(color: str, blocks: list[dict]) -> dict:
    """Wrap blocks in a Slack attachment with a colored sidebar."""
    return {"color": color, "blocks": blocks}


# --- Error / Success ---


def format_error(title: str, message: str) -> list[dict]:
    """Build Block Kit blocks for an error response."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate_text(f":x: *{title}*\n{message}"),
            },
        }
    ]


def format_success(message: str) -> list[dict]:
    """Build Block Kit blocks for a simple success response."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate_text(message),
            },
        }
    ]


# --- Subscribe ---


def format_subscribe_success(
    project_path: str,
    schedule_display: str,
    poll_display: str,
    mode: str,
    include_drafts: bool,
    lifecycle: bool,
    delivery_description: str,
) -> list[dict]:
    """Format a subscription confirmation message."""
    drafts_text = "included" if include_drafts else "excluded"
    lifecycle_text = "enabled" if lifecycle else "disabled"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: `/mr-subscribe` — *Subscribed to {project_path}*\n"
                    f"Digest schedule: {schedule_display} | Poll interval: {poll_display}\n"
                    f"Mode: {mode} | Drafts: {drafts_text} | Lifecycle: {lifecycle_text}\n"
                    f"{delivery_description}"
                ),
            },
        }
    ]


def format_already_subscribed(project_path: str) -> list[dict]:
    """Format a 'already subscribed' error."""
    return format_error(
        "Already subscribed",
        f"This context already has a subscription for *{project_path}*. "
        "Use `/mr-config` to change settings.",
    )


def format_project_not_found(project_path: str, gitlab_url: str) -> list[dict]:
    """Format a 'project not found' error."""
    return format_error(
        "Project not found",
        f'Could not find "{project_path}" on {gitlab_url}. '
        "Check the URL and your access permissions.",
    )


def format_invalid_url() -> list[dict]:
    """Format an 'invalid URL format' error."""
    return format_error(
        "Invalid URL format",
        "Please provide a repo as SSH URL, HTTPS URL, or project path.\n"
        "Examples:\n"
        "  `git@gitlab.example.com:mygroup/myproject.git`\n"
        "  `https://gitlab.example.com/mygroup/myproject.git`\n"
        "  `mygroup/myproject`",
    )


# --- Schedule Display ---


def format_schedule_display(cron_expr: str) -> str:
    """Map a cron expression to human-readable text."""
    return SCHEDULE_PRESETS_DISPLAY.get(cron_expr, f"custom ({cron_expr})")


# --- Filters ---


def format_active_filters(sub: dict) -> str:
    """Return a text summary of active filters (only non-default ones)."""
    parts = []
    if sub.get("include_drafts"):
        parts.append("include drafts")
    if sub.get("filter_labels"):
        parts.append(f"labels={sub['filter_labels']}")
    if sub.get("filter_branch"):
        parts.append(f"branch={sub['filter_branch']}")
    return ", ".join(parts) if parts else ""


# --- Digest ---


def format_digest(
    project_path: str,
    mrs: list[dict],
    total_count: int,
    project_web_url: str,
    suppress_empty: bool,
    sub: dict,
    approval_data: dict | None = None,
) -> list[dict] | None:
    """Build a colored attachment for a digest message.

    Returns a list containing a single attachment dict with 'color' and 'blocks',
    or None if suppress_empty and no MRs.
    """
    if total_count == 0:
        if suppress_empty:
            return None
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":white_check_mark: *All clear!* No open merge requests.",
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f":fox_face: {project_path}"}],
            },
        ]
        return [_attachment(_COLOR_GRAY, blocks)]

    blocks: list[dict] = []

    # Header
    filter_text = format_active_filters(sub)
    mr_word = "merge request" if total_count == 1 else "merge requests"
    header_text = f":arrows_counterclockwise: *{total_count} open {mr_word}*"
    if filter_text and filter_text != "none":
        header_text += f"  |  :mag: {filter_text}"
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": header_text},
    })

    # MR list
    display_mrs = mrs[:_MAX_DISPLAY_MRS]
    for mr in display_mrs:
        if len(blocks) >= _MAX_BLOCKS:
            break
        title = mr.get("title", "Untitled")
        url = mr.get("web_url", "")
        author = _resolve_author(mr)
        age = _mr_age(mr.get("created_at", ""))
        target = mr.get("target_branch", "?")
        draft = "  `DRAFT`" if mr.get("draft") else ""

        line = f"*<{url}|!{mr.get('iid', '?')} {title}>*{draft}"

        # Metadata line
        meta_parts = [f":bust_in_silhouette: {author}", f":twisted_rightwards_arrows: {target}", f":clock1: {age}"]
        line += f"\n{'    '.join(meta_parts)}"

        # Labels
        labels = mr.get("labels", [])
        if labels:
            label_text = "  ".join(f"`{l}`" for l in labels)
            line += f"\n:label: {label_text}"

        # Approval status
        if approval_data:
            info = approval_data.get(mr.get("iid"))
            if info and info["approval_count"] > 0:
                approvers = ", ".join(user_cache.resolve(n) for n in info["approved_by"])
                line += f"\n:white_check_mark: Approved ({info['approval_count']}): {approvers}"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate_text(line)},
        })

    # Truncation notice
    if total_count > _MAX_DISPLAY_MRS:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_...and {total_count - _MAX_DISPLAY_MRS} more — "
                        f"<{project_web_url}/-/merge_requests?state=opened|View all on GitLab>_",
            }],
        })

    # Footer
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f":fox_face: {project_path}  |  {now}"}],
    })

    return [_attachment(_COLOR_ORANGE, blocks)]


# --- Digest Resolved Section ---


def format_digest_resolved_section(resolved_mrs: list[dict]) -> list[dict]:
    """Build a colored attachment for recently resolved MRs in digest."""
    if not resolved_mrs:
        return []

    blocks: list[dict] = []
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f":checkered_flag: *Recently resolved ({len(resolved_mrs)})*"},
    })

    for item in resolved_mrs:
        mr = item["mr"]
        state = item["new_state"]
        title = mr.get("title", "Untitled")
        url = mr.get("web_url", "")
        icon = ":large_purple_circle:" if state == "merged" else ":red_circle:"
        merge_info = ""
        if state == "merged" and item.get("merge_user"):
            merge_info = f" by {item['merge_user']}"
        line = f"{icon} <{url}|{title}> — {state}{merge_info}"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate_text(line)},
        })

    return [_attachment(_COLOR_PURPLE, blocks)]


# --- Realtime ---


def format_realtime_notification(
    project_path: str, mr: dict, approval_info: dict | None
) -> list[dict]:
    """Build a colored attachment for a new/updated MR notification."""
    title = mr.get("title", "Untitled")
    url = mr.get("web_url", "")
    iid = mr.get("iid", "?")
    author = _resolve_author(mr)
    target = mr.get("target_branch", "?")
    age = _mr_age(mr.get("created_at", ""))
    labels = mr.get("labels", [])
    draft = "  `DRAFT`" if mr.get("draft") else ""

    blocks: list[dict] = []

    # Title
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": _truncate_text(f":arrows_counterclockwise: *<{url}|!{iid} {title}>*{draft}"),
        },
    })

    # Metadata fields
    fields = [
        {"type": "mrkdwn", "text": f":bust_in_silhouette: *Author*\n{author}"},
        {"type": "mrkdwn", "text": f":twisted_rightwards_arrows: *Branch*\n{target}"},
        {"type": "mrkdwn", "text": f":clock1: *Age*\n{age}"},
    ]
    if labels:
        label_text = " ".join(f"`{l}`" for l in labels)
        fields.append({"type": "mrkdwn", "text": f":label: *Labels*\n{label_text}"})
    if approval_info and approval_info["approval_count"] > 0:
        approvers = ", ".join(user_cache.resolve(n) for n in approval_info["approved_by"])
        fields.append({"type": "mrkdwn", "text": f":white_check_mark: *Approved ({approval_info['approval_count']})*\n{approvers}"})

    blocks.append({"type": "section", "fields": fields[:10]})  # Slack max 10 fields

    # Footer
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f":fox_face: {project_path}"}],
    })

    return [_attachment(_COLOR_BLUE, blocks)]


# --- Lifecycle ---


def format_lifecycle_notification(
    project_path: str, mr: dict, new_state: str, merge_user: str | None
) -> list[dict]:
    """Build a colored attachment for a lifecycle event (merged/closed)."""
    title = mr.get("title", "Untitled")
    url = mr.get("web_url", "")
    iid = mr.get("iid", "?")
    author = _resolve_author(mr)

    if new_state == "merged":
        icon = ":large_purple_circle:"
        color = _COLOR_PURPLE
        resolved_merger = user_cache.resolve(merge_user) if merge_user else None
        action = f"Merged by {resolved_merger}" if resolved_merger else "Merged"
    else:
        icon = ":red_circle:"
        color = _COLOR_RED
        action = "Closed"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate_text(f"{icon} *<{url}|!{iid} {title}>*\n{action}  |  Author: {author}"),
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":fox_face: {project_path}"}],
        },
    ]

    return [_attachment(color, blocks)]


# --- Approval ---


def format_approval_notification(
    project_path: str, mr: dict, approval_info: dict
) -> list[dict]:
    """Build a colored attachment for an MR approval event."""
    title = mr.get("title", "Untitled")
    url = mr.get("web_url", "")
    iid = mr.get("iid", "?")
    approvers = ", ".join(user_cache.resolve(n) for n in approval_info["approved_by"])
    count = approval_info["approval_count"]

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate_text(
                    f":white_check_mark: *<{url}|!{iid} {title}>*\n"
                    f"Approved ({count})  |  By: {approvers}"
                ),
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":fox_face: {project_path}"}],
        },
    ]

    return [_attachment(_COLOR_GREEN, blocks)]


# --- Subscription List ---


def format_subscription_list(
    subs: list[dict],
    channel_policy: str,
    global_defaults: dict | None = None,
) -> list[dict]:
    """Build Block Kit blocks for /mr-list output."""
    active = [s for s in subs if s["status"] == "active"]
    paused = [s for s in subs if s["status"] == "paused"]

    # Global settings header
    header = f":gear: `/mr-list` — *MR Notify*\n"
    header += f"Channel policy: `{channel_policy}`"
    if global_defaults:
        header += (
            f" | Default schedule: `{global_defaults.get('schedule', '?')}`"
            f" | Default poll: `{global_defaults.get('poll_interval', '?')}`"
            f" | Default mode: `{global_defaults.get('mode', '?')}`"
        )

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
    ]

    sub_header = f"*Subscriptions ({len(active)} active"
    if paused:
        sub_header += f", {len(paused)} paused"
    sub_header += ")*"
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": sub_header}}
    )

    for i, sub in enumerate(subs, 1):
        schedule = format_schedule_display(sub["schedule"])
        poll = format_schedule_display(sub.get("poll_interval", "*/5 * * * *"))
        status = sub["status"]
        if status == "paused" and sub.get("pause_reason"):
            status = f"paused ({sub['pause_reason']})"
        filters = format_active_filters(sub)
        lifecycle = "on" if sub.get("lifecycle_enabled") else "off"
        approvals = "on" if sub.get("notify_approvals") else "off"
        created = sub.get("created_at", "?")[:10]

        line = (
            f"*{i}. {sub['gitlab_project_path']}*\n"
            f"   Digest: {schedule} | Poll: {poll}\n"
            f"   Lifecycle: {lifecycle} | Approvals: {approvals} | Filters: {filters}\n"
            f"   Status: {status} | Created by <@{sub['created_by']}> on {created}"
        )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate_text(line)},
        })

    return blocks


# --- Admin Status ---


def format_admin_status(
    channel_policy: str,
    active_count: int,
    paused_count: int,
    admin_ids: set[str],
) -> list[dict]:
    """Build Block Kit blocks for /mr-admin --status output."""
    admins = ", ".join(f"<@{uid}>" for uid in sorted(admin_ids)) if admin_ids else "none"
    text = (
        "*MR Notify Bot Status*\n"
        f"Channel policy: {channel_policy}\n"
        f"Active subscriptions: {active_count}\n"
        f"Paused subscriptions: {paused_count}\n"
        f"Bot admins: {admins}"
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    ]


# --- Help ---


def format_help() -> list[dict]:
    """Build Block Kit blocks for /mr-help output."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*MR Notify — Commands*\n\n"
                    "`/mr-subscribe <repo> [--dm]` — Subscribe to MR notifications (--dm = notify only you)\n"
                    "`/mr-unsubscribe <repo> [--dm]` — Remove a subscription\n"
                    "`/mr-list` — List subscriptions in this context\n"
                    "`/mr-check [repo] [--dm]` — Immediately check for open MRs\n"
                    "`/mr-config <repo> [--dm] [options]` — Configure a subscription\n"
                    "`/mr-admin [options]` — Bot administration (admins only)\n"
                    "`/mr-help` — Show this help message"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Repo formats*\n"
                    "  `mygroup/myproject` — bare path\n"
                    "  `git@gitlab.example.com:mygroup/myproject.git` — SSH\n"
                    "  `https://gitlab.example.com/mygroup/myproject.git` — HTTPS"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*`/mr-config` options*\n"
                    "  `--schedule <preset|cron>` — Digest timing: `morning`, `twice-daily`, `hourly`, `custom \"<cron>\"`\n"
                    "  `--poll-interval <preset|cron>` — Change detection: `1min`, `2min`, `5min`, `15min`, `30min`, `hourly`\n"
                    "  `--include-drafts` / `--exclude-drafts` — Include or exclude draft MRs (default: excluded)\n"
                    "  `--labels \"bug,hotfix\"` — Filter by labels (OR logic)\n"
                    "  `--branch main` — Filter by target branch\n"
                    "  `--lifecycle` / `--no-lifecycle` — Toggle merge/close notifications (default: on)\n"
                    "  `--approvals` / `--no-approvals` — Toggle approval notifications (default: on)\n"
                    "  `--suppress-empty` / `--show-empty` — Toggle \"all clear\" messages (default: shown)\n"
                    "  `--dm` — Target your personal DM subscription\n"
                    "  `--resume` — Resume a paused subscription"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Examples*\n"
                    "  `/mr-subscribe mygroup/myproject`\n"
                    "  `/mr-subscribe mygroup/myproject --dm` — personal DM notifications\n"
                    "  `/mr-config mygroup/myproject --schedule twice-daily --poll-interval 15min`\n"
                    "  `/mr-config mygroup/myproject --dm --poll-interval 1min` — configure DM subscription\n"
                    "  `/mr-config mygroup/myproject --labels \"bug,urgent\" --branch main`"
                ),
            },
        },
    ]
