"""Tests for AddBranchProtection (read-only survey)."""

from __future__ import annotations

from unittest.mock import MagicMock

from github import GithubException, UnknownObjectException
from sqlite_utils import Database

from tacon.db import get_events_by_op
from tacon.ops import get_op_class
from tacon.ops.add_branch_protection import AddBranchProtection


def _branch(protected: bool = True) -> MagicMock:
    b = MagicMock(name="Branch")
    b.protected = protected
    return b


def _protection(
    *, approvals: int | None = 1, contexts: list[str] | None = None, strict: bool = False
) -> MagicMock:
    p = MagicMock(name="BranchProtection")
    if approvals is not None:
        reviews = MagicMock(name="RequiredReviews")
        reviews.required_approving_review_count = approvals
        reviews.dismiss_stale_reviews = False
        p.required_pull_request_reviews = reviews
    else:
        p.required_pull_request_reviews = None
    sc = MagicMock(name="RequiredStatusChecks")
    sc.contexts = contexts or []
    sc.strict = strict
    p.required_status_checks = sc
    enforce = MagicMock()
    enforce.enabled = False
    p.enforce_admins = enforce
    return p


def _missing() -> UnknownObjectException:
    return UnknownObjectException(404, {"message": "Not Found"}, {})


# ---------- registry ----------


def test_add_branch_protection_is_registered() -> None:
    assert get_op_class("add-branch-protection") is AddBranchProtection


def test_supports_rollback_is_false() -> None:
    assert AddBranchProtection.supports_rollback is False


# ---------- plan ----------


def test_plan_reports_unprotected(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.return_value = _branch(protected=False)
    op = AddBranchProtection()
    diff = op.plan(tmp_db, fake_gh)

    assert len(diff.per_repo) == 3
    assert all("protected: NO" in r.summary for r in diff.per_repo)
    assert not any(r.blocked for r in diff.per_repo)


def test_plan_reports_protected_with_details(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.return_value = _branch(protected=True)
    fake_repo.get_branch.return_value.get_protection.return_value = _protection(
        approvals=2, contexts=["ci/lint", "ci/test"], strict=True
    )
    op = AddBranchProtection()
    diff = op.plan(tmp_db, fake_gh)

    assert all("protected: YES" in r.summary for r in diff.per_repo)
    assert all("2 approval" in r.summary for r in diff.per_repo)
    assert all("2 status check" in r.summary for r in diff.per_repo)
    assert all("strict" in r.summary for r in diff.per_repo)


def test_plan_blocks_when_branch_missing(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.side_effect = _missing()
    op = AddBranchProtection(branch="custom-branch")
    diff = op.plan(tmp_db, fake_gh)

    assert all(r.blocked for r in diff.per_repo)
    assert all("'custom-branch' missing" in r.blocked_reason for r in diff.per_repo)


def test_plan_handles_get_protection_404(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """branch.protected=True but get_protection() 404s (token lacks admin scope)."""
    fake_repo.get_branch.return_value = _branch(protected=True)
    fake_repo.get_branch.return_value.get_protection.side_effect = _missing()
    op = AddBranchProtection()
    diff = op.plan(tmp_db, fake_gh)

    assert all("details restricted" in r.summary for r in diff.per_repo)
    assert not any(r.blocked for r in diff.per_repo)


def test_plan_handles_protection_read_error(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.return_value = _branch(protected=True)
    fake_repo.get_branch.return_value.get_protection.side_effect = GithubException(
        500, {"message": "boom"}, {}
    )
    op = AddBranchProtection()
    diff = op.plan(tmp_db, fake_gh)
    assert all("read failed" in r.summary for r in diff.per_repo)


def test_plan_handles_unreachable_repo(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.side_effect = GithubException(500, {"message": "boom"}, {})
    op = AddBranchProtection()
    diff = op.plan(tmp_db, fake_gh)
    assert all(r.blocked for r in diff.per_repo)
    assert all("inspect failed" in r.blocked_reason for r in diff.per_repo)


# ---------- apply (audit-only) ----------


def test_apply_writes_reported_events(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.return_value = _branch(protected=False)
    op = AddBranchProtection()
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "reported" for r in result.per_repo)
    events = get_events_by_op(tmp_db, result.op_id)
    assert len(events) == 3
    assert all(e["status"] == "reported" for e in events)
    assert all(e["op_class"] == "add_branch_protection" for e in events)
    # The summary lands in error_message for audit.
    assert all("protected: NO" in (e["error_message"] or "") for e in events)


def test_apply_records_blocked_as_skipped(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.side_effect = _missing()
    op = AddBranchProtection(branch="nonexistent")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)
    assert all(r.status == "skipped" for r in result.per_repo)


def test_apply_respects_confirm_no(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.return_value = _branch(protected=False)
    op = AddBranchProtection()
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: False)
    assert all(r.status == "skipped" for r in result.per_repo)
