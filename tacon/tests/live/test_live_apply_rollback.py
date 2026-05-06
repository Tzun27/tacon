"""Live apply+rollback round-trip on a single in-scope repo.

What this test does, in order:
  1. Verifies the target repo is in scope (refuses otherwise).
  2. Confirms the test path is currently absent (refuses if present —
     means a previous run didn't clean up, leave it alone).
  3. Plans + applies AddFile to write a clearly-tacon-marked file.
  4. Verifies the file landed.
  5. Rolls back. Verifies the file is gone.

The test path lives at the unique unambiguous location:
  .tacon-live-test/<random-hex>.txt

If anything goes wrong, the test attempts a best-effort cleanup so we
don't leave litter in the repo.
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
from tacon.ops.add_file import AddFile
from tests.live.conftest import assert_in_scope


def _test_path() -> str:
    return f".tacon-live-test/{secrets.token_hex(6)}.txt"


def _fetch_or_none(client: RateLimitedClient, repo_full_name: str, path: str) -> Any:
    repo = client.get_repo(repo_full_name)
    try:
        return client.call(repo.get_contents, path)
    except UnknownObjectException:
        return None


def test_add_file_apply_then_rollback(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """End-to-end: AddFile.plan -> apply -> verify -> rollback -> verify."""

    # --- preflight: scope + path-clean checks ---
    assert_in_scope(write_target_repo)

    test_path = _test_path()
    assert _fetch_or_none(live_client, write_target_repo, test_path) is None, (
        f"Refusing to start: {test_path} already exists in {write_target_repo}; "
        "manually remove it before re-running"
    )

    # --- seed an in-memory DB with just this one repo, so AddFile.plan can
    # discover it and apply can record events. We use a tmp DB so we don't
    # mutate the user's real ~/.tacon/tacon.db. ---
    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-live",
        classroom_id="cls-live",
        title="live e2e test",
        slug="live-e2e",
        starter_repo=None,
        created_at="2026-05-06T00:00:00Z",
    )
    # Use the GitHub username portion of the repo name as the student_id.
    # This is just bookkeeping for the events table — it doesn't drive any
    # API call.
    student_id = upsert_student(db, username="tacon-live-test")
    upsert_repo(
        db,
        id=write_target_repo,
        assignment_id="asn-live",
        student_id=student_id,
    )

    content = (
        "# tacon live e2e test\n\n"
        f"Created by tacon v{__version__} for an automated apply+rollback "
        "round-trip. If you're reading this, the rollback step did NOT run "
        "cleanly — please delete this file.\n"
        f"run-id: {uuid.uuid4()}\n"
    )
    op = AddFile(
        path=test_path,
        content=content,
        message="tacon live e2e: apply",
        assignment_id="asn-live",
    )

    # We use a closure to capture op_id so the cleanup block can refer to it
    # even if rollback is reached via an exception.
    state: dict[str, str] = {"op_id": ""}

    try:
        # --- plan ---
        diff = op.plan(db, live_client)
        assert len(diff.per_repo) == 1
        per = diff.per_repo[0]
        assert per.repo_id == write_target_repo
        assert not per.blocked, f"plan blocked: {per.blocked_reason}"

        # --- apply (auto-confirm: the scope guard already ran and the path
        # is provably absent, so there's no decision left to make) ---
        result = op.apply(db, live_client, diff, confirm=lambda _r: True)
        assert len(result.per_repo) == 1
        per_apply = result.per_repo[0]
        assert per_apply.status == "applied", (
            f"apply failed: status={per_apply.status} "
            f"err={per_apply.error_class!r} {per_apply.error_message!r}"
        )
        assert per_apply.commit_sha
        assert per_apply.applied_blob_sha
        state["op_id"] = result.op_id

        # --- verify the file actually landed ---
        landed = _fetch_or_none(live_client, write_target_repo, test_path)
        assert landed is not None, f"apply reported success but {test_path} is absent"
        assert landed.sha == per_apply.applied_blob_sha

        # --- rollback ---
        rb_result = AddFile.rollback(db, live_client, result.op_id)
        assert len(rb_result.per_repo) == 1
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback did not succeed: status={per_rb.status} "
            f"err={per_rb.error_message!r}"
        )

        # --- verify the file is gone ---
        post_rollback = _fetch_or_none(live_client, write_target_repo, test_path)
        assert post_rollback is None, (
            f"rollback reported success but {test_path} is still present"
        )

    finally:
        # Best-effort cleanup if anything above fell through. We never
        # operate on a repo that didn't pass assert_in_scope earlier.
        leftover = _fetch_or_none(live_client, write_target_repo, test_path)
        if leftover is not None:
            try:
                repo = live_client.get_repo(write_target_repo)
                live_client.call(
                    repo.delete_file,
                    test_path,
                    "tacon live e2e: emergency cleanup",
                    leftover.sha,
                )
            except Exception as cleanup_err:  # noqa: BLE001
                pytest.fail(
                    f"Test left {test_path} in {write_target_repo} and "
                    f"automatic cleanup failed: {cleanup_err}. "
                    "Please remove the file manually."
                )


def test_add_file_blocked_when_path_exists(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """Plan against a path we KNOW exists (README) and verify it's blocked.

    This proves the AddFile-precondition-check path runs against real
    GitHub data, not just our mock. Read-only — the apply is gated by
    the blocked flag and never fires.
    """
    assert_in_scope(write_target_repo)

    # Pick a path that's almost universally present in classroom repos.
    # If neither exists, the test is meaningless; skip rather than fail.
    repo = live_client.get_repo(write_target_repo)
    candidate_path = None
    for guess in ("README.md", "readme.md", "README"):
        try:
            live_client.call(repo.get_contents, guess)
            candidate_path = guess
            break
        except UnknownObjectException:
            continue
    if candidate_path is None:
        pytest.skip(
            f"no README-like file in {write_target_repo}; cannot verify "
            "the blocked-when-present plan path"
        )

    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-live",
        classroom_id="cls-live",
        title="live e2e test",
        slug="live-e2e",
        starter_repo=None,
        created_at="2026-05-06T00:00:00Z",
    )
    student_id = upsert_student(db, username="tacon-live-test")
    upsert_repo(
        db,
        id=write_target_repo,
        assignment_id="asn-live",
        student_id=student_id,
    )

    op = AddFile(
        path=candidate_path,
        content="this should never be written",
        assignment_id="asn-live",
    )
    diff = op.plan(db, live_client)
    assert len(diff.per_repo) == 1
    per = diff.per_repo[0]
    assert per.blocked, f"plan should have been blocked but wasn't: {per.summary}"
    assert "file exists" in per.blocked_reason


