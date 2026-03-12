"""Cache mapping GitLab users to Slack user IDs for @mention rendering.

Matches by email (primary), then username, then display name / real name.
"""

import logging
import time

logger = logging.getLogger(__name__)

# Lookup caches: lowercase key → Slack user ID
_email_to_slack_id: dict[str, str] = {}
_name_to_slack_id: dict[str, str] = {}
_last_refresh: float = 0
_REFRESH_INTERVAL = 3600  # Refresh every hour


def refresh(slack_client) -> None:
    """Fetch all Slack workspace users and build lookup caches."""
    global _email_to_slack_id, _name_to_slack_id, _last_refresh

    try:
        emails: dict[str, str] = {}
        names: dict[str, str] = {}
        cursor = None

        while True:
            resp = slack_client.users_list(cursor=cursor, limit=200)
            for member in resp.get("members", []):
                if member.get("deleted") or member.get("is_bot"):
                    continue
                uid = member["id"]
                profile = member.get("profile", {})

                # Email (most reliable match)
                email = profile.get("email", "")
                if email:
                    emails[email.lower()] = uid
                    # Also index the local part (before @) for username matching
                    local_part = email.split("@")[0].lower()
                    if local_part:
                        names[local_part] = uid

                # Name variants for fallback matching
                for name_field in [
                    member.get("real_name"),
                    profile.get("real_name"),
                    profile.get("display_name"),
                    member.get("name"),
                ]:
                    if name_field:
                        names[name_field.lower()] = uid
                        # Also index first name for partial matching
                        first = name_field.strip().split()[0].lower()
                        if first and first not in names:
                            names[first] = uid

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        _email_to_slack_id = emails
        _name_to_slack_id = names
        _last_refresh = time.monotonic()
        logger.info(
            "Slack user cache refreshed",
            extra={"email_count": len(emails), "name_count": len(names)},
        )

    except Exception as e:
        logger.warning("Failed to refresh Slack user cache", extra={"error": str(e)})


def resolve(gitlab_name: str, gitlab_email: str | None = None, slack_client=None) -> str:
    """Resolve a GitLab user to a Slack @mention or plain text fallback.

    Tries email first, then name/username matching.
    Returns '<@SLACK_ID>' if matched, otherwise the original name.
    """
    if slack_client and (time.monotonic() - _last_refresh > _REFRESH_INTERVAL):
        refresh(slack_client)

    if not _name_to_slack_id and not _email_to_slack_id:
        return gitlab_name

    # Try email match first
    if gitlab_email:
        slack_id = _email_to_slack_id.get(gitlab_email.lower())
        if slack_id:
            return f"<@{slack_id}>"

    # Try name/username match
    slack_id = _name_to_slack_id.get(gitlab_name.lower())
    if slack_id:
        return f"<@{slack_id}>"

    return gitlab_name
