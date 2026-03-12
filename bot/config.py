"""Environment-based configuration, structured logging, and token security."""

import logging
import os

from pythonjsonlogger import jsonlogger

# Configuration values (populated by setup())
SLACK_BOT_TOKEN = ""
SLACK_APP_TOKEN = ""
GITLAB_TOKEN = ""
GITLAB_URL = ""
DATABASE_PATH = ""
GITLAB_CA_BUNDLE = ""
LOG_LEVEL = ""
DEFAULT_SCHEDULE = ""
DEFAULT_POLL_INTERVAL = ""
DEFAULT_MODE = ""
BOT_ADMINS: set[str] = set()

logger = logging.getLogger(__name__)


class _TokenRedactionFilter(logging.Filter):
    """Replaces the GitLab token value with [REDACTED] in log messages."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token and hasattr(record, "msg") and isinstance(record.msg, str):
            record.msg = record.msg.replace(self._token, "[REDACTED]")
        if self._token and record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    arg.replace(self._token, "[REDACTED]")
                    if isinstance(arg, str)
                    else arg
                    for arg in record.args
                )
        return True


def _load_dotenv() -> None:
    """Load .env file if present, without overriding existing env vars."""
    env_file = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_file):
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not os.environ.get(key):
                os.environ[key] = value


def setup() -> None:
    """Load configuration from environment variables and configure logging."""
    global SLACK_BOT_TOKEN, SLACK_APP_TOKEN, GITLAB_TOKEN, GITLAB_URL
    global DATABASE_PATH, GITLAB_CA_BUNDLE, LOG_LEVEL
    global DEFAULT_SCHEDULE, DEFAULT_POLL_INTERVAL, DEFAULT_MODE, BOT_ADMINS

    _load_dotenv()

    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
    SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
    GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")
    GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.example.com")
    DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/mr-notify.db")
    GITLAB_CA_BUNDLE = os.environ.get("REQUESTS_CA_BUNDLE", "")
    if not GITLAB_CA_BUNDLE and os.path.isfile("ca-bundle.crt"):
        GITLAB_CA_BUNDLE = os.path.abspath("ca-bundle.crt")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    DEFAULT_SCHEDULE = os.environ.get("DEFAULT_SCHEDULE", "0 9 * * 1-5")
    DEFAULT_POLL_INTERVAL = os.environ.get("DEFAULT_POLL_INTERVAL", "*/5 * * * *")
    DEFAULT_MODE = os.environ.get("DEFAULT_MODE", "digest")

    admins_raw = os.environ.get("BOT_ADMINS", "")
    BOT_ADMINS = {uid.strip() for uid in admins_raw.split(",") if uid.strip()}

    _configure_logging()

    if not BOT_ADMINS:
        logger.warning(
            "BOT_ADMINS is not set — no global admin commands available"
        )


def _configure_logging() -> None:
    """Set up structured JSON logging with token redaction."""
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    if GITLAB_TOKEN:
        root_logger.addFilter(_TokenRedactionFilter(GITLAB_TOKEN))


def is_bot_admin(user_id: str) -> bool:
    """Check if a Slack user ID is in the BOT_ADMINS set."""
    return user_id in BOT_ADMINS
