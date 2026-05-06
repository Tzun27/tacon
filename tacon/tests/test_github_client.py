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

    def test_call_retries_on_secondary_rate_via_github_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """403 + 'abuse' message classifies as rate_limit and gets retried."""
        client = RateLimitedClient.__new__(RateLimitedClient)
        client._token = "fake"  # type: ignore[attr-defined]
        client._gh = MagicMock()  # type: ignore[attr-defined]
        client._rate = 1000.0  # type: ignore[attr-defined]
        client._max_retries = 2  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]
        monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

        attempts: list[int] = []

        def flaky() -> str:
            attempts.append(1)
            if len(attempts) < 2:
                raise GithubException(403, {"message": "abuse detection"}, {})
            return "ok"

        assert client.call(flaky) == "ok"
        assert len(attempts) == 2

    def test_call_does_not_retry_other_github_exceptions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = RateLimitedClient.__new__(RateLimitedClient)
        client._token = "fake"  # type: ignore[attr-defined]
        client._gh = MagicMock()  # type: ignore[attr-defined]
        client._rate = 1000.0  # type: ignore[attr-defined]
        client._max_retries = 5  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]
        monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)

        attempts: list[int] = []

        def boom() -> None:
            attempts.append(1)
            raise GithubException(404, {"message": "Not Found"}, {})

        with pytest.raises(GithubException):
            client.call(boom)
        # 404 is not retryable
        assert len(attempts) == 1

    def test_get_repo_calls_through(self) -> None:
        """get_repo should call self.call with self._gh.get_repo."""
        client = RateLimitedClient.__new__(RateLimitedClient)
        client._token = "fake"  # type: ignore[attr-defined]
        gh_mock = MagicMock()
        gh_mock.get_repo.return_value = "fake-repo-obj"
        client._gh = gh_mock  # type: ignore[attr-defined]
        client._rate = 1000.0  # type: ignore[attr-defined]
        client._max_retries = 0  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]

        assert client.get_repo("owner/repo") == "fake-repo-obj"
        gh_mock.get_repo.assert_called_once_with("owner/repo")

    def test_gh_property_returns_underlying_github(self) -> None:
        client = RateLimitedClient.__new__(RateLimitedClient)
        gh_mock = MagicMock()
        client._token = "fake"  # type: ignore[attr-defined]
        client._gh = gh_mock  # type: ignore[attr-defined]
        client._rate = 1.0  # type: ignore[attr-defined]
        client._max_retries = 0  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]
        assert client.gh is gh_mock


# ---------- _retry_after_seconds ----------


class TestRetryAfterSeconds:
    def _exc_with_headers(self, headers: dict[str, str] | None) -> Exception:
        exc = MagicMock()
        exc.headers = headers
        return exc  # type: ignore[return-value]

    def test_returns_none_when_headers_missing(self) -> None:
        from tacon.github_client import _retry_after_seconds

        assert _retry_after_seconds(self._exc_with_headers(None)) is None

    def test_returns_none_when_headers_not_dict(self) -> None:
        from tacon.github_client import _retry_after_seconds

        assert _retry_after_seconds(self._exc_with_headers(["not", "a", "dict"])) is None  # type: ignore[arg-type]

    def test_parses_lowercase_retry_after(self) -> None:
        from tacon.github_client import _retry_after_seconds

        exc = self._exc_with_headers({"retry-after": "30"})
        assert _retry_after_seconds(exc) == 30.0

    def test_parses_mixed_case_retry_after(self) -> None:
        from tacon.github_client import _retry_after_seconds

        exc = self._exc_with_headers({"Retry-After": "5"})
        assert _retry_after_seconds(exc) == 5.0

    def test_parses_x_ratelimit_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tacon.github_client import _retry_after_seconds

        monkeypatch.setattr(time, "time", lambda: 1000.0)
        exc = self._exc_with_headers({"x-ratelimit-reset": "1042"})
        assert _retry_after_seconds(exc) == 42.0

    def test_x_ratelimit_reset_clamped_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If reset is in the past, return 0 not negative."""
        from tacon.github_client import _retry_after_seconds

        monkeypatch.setattr(time, "time", lambda: 9999.0)
        exc = self._exc_with_headers({"x-ratelimit-reset": "1000"})
        assert _retry_after_seconds(exc) == 0.0

    def test_returns_none_for_malformed_value(self) -> None:
        from tacon.github_client import _retry_after_seconds

        exc = self._exc_with_headers({"retry-after": "not-a-number"})
        assert _retry_after_seconds(exc) is None

    def test_returns_none_for_malformed_x_ratelimit_reset(self) -> None:
        from tacon.github_client import _retry_after_seconds

        exc = self._exc_with_headers({"x-ratelimit-reset": "not-a-number"})
        assert _retry_after_seconds(exc) is None

    def test_call_uses_retry_after_header_for_sleep_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify that retry-after is consulted (not just exponential)."""
        client = RateLimitedClient.__new__(RateLimitedClient)
        client._token = "fake"  # type: ignore[attr-defined]
        client._gh = MagicMock()  # type: ignore[attr-defined]
        client._rate = 1000.0  # type: ignore[attr-defined]
        client._max_retries = 2  # type: ignore[attr-defined]
        client._next_call_at = 0.0  # type: ignore[attr-defined]

        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", sleeps.append)

        attempts: list[int] = []

        def flaky() -> str:
            attempts.append(1)
            if len(attempts) < 2:
                raise RateLimitExceededException(
                    403, {"message": "rate"}, {"retry-after": "7"}
                )
            return "ok"

        assert client.call(flaky) == "ok"
        # Second call's sleep should be ~7 (the retry-after value)
        assert any(abs(s - 7.0) < 0.01 for s in sleeps), f"sleeps={sleeps}"


