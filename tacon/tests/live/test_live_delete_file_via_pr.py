"""Live DeleteFile `--via-pr` apply + rollback round-trip.

Mirrors test_live_delete_file.py (direct-write) but exercises the
via-pr path:

  1. Scope guard.
  2. Preflight: confirm test path is absent + no leftover tacon branch.
  3. Seed the file on the default branch via the API.
  4. DeleteFile(via_pr=True).plan -> apply.
  5. Verify a tacon branch was created and the file is GONE on the branch
     but STILL present on default branch (the whole point of via-pr).
  6. Verify the PR is open with the right head/base refs.
  7. Rollback closes the PR + deletes the branch — file remains on default.
  8. Cleanup deletes the seeded file (now reverted to default).

`try/finally` block does best-effort cleanup so we don't litter PRs,
branches, or seeded files in the test repo if something fails mid-flight.
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
from tacon.ops.delete_file import DeleteFile
from tests.live.conftest import assert_in_scope


def _test_path() -> str:
    return f".tacon-live-delete-via-pr/{secrets.token_hex(6)}.txt"


def _fetch_or_none(
    client: RateLimitedClient,
    repo_full_name: str,
    path: str,
    *,
    ref: str | None = None,
) -> Any:
    repo = client.get_repo(repo_full_name)
    try:
        if ref is not None:
            return client.call(repo.get_contents, path, ref=ref)
        return client.call(repo.get_contents, path)
    except UnknownObjectException:
        return None


def _branch_missing(client: RateLimitedClient, repo: Any, branch: str) -> bool:
    try:
        client.call(repo.get_git_ref, f"heads/{branch}")
        return False
    except UnknownObjectException:
        return True


def test_delete_file_via_pr_apply_then_rollback(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """End-to-end: seed file -> DeleteFile(via_pr=True).plan -> apply -> verify PR -> rollback -> verify clean."""

    # --- preflight: scope + path-clean checks ---
    assert_in_scope(write_target_repo)

    test_path = _test_path()
    assert (
        _fetch_or_none(live_client, write_target_repo, test_path) is None
    ), f"Refusing to start: {test_path} already exists in {write_target_repo}"

    # --- seed the file we'll propose to delete via PR ---
    repo = live_client.get_repo(write_target_repo)
    seeded_content = (
        "# tacon live delete-file via-pr e2e test\n\n"
        f"Seeded by tacon v{__version__} so DeleteFile via-pr can propose its removal.\n"
        f"run-id: {uuid.uuid4()}\n"
    )
    seed_resp = live_client.call(
        repo.create_file,
        test_path,
        "tacon live delete-file via-pr e2e: seed",
        seeded_content,
    )
    seeded_blob_sha = seed_resp["content"].sha

    # --- seed an in-memory DB ---
    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-live-del-pr",
        classroom_id="cls-live",
        title="live delete-file via-pr e2e",
        slug="live-delete-via-pr",
        starter_repo=None,
        created_at="2026-05-07T00:00:00Z",
    )
    student_id = upsert_student(db, username="tacon-live-delete-via-pr")
    upsert_repo(
        db,
        id=write_target_repo,
        assignment_id="asn-live-del-pr",
        student_id=student_id,
    )

    op = DeleteFile(
        path=test_path,
        message="tacon live delete-file via-pr e2e: apply",
        assignment_id="asn-live-del-pr",
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
        assert per_apply.applied_blob_sha == seeded_blob_sha
        state["op_id"] = result.op_id

        # --- read back pr_number + pr_branch from the events table ---
        rows = list(db["events"].rows_where("op_id = ?", (result.op_id,)))
        assert len(rows) == 1
        ev = rows[0]
        pr_number = ev["pr_number"]
        branch = ev["pr_branch"]
        assert pr_number is not None
        assert branch
        assert branch.startswith("tacon/delete-file-")
        state["pr_number"] = pr_number
        state["branch"] = branch

        # --- verify: file ABSENT on tacon branch, STILL PRESENT on default ---
        on_branch = _fetch_or_none(
            live_client, write_target_repo, test_path, ref=branch
        )
        assert on_branch is None, (
            f"--via-pr did not delete {test_path} on branch {branch}"
        )
        on_default = _fetch_or_none(live_client, write_target_repo, test_path)
        assert on_default is not None, (
            f"--via-pr accidentally deleted {test_path} on default branch — bug"
        )
        assert on_default.sha == seeded_blob_sha

        # --- verify the PR exists ---
        pr = live_client.call(repo.get_pull, pr_number)
        assert pr.state == "open"
        assert pr.head.ref == branch
        assert pr.base.ref == repo.default_branch

        # --- rollback ---
        rb_result = DeleteFile.rollback(db, live_client, result.op_id)
        assert len(rb_result.per_repo) == 1
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback did not succeed: status={per_rb.status} "
            f"err={per_rb.error_message!r}"
        )

        # --- verify PR closed-not-merged + branch gone + file STILL on default ---
        pr_after = live_client.call(repo.get_pull, pr_number)
        assert pr_after.state == "closed"
        assert not pr_after.merged
        assert _branch_missing(live_client, repo, branch), (
            f"branch {branch!r} should have been deleted but still exists"
        )
        on_default_after = _fetch_or_none(live_client, write_target_repo, test_path)
        assert on_default_after is not None, (
            "via-pr rollback should NOT touch the default branch — "
            f"but {test_path} is now missing"
        )
        assert on_default_after.sha == seeded_blob_sha

    finally:
        # Best-effort cleanup. Repo passed assert_in_scope; safe to act.
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
        # 3. Remove the seeded file from default branch (it should still be there
        #    after a successful rollback — via-pr never touches default).
        leftover = _fetch_or_none(live_client, write_target_repo, test_path)
        if leftover is not None:
            try:
                live_client.call(
                    repo.delete_file,
                    test_path,
                    "tacon live delete-file via-pr e2e: cleanup seeded file",
                    leftover.sha,
                )
            except Exception as cleanup_err:  # noqa: BLE001
                pytest.fail(
                    f"Test left seeded {test_path} on default of {write_target_repo} "
                    f"and automatic cleanup failed: {cleanup_err}"
                )
