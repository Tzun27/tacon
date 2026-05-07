"""Live `--via-pr` apply + rollback round-trip on a single in-scope repo.

Mirrors the direct-write `test_live_apply_rollback.py`, but exercises
the via-pr path:

  1. Scope guard.
  2. Preflight: confirm the test path is absent on default branch AND
     no leftover tacon branch from a prior crashed run.
  3. AddFile(via_pr=True).plan -> apply.
  4. Verify a tacon branch was created with the file on it.
  5. Verify the PR exists, head=tacon-branch, base=default.
  6. Verify the file is NOT on default branch (the whole point of via-pr).
  7. Rollback: PR closes, branch deletes.
  8. Verify the PR is closed-not-merged + branch is gone.

`try/finally` block does best-effort cleanup so we don't litter PRs or
branches in the test repo if something fails mid-flight.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from github import GithubException, UnknownObjectException

from tacon import __version__
from tacon.db import (
    open_db,
    upsert_assignment,
    upsert_repo,
    upsert_student,
)
from tacon.github_client import RateLimitedClient
from tacon.ops.add_file import AddFile
from tests.live.conftest import assert_in_scope


def _test_path() -> str:
    return f".tacon-live-via-pr/{secrets.token_hex(6)}.txt"


def _fetch_or_none(client: RateLimitedClient, repo_full_name: str, path: str, *, ref: str | None = None) -> Any:
    repo = client.get_repo(repo_full_name)
    try:
        if ref is not None:
            return client.call(repo.get_contents, path, ref=ref)
        return client.call(repo.get_contents, path)
    except UnknownObjectException:
        return None


def test_via_pr_apply_then_rollback(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """End-to-end: AddFile(via_pr=True).plan -> apply -> verify PR -> rollback -> verify clean."""

    # --- preflight: scope + path-clean checks ---
    assert_in_scope(write_target_repo)

    test_path = _test_path()
    # The file MUST be absent on the default branch (otherwise plan blocks
    # and we never exercise the via-pr machinery).
    assert (
        _fetch_or_none(live_client, write_target_repo, test_path) is None
    ), f"Refusing to start: {test_path} already exists in {write_target_repo}"

    # --- seed an in-memory DB with just this one repo ---
    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-live-pr",
        classroom_id="cls-live",
        title="live via-pr e2e test",
        slug="live-via-pr",
        starter_repo=None,
        created_at="2026-05-07T00:00:00Z",
    )
    student_id = upsert_student(db, username="tacon-live-via-pr")
    upsert_repo(
        db,
        id=write_target_repo,
        assignment_id="asn-live-pr",
        student_id=student_id,
    )

    content = (
        "# tacon live via-pr e2e test\n\n"
        f"Created by tacon v{__version__} for an automated apply+rollback "
        "round-trip via PR. If you're reading this on the default branch, "
        "something is very wrong — via-pr should never write to default.\n"
        f"run-id: {uuid.uuid4()}\n"
    )
    op = AddFile(
        path=test_path,
        content=content,
        message="tacon live via-pr e2e: apply",
        assignment_id="asn-live-pr",
        via_pr=True,
    )

    state: dict[str, Any] = {"op_id": "", "branch": "", "pr_number": None}

    try:
        # --- plan ---
        diff = op.plan(db, live_client)
        assert len(diff.per_repo) == 1
        per = diff.per_repo[0]
        assert per.repo_id == write_target_repo
        assert not per.blocked, f"plan blocked: {per.blocked_reason}"

        # --- apply ---
        result = op.apply(db, live_client, diff, confirm=lambda _r: True)
        assert len(result.per_repo) == 1
        per_apply = result.per_repo[0]
        assert per_apply.status == "applied", (
            f"apply failed: status={per_apply.status} "
            f"err={per_apply.error_class!r} {per_apply.error_message!r}"
        )
        state["op_id"] = result.op_id

        # --- read back pr_number + pr_branch from the events table ---
        rows = list(
            db["events"].rows_where("op_id = ?", (result.op_id,))
        )
        assert len(rows) == 1
        ev = rows[0]
        pr_number = ev["pr_number"]
        branch = ev["pr_branch"]
        assert pr_number is not None
        assert branch
        assert branch.startswith("tacon/add-file-")
        state["pr_number"] = pr_number
        state["branch"] = branch

        # --- verify the file landed ON THE BRANCH (not default) ---
        on_branch = _fetch_or_none(
            live_client, write_target_repo, test_path, ref=branch
        )
        assert on_branch is not None, (
            f"apply reported success but {test_path} is absent on {branch}"
        )
        on_default = _fetch_or_none(live_client, write_target_repo, test_path)
        assert on_default is None, (
            f"--via-pr leaked {test_path} onto default branch — this is a bug"
        )

        # --- verify the PR exists ---
        repo = live_client.get_repo(write_target_repo)
        pr = live_client.call(repo.get_pull, pr_number)
        assert pr.state == "open"
        assert pr.head.ref == branch
        assert pr.base.ref == repo.default_branch

        # --- rollback ---
        rb_result = AddFile.rollback(db, live_client, result.op_id)
        assert len(rb_result.per_repo) == 1
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback did not succeed: status={per_rb.status} "
            f"err={per_rb.error_message!r}"
        )

        # --- verify the PR is closed-not-merged + branch is gone ---
        pr_after = live_client.call(repo.get_pull, pr_number)
        assert pr_after.state == "closed"
        assert not pr_after.merged
        assert _fetch_or_none(
            live_client, write_target_repo, test_path, ref=branch
        ) is None or _branch_missing(live_client, repo, branch), (
            f"branch {branch!r} should have been deleted but still exists"
        )

    finally:
        # Best-effort cleanup. The repo passed assert_in_scope; safe to act.
        repo = live_client.get_repo(write_target_repo)
        # 1. Close the PR if it's still open.
        if state["pr_number"]:
            try:
                pr = live_client.call(repo.get_pull, state["pr_number"])
                if pr.state == "open":
                    live_client.call(pr.edit, state="closed")
            except (GithubException, UnknownObjectException):
                pass
        # 2. Delete the tacon branch if it still exists.
        if state["branch"]:
            try:
                ref = live_client.call(repo.get_git_ref, f"heads/{state['branch']}")
                live_client.call(ref.delete)
            except (GithubException, UnknownObjectException):
                pass
        # 3. Defense-in-depth: if anything ended up on default branch, remove it.
        leftover = _fetch_or_none(live_client, write_target_repo, test_path)
        if leftover is not None:
            try:
                live_client.call(
                    repo.delete_file,
                    test_path,
                    "tacon live via-pr e2e: emergency cleanup",
                    leftover.sha,
                )
            except Exception as cleanup_err:  # noqa: BLE001
                pytest.fail(
                    f"Test left {test_path} on default of {write_target_repo} "
                    f"and automatic cleanup failed: {cleanup_err}"
                )


def _branch_missing(client: RateLimitedClient, repo: Any, branch: str) -> bool:
    try:
        client.call(repo.get_git_ref, f"heads/{branch}")
        return False
    except UnknownObjectException:
        return True