# ---------- token resolution ----------


class TestTokenResolution:
    def test_get_default_token_prefers_tacon_github_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.setenv("TACON_GITHUB_TOKEN", "tacon-tok")
        monkeypatch.setenv("GITHUB_TOKEN", "gh-tok")
        monkeypatch.setenv("GH_TOKEN", "gh-other-tok")
        assert github_client.get_default_token() == "tacon-tok"

    def test_get_default_token_falls_through_to_github_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.delenv("TACON_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "gh-tok")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        assert github_client.get_default_token() == "gh-tok"

    def test_get_default_token_falls_back_to_gh_cli(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.delenv("TACON_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setattr(github_client, "_gh_token_from_cli", lambda: "from-gh-cli")
        assert github_client.get_default_token() == "from-gh-cli"

    def test_get_default_token_raises_when_nothing_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.delenv("TACON_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setattr(github_client, "_gh_token_from_cli", lambda: None)
        with pytest.raises(RuntimeError, match="No GitHub token"):
            github_client.get_default_token()

    def test_gh_token_from_cli_returns_none_when_gh_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.setattr(github_client.shutil, "which", lambda _: None)
        assert github_client._gh_token_from_cli() is None

    def test_gh_token_from_cli_returns_token_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.setattr(github_client.shutil, "which", lambda _: "/usr/bin/gh")

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "ghp_real_token\n"
        monkeypatch.setattr(github_client.subprocess, "run", lambda *_a, **_kw: fake_result)
        assert github_client._gh_token_from_cli() == "ghp_real_token"

    def test_gh_token_from_cli_returns_none_on_nonzero_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.setattr(github_client.shutil, "which", lambda _: "/usr/bin/gh")
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        monkeypatch.setattr(github_client.subprocess, "run", lambda *_a, **_kw: fake_result)
        assert github_client._gh_token_from_cli() is None

    def test_gh_token_from_cli_returns_none_on_empty_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.setattr(github_client.shutil, "which", lambda _: "/usr/bin/gh")
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "  \n"
        monkeypatch.setattr(github_client.subprocess, "run", lambda *_a, **_kw: fake_result)
        assert github_client._gh_token_from_cli() is None

    def test_gh_token_from_cli_handles_subprocess_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.setattr(github_client.shutil, "which", lambda _: "/usr/bin/gh")

        def boom(*_a, **_kw):
            raise github_client.subprocess.TimeoutExpired(cmd=["gh"], timeout=5)

        monkeypatch.setattr(github_client.subprocess, "run", boom)
        assert github_client._gh_token_from_cli() is None

    def test_gh_token_from_cli_handles_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.setattr(github_client.shutil, "which", lambda _: "/usr/bin/gh")

        def boom(*_a, **_kw):
            raise OSError("permission denied")

        monkeypatch.setattr(github_client.subprocess, "run", boom)
        assert github_client._gh_token_from_cli() is None


class TestRateLimitedClientConstructor:
    def test_init_uses_explicit_token_without_calling_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        called = {"resolver": False}

        def fake_resolver() -> str:
            called["resolver"] = True
            return "should-not-be-used"

        monkeypatch.setattr(github_client, "get_default_token", fake_resolver)

        # Patch Github so we don't really hit the API on init
        fake_github_cls = MagicMock(name="GithubCls")
        monkeypatch.setattr(github_client, "Github", fake_github_cls)

        client = RateLimitedClient(token="explicit-tok", rate_per_sec=2.0, max_retries=5)
        assert called["resolver"] is False
        fake_github_cls.assert_called_once_with("explicit-tok", per_page=100)
        assert client._rate == 2.0  # type: ignore[attr-defined]
        assert client._max_retries == 5  # type: ignore[attr-defined]

    def test_init_resolves_token_when_none_supplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tacon import github_client

        monkeypatch.setattr(github_client, "get_default_token", lambda: "resolved")
        fake_github_cls = MagicMock(name="GithubCls")
        monkeypatch.setattr(github_client, "Github", fake_github_cls)

        RateLimitedClient(rate_per_sec=3.0)
        fake_github_cls.assert_called_once_with("resolved", per_page=100)

    def test_init_clamps_rate_to_minimum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tacon import github_client

        monkeypatch.setattr(github_client, "get_default_token", lambda: "tok")
        monkeypatch.setattr(github_client, "Github", MagicMock())

        client = RateLimitedClient(rate_per_sec=0.0001)
        # Constructor floors at 0.1
        assert client._rate >= 0.1  # type: ignore[attr-defined]
