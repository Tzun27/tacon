"""Read-only live tests against the real GitHub API.

Verifies that:
  - The token resolves and authenticates.
  - We can list repos in the configured org/assignment.
  - The scope guard correctly accepts in-scope names and rejects others.

Nothing in this file ever calls a write API.
"""

from __future__ import annotations

import pytest

from tacon.github_client import RateLimitedClient
from tests.live.conftest import (
    ASSIGNMENT_PREFIX,
    TEST_ORG,
    OutOfScopeError,
    assert_in_scope,
)


def test_token_authenticates(live_client: RateLimitedClient) -> None:
    """The token must be valid; the authenticated user lookup is read-only."""
    user = live_client.call(live_client.gh.get_user)
    assert user.login  # any non-empty login proves auth worked


def test_org_is_visible(live_client: RateLimitedClient) -> None:
    org = live_client.call(live_client.gh.get_organization, TEST_ORG)
    assert org.login == TEST_ORG


def test_assignment_repos_match_scope(assignment_repos: list[str]) -> None:
    """Every repo discovered for the assignment must pass the scope guard."""
    assert assignment_repos, (
        f"expected at least one repo containing {ASSIGNMENT_PREFIX!r} "
        f"in {TEST_ORG!r}; found none"
    )
    for full_name in assignment_repos:
        assert_in_scope(full_name)  # guard never fails on its own list


def test_can_read_first_repo_metadata(
    live_client: RateLimitedClient, assignment_repos: list[str]
) -> None:
    full_name = assignment_repos[0]
    assert_in_scope(full_name)
    repo = live_client.get_repo(full_name)
    assert repo.full_name == full_name
    # default_branch is what apply()/rollback() target by default
    assert repo.default_branch in ("main", "master")


# ---------- scope guard self-tests ----------


def test_scope_guard_rejects_wrong_org() -> None:
    with pytest.raises(OutOfScopeError, match="not touch repos outside"):
        assert_in_scope(f"some-other-org/{ASSIGNMENT_PREFIX}-anything")


def test_scope_guard_rejects_unmarked_repo() -> None:
    with pytest.raises(OutOfScopeError, match="assignment prefix"):
        assert_in_scope(f"{TEST_ORG}/totally-unrelated-repo")


def test_scope_guard_rejects_malformed_name() -> None:
    with pytest.raises(OutOfScopeError, match="malformed"):
        assert_in_scope("nopath")
