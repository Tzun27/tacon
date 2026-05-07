"""Live FixCIWorkflow apply + rollback round-trips (direct-write + via-pr).

Each test:
  1. Seeds a tacon-marked workflow with `actions/checkout@v3` via the API.
  2. Runs FixCIWorkflow with `make_bump_action_transform("v3" -> "v4")`.
  3. Verifies the bump landed (direct-write) or only landed on the
     tacon branch (via-pr).
  4. Rolls back. Verifies the prior content is restored (direct-write) or
     PR closed + branch deleted (via-pr).
  5. try/finally removes the seeded workflow + any leftover PR/branch.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from github import GithubException, UnknownObjectException

from tacon.db import (
    open_db,
    upsert_assignment,
    upsert_repo,
    upsert_student,
)
from tacon.github_client import RateLimitedClient
from tacon.ops.fix_ci_workflow import FixCIWorkflow, make_bump_action_transform
from tests.live.conftest import assert_in_scope


def _workflow_name() -> str:
    return f"tacon-live-fix-{secrets.token_hex(4)}"


def _seeded_workflow(run_id: str) -> str:
    """Workflow content with checkout@v3 to be bumped to v4."""
    return (
        f"# tacon live fix-ci-workflow e2e ({run_id})\n"
        "name: tacon-live-fix-test\n"
        "on:\n"
        "  workflow_dispatch:\n"
        "jobs:\n"
        "  noop:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v3\n"
        "      - run: echo 'tacon live fix test workflow — safe to delete'\n"
    )


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


def _seed_db(tmp_path: Path, repo_id: str, *, slug: str) -> Any:
    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    upsert_assignment(
        db,
        id=f"asn-{slug}",
        classroom_id="cls-live",
        title=f"live {slug} e2e",
        slug=slug,
        starter_repo=None,
        created_at="2026-05-07T00:00:00Z",
    )
    student_id = upsert_student(db, username=f"tacon-{slug}")
    upsert_repo(db, id=repo_id, assignment_id=f"asn-{slug}", student_id=student_id)
    return db


def _decode(content_file: Any) -> str:
    """Decode a PyGithub ContentFile to a UTF-8 string."""
    import base64

    return base64.b64decode(getattr(content_file, "content", "") or "").decode(
        "utf-8", errors="replace"
    )


def test_fix_ci_workflow_apply_then_rollback(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """Direct-write: seed v3 workflow -> bump to v4 -> verify -> rollback to v3."""

    assert_in_scope(write_target_repo)

    name = _workflow_name()
    path = f".github/workflows/{name}.yml"
    assert _fetch_or_none(live_client, write_target_repo, path) is None, (
        f"Refusing to start: {path} already exists in {write_target_repo}"
    )

    repo = live_client.get_repo(write_target_repo)
    run_id = str(uuid.uuid4())
    seeded_content = _seeded_workflow(run_id)
    seed_resp = live_client.call(
        repo.create_file,
        path,
        "tacon live fix-ci-workflow e2e: seed v3",
        seeded_content,
    )
    seeded_blob_sha = seed_resp["content"].sha

    db = _seed_db(tmp_path, write_target_repo, slug="fix-ci-workflow")
    op = FixCIWorkflow(
        name=name,
        transform=make_bump_action_transform(
            "actions/checkout@v3", "actions/checkout@v4"
        ),
        transform_id="bump-action:actions/checkout@v3->actions/checkout@v4",
        message="tacon live fix-ci-workflow e2e: bump checkout v3->v4",
        assignment_id="asn-fix-ci-workflow",
    )

    state: dict[str, str] = {"op_id": ""}

    try:
        diff = op.plan(db, live_client)
        per = diff.per_repo[0]
        assert not per.blocked, f"plan blocked: {per.blocked_reason}"

        result = op.apply(db, live_client, diff, confirm=lambda _r: True)
        per_apply = result.per_repo[0]
        assert per_apply.status == "applied", (
            f"apply failed: status={per_apply.status} "
            f"err={per_apply.error_class!r} {per_apply.error_message!r}"
        )
        state["op_id"] = result.op_id

        # --- verify: file now contains v4, not v3 ---
        landed = _fetch_or_none(live_client, write_target_repo, path)
        assert landed is not None
        landed_text = _decode(landed)
        assert "actions/checkout@v4" in landed_text
        assert "actions/checkout@v3" not in landed_text
        assert landed.sha != seeded_blob_sha
        assert landed.sha == per_apply.applied_blob_sha

        # --- rollback ---
        rb_result = FixCIWorkflow.rollback(db, live_client, result.op_id)
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback failed: status={per_rb.status} err={per_rb.error_message!r}"
        )

        # --- verify: file restored to v3 ---
        restored = _fetch_or_none(live_client, write_target_repo, path)
        assert restored is not None
        restored_text = _decode(restored)
        assert "actions/checkout@v3" in restored_text
        assert "actions/checkout@v4" not in restored_text
        # Git is content-addressed: restoring the original bytes yields the
        # same blob sha.
        assert restored.sha == seeded_blob_sha

    finally:
        # Clean up the seeded workflow regardless of how the test ended.
        leftover = _fetch_or_none(live_client, write_target_repo, path)
        if leftover is not None:
            try:
                live_client.call(
                    repo.delete_file,
                    path,
                    "tacon live fix-ci-workflow e2e: cleanup seeded workflow",
                    leftover.sha,
                )
            except Exception as cleanup_err:  # noqa: BLE001
                pytest.fail(
                    f"Test left {path} in {write_target_repo} and cleanup failed: "
                    f"{cleanup_err}. Remove the workflow manually."
                )


def test_fix_ci_workflow_via_pr_apply_then_rollback(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """Via-PR: seed v3 -> open PR with v4 bump on tacon branch -> rollback closes PR."""

    assert_in_scope(write_target_repo)

    name = _workflow_name()
    path = f".github/workflows/{name}.yml"
    assert _fetch_or_none(live_client, write_target_repo, path) is None, (
        f"Refusing to start: {path} already exists in {write_target_repo}"
    )

    repo = live_client.get_repo(write_target_repo)
    run_id = str(uuid.uuid4())
    seeded_content = _seeded_workflow(run_id)
    seed_resp = live_client.call(
        repo.create_file,
        path,
        "tacon live fix-ci-workflow via-pr e2e: seed v3",
        seeded_content,
    )
    seeded_blob_sha = seed_resp["content"].sha

    db = _seed_db(tmp_path, write_target_repo, slug="fix-ci-workflow-pr")
    op = FixCIWorkflow(
        name=name,
        transform=make_bump_action_transform(
            "actions/checkout@v3", "actions/checkout@v4"
        ),
        transform_id="bump-action:actions/checkout@v3->actions/checkout@v4",
        message="tacon live fix-ci-workflow via-pr e2e: bump checkout v3->v4",
        assignment_id="asn-fix-ci-workflow-pr",
        via_pr=True,
    )

    state: dict[str, Any] = {"op_id": "", "branch": "", "pr_number": None}

    try:
        diff = op.plan(db, live_client)
        per = diff.per_repo[0]
        assert not per.blocked, f"plan blocked: {per.blocked_reason}"

        result = op.apply(db, live_client, diff, confirm=lambda _r: True)
        per_apply = result.per_repo[0]
        assert per_apply.status == "applied", (
            f"apply failed: status={per_apply.status} "
            f"err={per_apply.error_class!r} {per_apply.error_message!r}"
        )
        state["op_id"] = result.op_id

        rows = list(db["events"].rows_where("op_id = ?", (result.op_id,)))
        assert len(rows) == 1
        ev = rows[0]
        pr_number = ev["pr_number"]
        branch = ev["pr_branch"]
        assert pr_number is not None
        assert branch
        assert branch.startswith("tacon/fix-ci-workflow-")
        state["pr_number"] = pr_number
        state["branch"] = branch

        # --- verify: bump landed on branch only; default still has v3 ---
        on_branch = _fetch_or_none(
            live_client, write_target_repo, path, ref=branch
        )
        assert on_branch is not None
        branch_text = _decode(on_branch)
        assert "actions/checkout@v4" in branch_text
        assert "actions/checkout@v3" not in branch_text

        on_default = _fetch_or_none(live_client, write_target_repo, path)
        assert on_default is not None
        assert on_default.sha == seeded_blob_sha
        default_text = _decode(on_default)
        assert "actions/checkout@v3" in default_text
        assert "actions/checkout@v4" not in default_text

        # --- verify PR open ---
        pr = live_client.call(repo.get_pull, pr_number)
        assert pr.state == "open"
        assert pr.head.ref == branch
        assert pr.base.ref == repo.default_branch

        # --- rollback ---
        rb_result = FixCIWorkflow.rollback(db, live_client, result.op_id)
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback failed: status={per_rb.status} err={per_rb.error_message!r}"
        )

        # --- verify PR closed + branch gone + default still untouched ---
        pr_after = live_client.call(repo.get_pull, pr_number)
        assert pr_after.state == "closed"
        assert not pr_after.merged
        assert _branch_missing(live_client, repo, branch)
        on_default_after = _fetch_or_none(live_client, write_target_repo, path)
        assert on_default_after is not None
        assert on_default_after.sha == seeded_blob_sha

    finally:
        if state["pr_number"]:
            try:
                pr = live_client.call(repo.get_pull, state["pr_number"])
                if pr.state == "open":
                    live_client.call(pr.edit, state="closed")
            except (GithubException, UnknownObjectException):
                pass
        if state["branch"]:
            try:
                ref = live_client.call(
                    repo.get_git_ref, f"heads/{state['branch']}"
                )
                live_client.call(ref.delete)
            except (GithubException, UnknownObjectException):
                pass
        leftover = _fetch_or_none(live_client, write_target_repo, path)
        if leftover is not None:
            try:
                live_client.call(
                    repo.delete_file,
                    path,
                    "tacon live fix-ci-workflow via-pr e2e: cleanup seeded workflow",
                    leftover.sha,
                )
            except Exception as cleanup_err:  # noqa: BLE001
                pytest.fail(
                    f"Test left seeded {path} on default of {write_target_repo} "
                    f"and cleanup failed: {cleanup_err}"
                )
