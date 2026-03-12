"""GitLab REST API v4 wrapper with typed exceptions and project caching."""

import logging
from urllib.parse import quote

import requests

from bot import config, database

logger = logging.getLogger(__name__)

_MAX_PAGES = 10


# --- Typed Exceptions ---


class AuthenticationError(Exception):
    """GitLab returned 401 — token invalid or expired."""


class ProjectNotFoundError(Exception):
    """GitLab returned 404 — project does not exist or is inaccessible."""


class RateLimitError(Exception):
    """GitLab returned 429 — rate limited."""

    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s" if retry_after else "Rate limited")


class GitLabUnavailableError(Exception):
    """GitLab returned 5xx or a network error occurred."""


# --- Client ---


class GitLabClient:
    """Wrapper around GitLab REST API v4 with session management."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["PRIVATE-TOKEN"] = config.GITLAB_TOKEN
        if config.GITLAB_CA_BUNDLE:
            self._session.verify = config.GITLAB_CA_BUNDLE
        self._base_url = config.GITLAB_URL.rstrip("/") + "/api/v4"
        self._approvals_available: bool | None = None  # None = untested

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make a request and raise typed exceptions on errors."""
        url = f"{self._base_url}{path}"
        try:
            resp = self._session.request(method, url, timeout=30, **kwargs)
        except requests.ConnectionError as e:
            raise GitLabUnavailableError(f"Connection error: {e}") from e
        except requests.Timeout as e:
            raise GitLabUnavailableError(f"Request timeout: {e}") from e

        if resp.status_code == 401:
            raise AuthenticationError("GitLab authentication failed")
        if resp.status_code == 404:
            raise ProjectNotFoundError(f"Not found: {path}")
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            raise RateLimitError(int(retry_after) if retry_after else None)
        if resp.status_code >= 500:
            raise GitLabUnavailableError(f"GitLab server error: {resp.status_code}")

        resp.raise_for_status()
        return resp

    def get_project_by_path(self, project_path: str) -> dict:
        """Look up a GitLab project by path. Uses cache with 24h TTL.

        Returns the project dict from GitLab API.
        Raises ProjectNotFoundError if not found, AuthenticationError on 401.
        """
        # Check cache
        cached_id = database.get_cached_project(project_path)
        if cached_id is not None:
            try:
                resp = self._request("GET", f"/projects/{cached_id}")
                return resp.json()
            except ProjectNotFoundError:
                database.invalidate_cached_project(project_path)
                # Fall through to re-resolve by path

        # Resolve by path
        encoded_path = quote(project_path, safe="")
        resp = self._request("GET", f"/projects/{encoded_path}")
        project = resp.json()

        # Cache the result
        database.set_cached_project(project_path, project["id"])
        return project

    def get_open_merge_requests(self, project_id: int) -> list[dict]:
        """Fetch ALL open merge requests with exhaustive pagination.

        Returns list of MR dicts. Stops after _MAX_PAGES (safety limit).
        """
        all_mrs: list[dict] = []
        page = 1
        while page <= _MAX_PAGES:
            resp = self._request(
                "GET",
                f"/projects/{project_id}/merge_requests",
                params={"state": "opened", "per_page": 100, "page": page},
            )
            mrs = resp.json()
            all_mrs.extend(mrs)
            next_page = resp.headers.get("x-next-page", "")
            if not next_page:
                break
            page = int(next_page)

        if page > _MAX_PAGES:
            logger.warning(
                "Pagination safety limit reached",
                extra={"project_id": project_id, "pages_fetched": _MAX_PAGES},
            )
        return all_mrs

    def get_merge_request(self, project_id: int, mr_iid: int) -> dict:
        """Fetch a single merge request by IID."""
        resp = self._request(
            "GET", f"/projects/{project_id}/merge_requests/{mr_iid}"
        )
        return resp.json()

    def get_mr_approvals(self, project_id: int, mr_iid: int) -> dict | None:
        """Fetch approval info for a merge request.

        Returns dict with 'approved_by' (list of names) and 'approval_count',
        or None if the approvals API is not available (GitLab CE).
        """
        if self._approvals_available is False:
            return None

        try:
            resp = self._request(
                "GET", f"/projects/{project_id}/merge_requests/{mr_iid}/approvals"
            )
            data = resp.json()
            self._approvals_available = True
            approved_by = [a["user"]["name"] for a in data.get("approved_by", [])]
            return {
                "approved_by": approved_by,
                "approval_count": len(approved_by),
            }
        except (ProjectNotFoundError, GitLabUnavailableError):
            if self._approvals_available is None:
                self._approvals_available = False
                logger.info(
                    "Approvals API not available — approval status will be omitted"
                )
            return None
