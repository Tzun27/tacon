"""Tests for AddBranchProtection (survey + write modes)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from github import GithubException, UnknownObjectException
from sqlite_utils import Database

from tacon.db import get_events_by_op
from tacon.ops import get_op_class
from tacon.ops._branch_protection_rule import BranchProtectionRule
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


def test_supports_rollback_is_true_at_class_level() -> None:
    """Class-level flag is True so the CLI lets rollback proceed; rollback()
    itself filters to status='applied' events, so survey ops are naturally
    excluded (their events are status='reported')."""
    assert AddBranchProtection.supports_rollback is True


def test_args_reports_survey_mode_when_rule_is_none() -> None:
    op = AddBranchProtection()
    assert op.args["mode"] == "report"
    assert op.args["rule"] is None


def test_args_reports_write_mode_when_rule_is_set() -> None:
    op = AddBranchProtection(rule=BranchProtectionRule(required_approving_review_count=1))
    assert op.args["mode"] == "write"
    assert isinstance(op.args["rule"], dict)
    assert op.args["rule"]["required_approving_review_count"] == 1


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


# ---------- write mode: plan ----------


def _branch_with_protection(protection: MagicMock | None) -> MagicMock:
    """Build a branch mock that returns the given protection (or unprotected)."""
    b = MagicMock(name="Branch")
    b.protected = protection is not None
    if protection is not None:
        b.get_protection.return_value = protection
    else:
        b.get_protection.side_effect = _missing()
    return b


def test_plan_write_mode_renders_diff_when_unprotected(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.return_value = _branch_with_protection(None)
    rule = BranchProtectionRule(required_approving_review_count=1, dismiss_stale_reviews=True)
    op = AddBranchProtection(rule=rule)
    diff = op.plan(tmp_db, fake_gh)

    assert len(diff.per_repo) == 3
    assert all(not r.blocked for r in diff.per_repo)
    assert all("set protection on 'main'" in r.summary for r in diff.per_repo)
    assert all("1 approval(s)" in r.summary for r in diff.per_repo)
    assert all("currently unprotected" in r.unified_diff for r in diff.per_repo)


def test_plan_write_mode_blocks_when_already_at_desired_state(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """If current protection already matches the rule, plan blocks (idempotent)."""
    p = _protection(approvals=1, contexts=[], strict=False)
    # also need require_code_owner_reviews=False; and dismiss_stale_reviews=False
    p.required_pull_request_reviews.dismiss_stale_reviews = False
    p.required_pull_request_reviews.require_code_owner_reviews = False
    p.allow_force_pushes = False
    p.allow_deletions = False
    p.required_linear_history = False
    fake_repo.get_branch.return_value = _branch_with_protection(p)

    rule = BranchProtectionRule(required_approving_review_count=1)
    op = AddBranchProtection(rule=rule)
    diff = op.plan(tmp_db, fake_gh)

    assert all(r.blocked for r in diff.per_repo)
    assert all("already at desired" in r.blocked_reason for r in diff.per_repo)


def test_plan_write_mode_blocks_when_branch_missing(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_branch.side_effect = _missing()
    op = AddBranchProtection(rule=BranchProtectionRule(required_approving_review_count=1))
    diff = op.plan(tmp_db, fake_gh)
    assert all(r.blocked for r in diff.per_repo)


# ---------- write mode: apply ----------


def test_apply_write_mode_records_applied_with_prior_state(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """A successful write records prior_state_json (here: null, was unprotected)."""
    fake_repo.get_branch.return_value = _branch_with_protection(None)
    rule = BranchProtectionRule(required_approving_review_count=2, dismiss_stale_reviews=True)
    op = AddBranchProtection(rule=rule)
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "applied" for r in result.per_repo)
    events = get_events_by_op(tmp_db, result.op_id, status="applied")
    assert len(events) == 3
    for e in events:
        assert e["prior_state_json"] == "null"

    # edit_protection was called with the rule's kwargs.
    call_kwargs_seen: list[dict] = []
    for c in fake_gh.call.call_args_list:
        # call(branch.edit_protection, **kwargs) — args[0] is the bound method
        if getattr(c.args[0], "_mock_name", "") == "edit_protection" or (
            hasattr(c.args[0], "__name__") and c.args[0].__name__ == "edit_protection"
        ):
            call_kwargs_seen.append(c.kwargs)
    # Hard to introspect MagicMock by name reliably; verify via side-effect:
    # the branch's edit_protection mock was called with the right kwargs at
    # least once per repo.
    assert fake_repo.get_branch.return_value.edit_protection.call_count == 3
    last_kwargs = (
        fake_repo.get_branch.return_value.edit_protection.call_args.kwargs
    )
    assert last_kwargs.get("required_approving_review_count") == 2
    assert last_kwargs.get("dismiss_stale_reviews") is True


def test_apply_write_mode_snapshots_prior_protection(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """When the branch was already protected, prior_state_json captures that."""
    p = _protection(approvals=1, contexts=["ci"], strict=False)
    p.required_pull_request_reviews.dismiss_stale_reviews = False
    p.required_pull_request_reviews.require_code_owner_reviews = False
    p.allow_force_pushes = False
    p.allow_deletions = False
    p.required_linear_history = False
    fake_repo.get_branch.return_value = _branch_with_protection(p)

    rule = BranchProtectionRule(
        required_approving_review_count=2, required_status_checks=("ci", "lint")
    )
    op = AddBranchProtection(rule=rule)
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)
    assert all(r.status == "applied" for r in result.per_repo)

    events = get_events_by_op(tmp_db, result.op_id, status="applied")
    for e in events:
        prior = json.loads(e["prior_state_json"])
        assert prior is not None
        assert prior["required_approving_review_count"] == 1
        assert prior["required_status_checks"] == ["ci"]


def test_apply_write_mode_classifies_403_as_permission(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """edit_protection raising a 403 → status='failed' with error_class='permission'."""
    branch_obj = _branch_with_protection(None)
    branch_obj.edit_protection.side_effect = GithubException(
        403, {"message": "Resource not accessible by personal access token"}, {}
    )
    fake_repo.get_branch.return_value = branch_obj

    op = AddBranchProtection(rule=BranchProtectionRule(required_approving_review_count=1))
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "failed" for r in result.per_repo)
    assert all(r.error_class == "permission" for r in result.per_repo)


# ---------- write mode: rollback ----------


def test_rollback_restores_when_was_unprotected(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """prior_state was null (unprotected) → rollback removes the protection."""
    fake_repo.get_branch.return_value = _branch_with_protection(None)

    rule = BranchProtectionRule(required_approving_review_count=1)
    op = AddBranchProtection(rule=rule)
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)
    op_id = apply_result.op_id

    # Set up branch_obj so that during rollback's drift check, the branch
    # appears to be at the rule we applied (the call to edit_protection by
    # apply() doesn't update fake_repo's state). Use a fresh mock.
    applied_protection = _protection(approvals=1, contexts=[], strict=False)
    applied_protection.required_pull_request_reviews.dismiss_stale_reviews = False
    applied_protection.required_pull_request_reviews.require_code_owner_reviews = (
        False
    )
    applied_protection.allow_force_pushes = False
    applied_protection.allow_deletions = False
    applied_protection.required_linear_history = False
    fake_repo.get_branch.return_value = _branch_with_protection(applied_protection)

    rb_result = AddBranchProtection.rollback(tmp_db, fake_gh, op_id)
    assert all(r.status == "rolled_back" for r in rb_result.per_repo)
    # remove_protection was called per-repo (since prior was null)
    assert fake_repo.get_branch.return_value.remove_protection.call_count == 3


def test_rollback_restores_prior_rule_state(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """prior_state was a real rule → rollback re-applies it via edit_protection."""
    prior = _protection(approvals=1, contexts=[], strict=False)
    prior.required_pull_request_reviews.dismiss_stale_reviews = False
    prior.required_pull_request_reviews.require_code_owner_reviews = False
    prior.allow_force_pushes = False
    prior.allow_deletions = False
    prior.required_linear_history = False
    fake_repo.get_branch.return_value = _branch_with_protection(prior)

    rule = BranchProtectionRule(required_approving_review_count=2)
    op = AddBranchProtection(rule=rule)
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)
    op_id = apply_result.op_id

    # Now set the "current" state to match the applied rule (drift check passes).
    applied_protection = _protection(approvals=2, contexts=[], strict=False)
    applied_protection.required_pull_request_reviews.dismiss_stale_reviews = False
    applied_protection.required_pull_request_reviews.require_code_owner_reviews = (
        False
    )
    applied_protection.allow_force_pushes = False
    applied_protection.allow_deletions = False
    applied_protection.required_linear_history = False
    branch_obj = _branch_with_protection(applied_protection)
    fake_repo.get_branch.return_value = branch_obj
    # Reset edit_protection's call_count so we only count rollback's call.
    branch_obj.edit_protection.reset_mock()

    rb_result = AddBranchProtection.rollback(tmp_db, fake_gh, op_id)
    assert all(r.status == "rolled_back" for r in rb_result.per_repo)
    # edit_protection called 3x with the prior rule (1 approval).
    assert branch_obj.edit_protection.call_count == 3
    last_kw = branch_obj.edit_protection.call_args.kwargs
    assert last_kw.get("required_approving_review_count") == 1


def test_rollback_skips_dirty_when_protection_drifted(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """If current protection doesn't match what we wrote, refuse to clobber."""
    fake_repo.get_branch.return_value = _branch_with_protection(None)
    rule = BranchProtectionRule(required_approving_review_count=2)
    op = AddBranchProtection(rule=rule)
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)
    op_id = apply_result.op_id

    # Now simulate drift: someone changed protection to 5 approvals.
    drifted = _protection(approvals=5, contexts=[], strict=False)
    drifted.required_pull_request_reviews.dismiss_stale_reviews = False
    drifted.required_pull_request_reviews.require_code_owner_reviews = False
    drifted.allow_force_pushes = False
    drifted.allow_deletions = False
    drifted.required_linear_history = False
    fake_repo.get_branch.return_value = _branch_with_protection(drifted)

    rb_result = AddBranchProtection.rollback(tmp_db, fake_gh, op_id)
    assert all(r.status == "skipped_dirty" for r in rb_result.per_repo)
    assert all("drifted" in (r.error_message or "") for r in rb_result.per_repo)


def test_rollback_of_survey_op_returns_empty(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """A survey op's events are status='reported', not 'applied' — rollback() filters
    to applied, so a survey op_id yields an empty result (no error, no work)."""
    fake_repo.get_branch.return_value = _branch_with_protection(None)
    op = AddBranchProtection()  # no rule → survey
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    rb_result = AddBranchProtection.rollback(tmp_db, fake_gh, apply_result.op_id)
    assert rb_result.per_repo == []


# ---------- describe / diff render ----------


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        (
            BranchProtectionRule(),
            "minimal protection",
        ),
        (
            BranchProtectionRule(
                required_approving_review_count=1, dismiss_stale_reviews=True
            ),
            "1 approval(s), dismiss-stale",
        ),
        (
            BranchProtectionRule(
                required_approving_review_count=2,
                enforce_admins=True,
                required_linear_history=True,
            ),
            "2 approval(s), admins enforced, linear-history",
        ),
    ],
)
def test_describe_rule(rule: BranchProtectionRule, expected: str) -> None:
    from tacon.ops.add_branch_protection import _describe_rule

    assert _describe_rule(rule) == expected
