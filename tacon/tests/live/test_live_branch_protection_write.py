"""Live AddBranchProtection write-mode apply + rollback round-trip.

Most TA tokens lack admin scope on classroom repos; that's the realistic
case. So this test:

  1. Reads the current protection state via the survey path (no admin
     needed).
  2. Attempts to apply a tacon-default-shaped rule.
  3. **If the apply fails with a 403** (token lacks admin), pytest.skips
     with a clear message — the test is harmless on TA tokens.
  4. Otherwise, verifies the protection landed, then rolls back to the
     prior state and verifies the prior state was restored.

try/finally cleanup is best-effort: if the rollback path leaves the
test branch in a non-original state (e.g. mid-flight crash), we attempt
a manual restore using the snapshot we captured at the start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from github import GithubException

from tacon.db import (
    open_db,
    upsert_assignment,
    upsert_repo,
    upsert_student,
)
from tacon.github_client import RateLimitedClient
from tacon.ops._branch_protection_rule import load_rule_template
from tacon.ops.add_branch_protection import (
    AddBranchProtection,
    _protection_to_rule_dict,
)
from tests.live.conftest import assert_in_scope


def _read_protection(
    client: RateLimitedClient, repo_full_name: str, branch_name: str
) -> tuple[bool, dict[str, Any] | None]:
    """Return (is_protected, rule-shaped-dict-or-None)."""
    repo = client.get_repo(repo_full_name)
    branch_obj = client.call(repo.get_branch, branch_name)
    if not bool(getattr(branch_obj, "protected", False)):
        return False, None
    try:
        protection = client.call(branch_obj.get_protection)
    except GithubException:
        return True, None
    return True, _protection_to_rule_dict(protection)


def test_branch_protection_write_apply_then_rollback(
    live_client: RateLimitedClient,
    write_target_repo: str,
    tmp_path: Path,
) -> None:
    """Write tacon-default protection, verify, rollback to prior state, verify."""

    assert_in_scope(write_target_repo)

    # --- snapshot the prior state so cleanup can restore it ---
    repo = live_client.get_repo(write_target_repo)
    target_branch = repo.default_branch
    was_protected, prior_rule_dict = _read_protection(
        live_client, write_target_repo, target_branch
    )

    # --- seed an in-memory DB ---
    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-live-bpw",
        classroom_id="cls-live",
        title="live add-branch-protection write e2e",
        slug="live-bpw",
        starter_repo=None,
        created_at="2026-05-07T00:00:00Z",
    )
    student_id = upsert_student(db, username="tacon-live-bpw")
    upsert_repo(
        db,
        id=write_target_repo,
        assignment_id="asn-live-bpw",
        student_id=student_id,
    )

    rule = load_rule_template("tacon-default")
    op = AddBranchProtection(assignment_id="asn-live-bpw", rule=rule)

    state: dict[str, Any] = {"op_id": "", "wrote_protection": False}
    try:
        diff = op.plan(db, live_client)
        assert len(diff.per_repo) == 1
        per = diff.per_repo[0]
        # If the repo is *already* at tacon-default, plan blocks. Skip — there
        # is nothing useful to test here without first changing state, which
        # we don't want to do speculatively.
        if per.blocked and "already at desired" in per.blocked_reason:
            pytest.skip(
                f"{write_target_repo}@{target_branch} already at the "
                "tacon-default rule; nothing to write or roll back"
            )

        try:
            result = op.apply(db, live_client, diff, confirm=lambda _r: True)
        except GithubException as exc:
            if int(getattr(exc, "status", 0)) == 403:
                pytest.skip(
                    "token lacks admin scope on the test repo "
                    f"({exc}); skipping write-mode live test"
                )
            raise
        per_apply = result.per_repo[0]
        if per_apply.status == "failed" and per_apply.error_class == "permission":
            pytest.skip(
                "token lacks admin scope on the test repo (per-repo "
                f"permission error: {per_apply.error_message}); skipping"
            )
        assert per_apply.status == "applied", (
            f"apply did not succeed: status={per_apply.status} "
            f"err={per_apply.error_class!r} {per_apply.error_message!r}"
        )
        state["op_id"] = result.op_id
        state["wrote_protection"] = True

        # --- verify the protection landed in tacon-default shape ---
        post_protected, post_rule = _read_protection(
            live_client, write_target_repo, target_branch
        )
        assert post_protected, "apply reported success but branch is unprotected"
        assert post_rule is not None
        assert post_rule["required_approving_review_count"] == 1
        assert post_rule["dismiss_stale_reviews"] is True
        assert post_rule["enforce_admins"] is False

        # --- rollback ---
        rb_result = AddBranchProtection.rollback(db, live_client, result.op_id)
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback failed: status={per_rb.status} "
            f"err={per_rb.error_message!r}"
        )

        # --- verify the prior state is restored ---
        final_protected, final_rule = _read_protection(
            live_client, write_target_repo, target_branch
        )
        assert final_protected == was_protected, (
            f"protected state mismatch after rollback: was {was_protected}, "
            f"now {final_protected}"
        )
        if was_protected and prior_rule_dict is not None:
            # Compare a few key fields rather than the whole dict — GitHub
            # may add fields the dataclass doesn't model.
            assert final_rule is not None
            for key in (
                "required_approving_review_count",
                "dismiss_stale_reviews",
                "enforce_admins",
                "required_linear_history",
            ):
                assert final_rule.get(key) == prior_rule_dict.get(key), (
                    f"{key} drifted after rollback: prior={prior_rule_dict.get(key)} "
                    f"final={final_rule.get(key)}"
                )

    finally:
        # If we wrote protection but the rollback didn't run (test crashed
        # mid-flight), try a best-effort restore so the next run starts clean.
        if state["wrote_protection"]:
            try:
                # If we can read the current state and it diverges from prior,
                # attempt a manual restore to the prior state.
                _, current_rule = _read_protection(
                    live_client, write_target_repo, target_branch
                )
                if current_rule != prior_rule_dict:
                    branch_obj = live_client.call(repo.get_branch, target_branch)
                    if prior_rule_dict is None:
                        if bool(getattr(branch_obj, "protected", False)):
                            live_client.call(branch_obj.remove_protection)
                    else:
                        from tacon.ops._branch_protection_rule import from_dict

                        prior_rule_obj = from_dict(prior_rule_dict)
                        live_client.call(
                            branch_obj.edit_protection,
                            **prior_rule_obj.to_edit_protection_kwargs(),
                        )
            except Exception:  # noqa: BLE001
                # Cleanup is best-effort. If this fails the test will already
                # have reported the underlying error; nothing more to do here.
                pass
