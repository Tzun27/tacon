"""Tests for tacon/ops/_via_pr.py — branch + PR helpers used by --via-pr mode.

Each test mocks the RateLimitedClient at the gh.call boundary so we can
drive the GitHub API surface synthetically without any network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from github import GithubException, UnknownObjectException

from tacon.ops._via_pr import (
    BranchConflictError,
    RollbackOutcome,
    close_pr_and_delete_branch,
    ensure_branch,
    open_or_find_pr,
    via_pr_branch_name,
)

# ---------- branch naming ----------


def test_via_pr_branch_name_kebab_cases_and_truncates() -> None:
    """`add_file` becomes `add-file`; the op_id prefix is 8 hex chars (no dashes)."""
    name = via_pr_branch_name("add_file", "bc247dc1-7db9-4398-9000-deadbeef0000")
    assert name == "tacon/add-file-bc247dc1"


def test_via_pr_branch_name_handles_kebab_already() -> None:
    """If somehow op_class is already kebab-cased, no double-conversion."""
    name = via_pr_branch_name("custom-op", "00000000-0000-0000-0000-000000000000")
    assert name == "tacon/custom-op-00000000"


# ---------- ensure_branch ----------


def _gh_with_repo(repo: MagicMock) -> MagicMock:
    """Build a fake RateLimitedClient that runs `fn(*args, **kwargs)` directly."""
    gh = MagicMock(name="RateLimitedClient")
    gh.get_repo.return_value = repo
    gh.call.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    return gh


def test_ensure_branch_creates_when_absent() -> None:
    repo = MagicMock(name="Repository")
    repo.create_git_ref.return_value = MagicMock()
    gh = _gh_with_repo(repo)

    result = ensure_branch(gh, repo, "tacon/add-file-aaaaaaaa", "deadbeef")

    assert result == "created"
    repo.create_git_ref.assert_called_once()
    kwargs = repo.create_git_ref.call_args.kwargs
    assert kwargs["ref"] == "refs/heads/tacon/add-file-aaaaaaaa"
    assert kwargs["sha"] == "deadbeef"


def test_ensure_branch_no_op_when_same_sha() -> None:
    repo = MagicMock(name="Repository")
    repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Reference already exists"}, {}
    )
    existing_ref = MagicMock()
    existing_ref.object.sha = "deadbeef"
    repo.get_git_ref.return_value = existing_ref
    gh = _gh_with_repo(repo)

    result = ensure_branch(gh, repo, "tacon/add-file-aaaaaaaa", "deadbeef")

    assert result == "exists_same"
    repo.get_git_ref.assert_called_once_with("heads/tacon/add-file-aaaaaaaa")


def test_ensure_branch_raises_on_different_sha() -> None:
    repo = MagicMock(name="Repository")
    repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Reference already exists"}, {}
    )
    existing_ref = MagicMock()
    existing_ref.object.sha = "cafef00d"
    repo.get_git_ref.return_value = existing_ref
    gh = _gh_with_repo(repo)

    with pytest.raises(BranchConflictError) as excinfo:
        ensure_branch(gh, repo, "tacon/add-file-aaaaaaaa", "deadbeef")
    assert "cafef00d" in str(excinfo.value)
    assert excinfo.value.expected_sha == "deadbeef"


def test_ensure_branch_propagates_unrelated_422() -> None:
    """A 422 that's not 'Reference already exists' (e.g. invalid sha) propagates."""
    repo = MagicMock(name="Repository")
    repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Validation failed: sha not found"}, {}
    )
    gh = _gh_with_repo(repo)

    with pytest.raises(GithubException):
        ensure_branch(gh, repo, "tacon/add-file-aaaaaaaa", "deadbeef")


