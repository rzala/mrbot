"""Repository URL parsing for SSH, HTTPS, and bare path formats."""

import re

# git@gitlab.example.com:mygroup/myproject.git
_SSH_PATTERN = re.compile(r"^git@[^:]+:(.+?)(?:\.git)?$")

# https://gitlab.example.com/mygroup/myproject.git
_HTTPS_PATTERN = re.compile(r"^https?://[^/]+/(.+?)(?:\.git)?$")

# mygroup/myproject (at least one slash, no protocol/host)
_PATH_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)+$")

_USAGE_EXAMPLES = (
    "Accepted formats:\n"
    "  SSH:   git@gitlab.example.com:mygroup/myproject.git\n"
    "  HTTPS: https://gitlab.example.com/mygroup/myproject.git\n"
    "  Path:  mygroup/myproject"
)


def parse_repo_url(text: str) -> str:
    """Parse a repository URL/path and return the normalized project path.

    Supports SSH URLs, HTTPS URLs, and bare project paths.
    Strips .git suffix if present.

    Returns:
        Normalized project path, e.g. 'mygroup/myproject'

    Raises:
        ValueError: If the input doesn't match any accepted format.
    """
    text = text.strip()
    if not text:
        raise ValueError(f"Please provide a repository URL or path.\n{_USAGE_EXAMPLES}")

    # Try SSH format
    match = _SSH_PATTERN.match(text)
    if match:
        return match.group(1)

    # Try HTTPS format
    match = _HTTPS_PATTERN.match(text)
    if match:
        return match.group(1)

    # Try bare path (strip .git if someone adds it)
    clean = text.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    if _PATH_PATTERN.match(clean):
        return clean

    raise ValueError(
        f'Invalid URL format: "{text}"\n{_USAGE_EXAMPLES}'
    )


# --- Config Option Parsing ---

_FLAGS = {
    "--include-drafts", "--exclude-drafts", "--no-lifecycle", "--lifecycle",
    "--suppress-empty", "--show-empty", "--resume",
    "--approvals", "--no-approvals", "--dm",
}
_VALUE_OPTIONS = {"--schedule", "--poll-interval", "--mode", "--labels", "--branch"}


def parse_config_options(text: str) -> tuple[str, dict]:
    """Parse /mr-config text into (repo_url, options_dict).

    Returns:
        Tuple of (repo_url_string, options_dict).
        options_dict keys use underscores (e.g., 'include_drafts', 'no_lifecycle').

    Raises:
        ValueError: If no repo URL found or unrecognized option.
    """
    if not text.strip():
        raise ValueError("Usage: /mr-config <repo-url> [options]")

    tokens = _tokenize(text)
    if not tokens:
        raise ValueError("Usage: /mr-config <repo-url> [options]")

    repo_url = tokens[0]
    options: dict = {}
    i = 1

    while i < len(tokens):
        token = tokens[i]
        if token in _FLAGS:
            key = token.lstrip("-").replace("-", "_")
            options[key] = True
            i += 1
        elif token in _VALUE_OPTIONS:
            if i + 1 >= len(tokens):
                raise ValueError(f"Option {token} requires a value")
            key = token.lstrip("-").replace("-", "_")
            options[key] = tokens[i + 1]
            i += 2
        elif token.startswith("--"):
            raise ValueError(f'Unrecognized option: "{token}"')
        else:
            # Might be part of a custom schedule expression like: custom "0 */2 * * 1-5"
            # Skip non-option tokens after the repo URL
            i += 1

    return repo_url, options


def _tokenize(text: str) -> list[str]:
    """Split text respecting quoted strings."""
    tokens = []
    current = []
    in_quote = None

    for char in text:
        if in_quote:
            if char == in_quote:
                in_quote = None
            else:
                current.append(char)
        elif char in ('"', "'"):
            in_quote = char
        elif char == " ":
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)

    if current:
        tokens.append("".join(current))
    return tokens
