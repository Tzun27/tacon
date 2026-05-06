"""Tests for tacon.github_client (classify_error + RateLimitedClient throttle)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from github import GithubException, RateLimitExceededException, UnknownObjectException

from tacon.github_client import (
    ERROR_AUTH,
    ERROR_CONFLICT,
    ERROR_NETWORK,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_RATE_LIMIT,
    ERROR_SSO,
    ERROR_UNKNOWN,
    RateLimitedClient,
    classify_error,
)


def _gh_exc(
    status: int, message: str = "", *, headers: dict[str, str] | None = None
) -> GithubException:
    return GithubException(status, {"message": message}, headers or {})


class TestClassifyError:
    def test_rate_limit_exception_class(self) -> None:
        exc = RateLimitExceededException(403, {"message": "rate limit"}, {})
        assert classify_error(exc) == ERROR_RATE_LIMIT

    def test_unknown_object_class(self) -> None:
        exc = UnknownObjectException(404, {"message": "Not Found"}, {})
        assert classify_error(exc) == ERROR_NOT_FOUND

    def test_401_auth(self) -> None:
        assert classify_error(_gh_exc(401, "Bad credentials")) == ERROR_AUTH

    def test_403_sso(self) -> None:
        assert (
            classify_error(_gh_exc(403, "Resource protected by organization SAML SSO")) == ERROR_SSO
        )

    def test_403_secondary_rate(self) -> None:
        assert (
            classify_error(_gh_exc(403, "You have triggered an abuse detection mechanism"))
            == ERROR_RATE_LIMIT
        )

    def test_403_permission(self) -> None:
        assert (
            classify_error(_gh_exc(403, "Resource not accessible by integration"))
            == ERROR_PERMISSION
        )

    def test_404(self) -> None:
        assert classify_error(_gh_exc(404, "Not Found")) == ERROR_NOT_FOUND

    def test_410_gone_treated_as_not_found(self) -> None:
        assert classify_error(_gh_exc(410, "Gone")) == ERROR_NOT_FOUND

    def test_409_conflict(self) -> None:
        assert classify_error(_gh_exc(409, "Conflict")) == ERROR_CONFLICT

    def test_422_branch_protection_is_permission(self) -> None:
        assert classify_error(_gh_exc(422, "branch protection rule violated")) == ERROR_PERMISSION

    def test_422_other_is_conflict(self) -> None:
        assert classify_error(_gh_exc(422, "Validation failed")) == ERROR_CONFLICT

    def test_network_timeout(self) -> None:
        class ConnectionTimeoutError(Exception):
            pass

        assert classify_error(ConnectionTimeoutError("timed out")) == ERROR_NETWORK

    def test_unknown_falls_back(self) -> None:
        assert classify_error(ValueError("???")) == ERROR_UNKNOWN


class TestRateLimitedClientThrottle:
    def test_throttle_spaces_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Build a client without hitting GitHub
        client = RateLimitedClient.__new__(RateLimitedClient)
        client._token = "fake"  # type: ignore[attr-defined]
        client._gh = MagicMock()  # type: ignore[attr-defined]
        client._rate = 10.0  # 10 req/sec -> 0.1s spacing  # type: ignore[attr-defined]
        client._max_retries = 3  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]

        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", sleeps.append)
        # First call: no backlog, no sleep. Subsequent: should request a spacer.
        client.call(lambda: "ok")
        client.call(lambda: "ok")
        # At least one positive sleep should have been requested for the second call
        assert any(s > 0 for s in sleeps)

    def test_call_passes_through_args(self) -> None:
        client = RateLimitedClient.__new__(RateLimitedClient)
        client._token = "fake"  # type: ignore[attr-defined]
        client._gh = MagicMock()  # type: ignore[attr-defined]
        client._rate = 1000.0  # type: ignore[attr-defined]
        client._max_retries = 0  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]

        result = client.call(lambda a, b, c=3: (a, b, c), 1, 2, c=42)
        assert result == (1, 2, 42)

    def test_call_retries_on_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = RateLimitedClient.__new__(RateLimitedClient)
        client._token = "fake"  # type: ignore[attr-defined]
        client._gh = MagicMock()  # type: ignore[attr-defined]
        client._rate = 1000.0  # type: ignore[attr-defined]
        client._max_retries = 2  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]
        monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

        calls: list[int] = []

        def flaky() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise RateLimitExceededException(403, {"message": "rate"}, {})
            return "ok"

        assert client.call(flaky) == "ok"
        assert len(calls) == 2

    def test_call_raises_after_exhausting_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = RateLimitedClient.__new__(RateLimitedClient)
        client._token = "fake"  # type: ignore[attr-defined]
        client._gh = MagicMock()  # type: ignore[attr-defined]
        client._rate = 1000.0  # type: ignore[attr-defined]
        client._max_retries = 1  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]
        monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

        def always_rate_limited() -> None:
            raise RateLimitExceededException(403, {"message": "rate"}, {})

        with pytest.raises(RateLimitExceededException):
            client.call(always_rate_limited)