def test_ensure_branch_propagates_403() -> None:
    """Permission errors propagate so the op layer maps to error_class='permission'."""
    repo = MagicMock(name="Repository")
    repo.create_git_ref.side_effect = GithubException(403, {"message": "Forbidden"}, {})
    gh = _gh_with_repo(repo)

    with pytest.raises(GithubException) as excinfo:
        ensure_branch(gh, repo, "tacon/add-file-aaaaaaaa", "deadbeef")
    assert excinfo.value.status == 403


# ---------- open_or_find_pr ----------


def test_open_or_find_pr_creates_new() -> None:
    repo = MagicMock(name="Repository")
    new_pr = MagicMock()
    new_pr.number = 7
    repo.create_pull.return_value = new_pr
    gh = _gh_with_repo(repo)

    n = open_or_find_pr(
        gh, repo,
        branch="tacon/add-file-aaaaaaaa",
        base="main",
        title="tacon: add starter",
        body="body",
    )
    assert n == 7
    repo.create_pull.assert_called_once_with(
        title="tacon: add starter",
        body="body",
        base="main",
        head="tacon/add-file-aaaaaaaa",
    )


def test_open_or_find_pr_finds_existing_when_already_open() -> None:
    repo = MagicMock(name="Repository")
    repo.create_pull.side_effect = GithubException(
        422,
        {
            "message": "Validation Failed",
            "errors": [{"message": "A pull request already exists for cs101:tacon/add-file-aaaaaaaa."}],
        },
        {},
    )
    existing = MagicMock()
    existing.number = 42
    existing.head.ref = "tacon/add-file-aaaaaaaa"
    repo.get_pulls.return_value = [existing]
    gh = _gh_with_repo(repo)

    n = open_or_find_pr(
        gh, repo,
        branch="tacon/add-file-aaaaaaaa",
        base="main",
        title="t",
        body="b",
    )
    assert n == 42


def test_open_or_find_pr_propagates_non_already_exists_422() -> None:
    repo = MagicMock(name="Repository")
    repo.create_pull.side_effect = GithubException(
        422, {"message": "head sha can't be empty"}, {}
    )
    gh = _gh_with_repo(repo)

    with pytest.raises(GithubException):
        open_or_find_pr(gh, repo, branch="x", base="main", title="t", body="b")


def test_open_or_find_pr_finds_existing_via_top_level_message() -> None:
    """Some GitHub responses put the marker on the top-level message rather than errors[]."""
    repo = MagicMock(name="Repository")
    repo.create_pull.side_effect = GithubException(
        422, {"message": "A pull request already exists"}, {}
    )
    existing = MagicMock()
    existing.number = 5
    existing.head.ref = "x"
    repo.get_pulls.return_value = [existing]
    gh = _gh_with_repo(repo)

    n = open_or_find_pr(gh, repo, branch="x", base="main", title="t", body="b")
    assert n == 5


def test_open_or_find_pr_raises_when_get_pulls_lies() -> None:
    """Defense-in-depth: if create_pull says 'already exists' but get_pulls returns
    nothing matching, surface a clear runtime error rather than silently looping."""
    repo = MagicMock(name="Repository")
    repo.create_pull.side_effect = GithubException(
        422, {"message": "A pull request already exists"}, {}
    )
    repo.get_pulls.return_value = []
    gh = _gh_with_repo(repo)

    with pytest.raises(RuntimeError, match="get_pulls did not return"):
        open_or_find_pr(gh, repo, branch="x", base="main", title="t", body="b")


# ---------- close_pr_and_delete_branch ----------


def _pull(*, number: int = 1, state: str = "open", merged: bool = False) -> MagicMock:
    pr = MagicMock(name=f"PR#{number}")
    pr.number = number
    pr.state = state
    pr.merged = merged
    pr.merge_commit_sha = "abcd1234deadbeef" if merged else None
    return pr


