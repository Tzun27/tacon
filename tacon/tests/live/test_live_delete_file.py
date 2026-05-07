"""Live DeleteFile direct-write apply + rollback round-trip.

This test:
  1. Verifies the target repo is in scope (refuses otherwise).
  2. Seeds a unique tacon-marked file on the default branch via the API
     (simulating a file the TA wants to remove).
  3. Plans + applies DeleteFile to remove it.
  4. Verifies the file is gone.
  5. Rolls back. Verifies the file is restored with the same blob sha.
  6. try/finally cleanup: deletes any seeded file we left behind.

Companion to test_live_apply_rollback.py (which exercises AddFile);
this exercises the inverse op.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from github import UnknownObjectException

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
    return f".tacon-live-delete/{secrets.token_hex(6)}.txt"


def _fetch_or_none(client: RateLimitedClient, repo_full_name: str, path: str) -> Any:
    repo = client.get_repo(repo_full_name)
    try:
        return client.call(repo.get_contents, path)
    except UnknownObjectException:
        return None


def test_delete_file_apply_then_rollback(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """End-to-end: seed file -> DeleteFile.plan -> apply -> verify gone -> rollback -> verify restored."""

    # --- preflight: scope + path-clean checks ---
    assert_in_scope(write_target_repo)

    test_path = _test_path()
    assert _fetch_or_none(live_client, write_target_repo, test_path) is None, (
        f"Refusing to start: {test_path} already exists in {write_target_repo}; "
        "manually remove it before re-running"
    )

    # --- seed the file we'll delete. Same content shape as test_live_apply_rollback. ---
    repo = live_client.get_repo(write_target_repo)
    seeded_content = (
        "# tacon live delete-file e2e test\n\n"
        f"Seeded by tacon v{__version__} so DeleteFile can remove it.\n"
        f"run-id: {uuid.uuid4()}\n"
    )
    seed_resp = live_client.call(
        repo.create_file,
        test_path,
        "tacon live delete-file e2e: seed",
        seeded_content,
    )
    seeded_blob_sha = seed_resp["content"].sha

    # --- seed an in-memory DB ---
    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-live-del",
        classroom_id="cls-live",
        title="live delete-file e2e",
        slug="live-delete",
        starter_repo=None,
        created_at="2026-05-07T00:00:00Z",
    )
    student_id = upsert_student(db, username="tacon-live-delete-test")
    upsert_repo(
        db,
        id=write_target_repo,
        assignment_id="asn-live-del",
        student_id=student_id,
    )

    op = DeleteFile(
        path=test_path,
        message="tacon live delete-file e2e: apply",
        assignment_id="asn-live-del",
    )

    state: dict[str, str] = {"op_id": ""}

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
        assert per_apply.commit_sha
        assert per_apply.applied_blob_sha == seeded_blob_sha, (
            "applied_blob_sha should equal the seeded blob sha (the blob we deleted)"
        )
        state["op_id"] = result.op_id

        # --- verify the file is gone ---
        gone = _fetch_or_none(live_client, write_target_repo, test_path)
        assert gone is None, (
            f"apply reported success but {test_path} is still present"
        )

        # --- rollback ---
        rb_result = DeleteFile.rollback(db, live_client, result.op_id)
        assert len(rb_result.per_repo) == 1
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback did not succeed: status={per_rb.status} "
            f"err={per_rb.error_message!r}"
        )

        # --- verify the file is restored with the same content ---
        restored = _fetch_or_none(live_client, write_target_repo, test_path)
        assert restored is not None, (
            f"rollback reported success but {test_path} is missing"
        )
        # Note: blob sha after re-create equals the original sha (git is content-addressed).
        assert restored.sha == seeded_blob_sha, (
            f"restored blob sha {restored.sha} != original {seeded_blob_sha}"
        )

    finally:
        # Best-effort cleanup. We always end this test with the path absent
        # (both apply-then-crash and rollback-then-crash leave the path either
        # absent or restored). Whichever it is, normalize to "absent" so the
        # next run can re-seed cleanly.
        leftover = _fetch_or_none(live_client, write_target_repo, test_path)
        if leftover is not None:
            try:
                live_client.call(
                    repo.delete_file,
                    test_path,
                    "tacon live delete-file e2e: emergency cleanup",
                    leftover.sha,
                )
            except Exception as cleanup_err:  # noqa: BLE001
                pytest.fail(
                    f"Test left {test_path} in {write_target_repo} and "
                    f"automatic cleanup failed: {cleanup_err}. "
                    "Please remove the file manually."
                )


def test_delete_file_blocked_when_path_absent(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """Plan against a path that does NOT exist and verify it's blocked.

    Mirror of the AddFile blocked-when-present test: proves the
    DeleteFile precondition check runs against real GitHub data.
    """
    assert_in_scope(write_target_repo)

    # Generate a path that's overwhelmingly unlikely to exist.
    absent_path = f".tacon-live-delete-absent/{secrets.token_hex(8)}.txt"
    assert _fetch_or_none(live_client, write_target_repo, absent_path) is None

    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-live-del",
        classroom_id="cls-live",
        title="live delete-file e2e",
        slug="live-delete",
        starter_repo=None,
        created_at="2026-05-07T00:00:00Z",
    )
    student_id = upsert_student(db, username="tacon-live-delete-test")
    upsert_repo(
        db,
        id=write_target_repo,
        assignment_id="asn-live-del",
        student_id=student_id,
    )

    op = DeleteFile(path=absent_path, assignment_id="asn-live-del")
    diff = op.plan(db, live_client)
    assert len(diff.per_repo) == 1
    per = diff.per_repo[0]
    assert per.blocked, f"plan should have been blocked but wasn't: {per.summary}"
    assert "absent" in per.blocked_reason or "nothing to delete" in per.blocked_reason
