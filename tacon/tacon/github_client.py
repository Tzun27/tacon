"""PyGithub wrapper with rate limiting + error classification.

Two responsibilities:
  1. Throttle calls so we stay below GitHub's secondary rate limits
     (sequential, default 3 req/sec; configurable via --rate).
  2. Classify exceptions so events.error_class can power triage.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

from github import (
    Auth,
    Github,
    GithubException,
    RateLimitExceededException,
    UnknownObjectException,
)
from github.Repository import Repository

# Error classes (keep in sync with db.py events.error_class enum)
ERROR_NETWORK = "network"
ERROR_AUTH = "auth"
ERROR_SSO = "sso"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_NOT_FOUND = "not_found"
ERROR_CONFLICT = "conflict"
ERROR_PERMISSION = "permission"
ERROR_UNKNOWN = "unknown"

DEFAULT_RATE_PER_SEC = 3.0
MAX_BACKOFF_RETRIES = 3


def _gh_token_from_cli() -> str | None:
    """Read the user's gh CLI token. Returns None if gh not available or not logged in."""
    if not shutil.which("gh"):
        return None
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def get_default_token() -> str:
    """Resolve a token from env (preferred for CI) or gh auth status."""
    for var in ("TACON_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    token = _gh_token_from_cli()
    if token:
        return token
    raise RuntimeError(
        "No GitHub token available. Set GITHUB_TOKEN, GH_TOKEN, or "
        "TACON_GITHUB_TOKEN, or run `gh auth login`."
    )


def classify_error(exc: BaseException) -> str:
    """Map an exception to one of the events.error_class enum values."""
    if isinstance(exc, RateLimitExceededException):
        return ERROR_RATE_LIMIT
    if isinstance(exc, UnknownObjectException):
        return ERROR_NOT_FOUND
    if isinstance(exc, GithubException):
        status = getattr(exc, "status", None)
        data = getattr(exc, "data", {}) or {}
        message = (data.get("message") or "").lower() if isinstance(data, dict) else ""
        if status == 401:
            return ERROR_AUTH
        if status == 403:
            if "saml" in message or "sso" in message:
                return ERROR_SSO
            if "abuse" in message or "secondary rate" in message:
                return ERROR_RATE_LIMIT
            return ERROR_PERMISSION
        if status == 404 or status == 410:
            return ERROR_NOT_FOUND
        if status == 409:
            return ERROR_CONFLICT
        if status == 422 and ("protected" in message or "branch" in message):
            return ERROR_PERMISSION
        if status == 422:
            return ERROR_CONFLICT
    # Network/timeout: requests + urllib3 raise from underneath PyGithub
    name = type(exc).__name__.lower()
    if any(token in name for token in ("timeout", "connection", "ssl", "dns")):
        return ERROR_NETWORK
    return ERROR_UNKNOWN


class RateLimitedClient:
    """Wraps PyGithub with a per-call throttle and retry-on-rate-limit.

    Call rate is enforced by sleeping after each call so the average stays
    at or below `rate_per_sec`. Sequential, not parallel.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        max_retries: int = MAX_BACKOFF_RETRIES,
    ) -> None:
        self._token = token or get_default_token()
        self._gh = Github(auth=Auth.Token(self._token), per_page=100)
        self._rate = max(rate_per_sec, 0.1)
        self._max_retries = max_retries
        self._next_call_at = 0.0

    @property
    def gh(self) -> Github:
        return self._gh

    def _throttle(self) -> None:
        now = time.monotonic()
        if now < self._next_call_at:
            time.sleep(self._next_call_at - now)
        self._next_call_at = time.monotonic() + (1.0 / self._rate)

    def call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute `fn(*args, **kwargs)` with throttle + retry on rate-limit/secondary."""
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                return fn(*args, **kwargs)
            except RateLimitExceededException as exc:
                if attempt >= self._max_retries:
                    raise
                # PyGithub stores reset time on the exception's headers
                wait = _retry_after_seconds(exc) or (2**attempt)
                time.sleep(min(wait, 60.0))
            except GithubException as exc:
                err_class = classify_error(exc)
                if err_class == ERROR_RATE_LIMIT and attempt < self._max_retries:
                    wait = _retry_after_seconds(exc) or (2**attempt)
                    time.sleep(min(wait, 60.0))
                    continue
                raise
        raise RuntimeError("unreachable: retry loop exhausted without raise")

    def get_repo(self, repo_id: str) -> Repository:
        repo: Repository = self.call(self._gh.get_repo, repo_id)
        return repo


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Best-effort extraction of Retry-After / X-RateLimit-Reset hint."""
    headers = getattr(exc, "headers", None) or {}
    if not isinstance(headers, dict):
        return None
    for key in ("retry-after", "Retry-After"):
        if key in headers:
            try:
                return float(headers[key])
            except (TypeError, ValueError):
                continue
    reset = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except (TypeError, ValueError):
            return None
    return None
