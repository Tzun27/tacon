"""Live AddBranchProtection read-only survey.

AddBranchProtection is read-only in v0.2 — it inspects each repo's
branch protection state and records a 'reported' event for the audit
trail. There's no rollback (supports_rollback=False).

This test:
  1. Verifies the target repo is in scope.
  2. Plans against the test repo and checks that the per-repo entry
     has a meaningful summary (either "protected: NO", "protected: YES",
     or one of the restricted/missing variants).
  3. Applies and verifies the event lands with status='reported' and
     the summary recorded in error_message (per the op's audit shape).

No writes hit GitHub — this is purely a read of the test repo's current
state, so there's nothing to clean up.
"""

from __future__ import annotations

from pathlib import Path

from tacon.db import (
    open_db,
    upsert_assignment,
    upsert_repo,
    upsert_student,
)
from tacon.github_client import RateLimitedClient
from tacon.ops.add_branch_protection import AddBranchProtection
from tests.live.conftest import assert_in_scope


def test_add_branch_protection_survey(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """Plan + apply (read-only) on the test repo; verify the event is recorded."""

    assert_in_scope(write_target_repo)

    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-live-bp",
        classroom_id="cls-live",
        title="live add-branch-protection survey",
        slug="live-bp",
        starter_repo=None,
        created_at="2026-05-07T00:00:00Z",
    )
    student_id = upsert_student(db, username="tacon-live-bp")
    upsert_repo(
        db,
        id=write_target_repo,
        assignment_id="asn-live-bp",
        student_id=student_id,
    )

    op = AddBranchProtection(assignment_id="asn-live-bp")

    # --- plan ---
    diff = op.plan(db, live_client)
    assert len(diff.per_repo) == 1
    per = diff.per_repo[0]
    assert per.repo_id == write_target_repo
    # Whatever the repo's actual state, the summary should be one of the
    # known shapes — never empty, never crashing.
    assert per.summary, "expected a non-empty summary"
    expected_prefixes = (
        "protected: NO",
        "protected: YES",
        "protected: yes",  # restricted-details variant
        "branch '",  # branch-missing variant
        "unreachable",  # api-error variant
    )
    assert per.summary.startswith(expected_prefixes), (
        f"unexpected summary shape: {per.summary!r}"
    )

    # --- apply (read-only — records the survey to events) ---
    result = op.apply(db, live_client, diff, confirm=lambda _r: True)
    assert len(result.per_repo) == 1
    per_apply = result.per_repo[0]
    # If the branch was missing or unreachable, plan blocks → apply skips.
    assert per_apply.status in {"reported", "skipped"}, (
        f"unexpected apply status: {per_apply.status}"
    )

    # --- verify event landed ---
    rows = list(db["events"].rows_where("op_id = ?", (result.op_id,)))
    assert len(rows) == 1
    ev = rows[0]
    assert ev["op_class"] == "add_branch_protection"
    assert ev["repo_id"] == write_target_repo
    assert ev["status"] == per_apply.status
    # The summary lives in error_message for audit (this op uses it as a
    # general "what we observed" channel, not just for failures).
    if per_apply.status == "reported":
        assert ev["error_message"] == per.summary