def test_close_pr_open_with_branch_present_rolls_back() -> None:
    repo = MagicMock(name="Repository")
    pr = _pull(number=7, state="open")
    repo.get_pull.return_value = pr
    branch_ref = MagicMock()
    repo.get_git_ref.return_value = branch_ref
    gh = _gh_with_repo(repo)

    outcome = close_pr_and_delete_branch(
        gh, repo, pr_number=7, branch="tacon/add-file-aaaaaaaa"
    )
    assert outcome == RollbackOutcome(
        status="rolled_back",
        pr_state="open",
        branch_deleted=True,
        note="",
    )
    pr.edit.assert_called_once_with(state="closed")
    branch_ref.delete.assert_called_once()


def test_close_pr_open_branch_missing_rolls_back() -> None:
    repo = MagicMock(name="Repository")
    pr = _pull(number=7, state="open")
    repo.get_pull.return_value = pr
    repo.get_git_ref.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    gh = _gh_with_repo(repo)

    outcome = close_pr_and_delete_branch(
        gh, repo, pr_number=7, branch="tacon/add-file-aaaaaaaa"
    )
    assert outcome.status == "rolled_back"
    assert outcome.pr_state == "open"
    assert outcome.branch_deleted is True  # already-absent branches count as success


def test_close_pr_already_closed_branch_present_rolls_back() -> None:
    repo = MagicMock(name="Repository")
    pr = _pull(number=7, state="closed", merged=False)
    repo.get_pull.return_value = pr
    branch_ref = MagicMock()
    repo.get_git_ref.return_value = branch_ref
    gh = _gh_with_repo(repo)

    outcome = close_pr_and_delete_branch(
        gh, repo, pr_number=7, branch="tacon/add-file-aaaaaaaa"
    )
    assert outcome.status == "rolled_back"
    assert outcome.pr_state == "closed"
    pr.edit.assert_not_called()
    branch_ref.delete.assert_called_once()


def test_close_pr_merged_skipped_dirty() -> None:
    repo = MagicMock(name="Repository")
    pr = _pull(number=7, state="closed", merged=True)
    repo.get_pull.return_value = pr
    gh = _gh_with_repo(repo)

    outcome = close_pr_and_delete_branch(
        gh, repo, pr_number=7, branch="tacon/add-file-aaaaaaaa"
    )
    assert outcome.status == "skipped_dirty"
    assert outcome.pr_state == "merged"
    assert outcome.branch_deleted is False
    assert "merged" in outcome.note.lower()
    assert "abcd1234" in outcome.note  # truncated merge sha visible
    pr.edit.assert_not_called()


def test_close_pr_not_found_is_idempotent() -> None:
    repo = MagicMock(name="Repository")
    repo.get_pull.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    branch_ref = MagicMock()
    repo.get_git_ref.return_value = branch_ref
    gh = _gh_with_repo(repo)

    outcome = close_pr_and_delete_branch(
        gh, repo, pr_number=99, branch="tacon/add-file-aaaaaaaa"
    )
    assert outcome.status == "rolled_back"
    assert outcome.pr_state == "not_found"
    branch_ref.delete.assert_called_once()


def test_close_pr_branch_delete_permission_failure_does_not_block_rollback() -> None:
    """Rollback's main payload is closing the PR. If branch-delete fails (token lacks
    permission), the rollback still counts as rolled_back; we just note that the
    branch wasn't cleaned up.
    """
    repo = MagicMock(name="Repository")
    pr = _pull(number=7, state="open")
    repo.get_pull.return_value = pr
    branch_ref = MagicMock()
    branch_ref.delete.side_effect = GithubException(403, {"message": "Forbidden"}, {})
    repo.get_git_ref.return_value = branch_ref
    gh = _gh_with_repo(repo)

    outcome = close_pr_and_delete_branch(
        gh, repo, pr_number=7, branch="tacon/add-file-aaaaaaaa"
    )
    assert outcome.status == "rolled_back"
    assert outcome.branch_deleted is False
    assert "could not be deleted" in outcome.note
    pr.edit.assert_called_once_with(state="closed")
