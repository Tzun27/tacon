"""Live AddCIWorkflow apply + rollback round-trips (direct-write + via-pr).

AddCIWorkflow inherits AddFile's apply path, but writes to
``.github/workflows/<name>.yml`` and validates the YAML. These tests
exercise both write modes against the real GitHub API on a tacon-marked
workflow filename so we never collide with a real workflow.

Same shape as test_live_apply_rollback.py and test_live_via_pr.py:
preflight -> plan -> apply -> verify -> rollback -> verify clean,
with try/finally cleanup.
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
from tacon.ops.add_ci_workflow import AddCIWorkflow
from tests.live.conftest import assert_in_scope


def _workflow_name() -> str:
    # 8-hex suffix, alphanumeric so it satisfies AddCIWorkflow's _NAME_RE
    return f"tacon-live-{secrets.token_hex(4)}"


def _workflow_content(run_id: str) -> str:
    return (
        f"# tacon live e2e workflow ({run_id})\n"
        "name: tacon-live-test\n"
        "on:\n"
        "  workflow_dispatch:\n"
        "jobs:\n"
        "  noop:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo 'tacon live test workflow — safe to delete'\n"
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


def test_add_ci_workflow_apply_then_rollback(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """Direct-write: AddCIWorkflow.plan -> apply -> verify -> rollback -> verify."""

    assert_in_scope(write_target_repo)

    name = _workflow_name()
    path = f".github/workflows/{name}.yml"
    assert _fetch_or_none(live_client, write_target_repo, path) is None, (
        f"Refusing to start: {path} already exists in {write_target_repo}"
    )

    db = _seed_db(tmp_path, write_target_repo, slug="add-ci-workflow")
    run_id = str(uuid.uuid4())
    op = AddCIWorkflow(
        name=name,
        content=_workflow_content(run_id),
        message="tacon live add-ci-workflow e2e: apply",
        assignment_id="asn-add-ci-workflow",
    )

    state: dict[str, str] = {"op_id": ""}

    try:
        diff = op.plan(db, live_client)
        assert len(diff.per_repo) == 1
        per = diff.per_repo[0]
        assert not per.blocked, f"plan blocked: {per.blocked_reason}"

        result = op.apply(db, live_client, diff, confirm=lambda _r: True)
        per_apply = result.per_repo[0]
        assert per_apply.status == "applied", (
            f"apply failed: status={per_apply.status} "
            f"err={per_apply.error_class!r} {per_apply.error_message!r}"
        )
        assert per_apply.commit_sha
        assert per_apply.applied_blob_sha
        state["op_id"] = result.op_id

        landed = _fetch_or_none(live_client, write_target_repo, path)
        assert landed is not None, f"workflow {path} did not land"
        assert landed.sha == per_apply.applied_blob_sha

        rb_result = AddCIWorkflow.rollback(db, live_client, result.op_id)
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback failed: status={per_rb.status} err={per_rb.error_message!r}"
        )

        post = _fetch_or_none(live_client, write_target_repo, path)
        assert post is None, f"rollback reported success but {path} is still present"

    finally:
        leftover = _fetch_or_none(live_client, write_target_repo, path)
        if leftover is not None:
            try:
                repo = live_client.get_repo(write_target_repo)
                live_client.call(
                    repo.delete_file,
                    path,
                    "tacon live add-ci-workflow e2e: emergency cleanup",
                    leftover.sha,
                )
            except Exception as cleanup_err:  # noqa: BLE001
                pytest.fail(
                    f"Test left {path} in {write_target_repo} and cleanup failed: "
                    f"{cleanup_err}. Remove the workflow manually."
                )


def test_add_ci_workflow_via_pr_apply_then_rollback(
    live_client: RateLimitedClient, write_target_repo: str, tmp_path: Path
) -> None:
    """Via-PR: AddCIWorkflow(via_pr=True).plan -> apply -> verify PR -> rollback -> verify clean."""

    assert_in_scope(write_target_repo)

    name = _workflow_name()
    path = f".github/workflows/{name}.yml"
    assert _fetch_or_none(live_client, write_target_repo, path) is None, (
        f"Refusing to start: {path} already exists in {write_target_repo}"
    )

    db = _seed_db(tmp_path, write_target_repo, slug="add-ci-workflow-pr")
    run_id = str(uuid.uuid4())
    op = AddCIWorkflow(
        name=name,
        content=_workflow_content(run_id),
        message="tacon live add-ci-workflow via-pr e2e: apply",
        assignment_id="asn-add-ci-workflow-pr",
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
        assert branch.startswith("tacon/add-ci-workflow-")
        state["pr_number"] = pr_number
        state["branch"] = branch

        on_branch = _fetch_or_none(live_client, write_target_repo, path, ref=branch)
        assert on_branch is not None, f"workflow {path} missing on branch {branch}"
        on_default = _fetch_or_none(live_client, write_target_repo, path)
        assert on_default is None, (
            "--via-pr leaked workflow onto default branch — bug"
        )

        repo = live_client.get_repo(write_target_repo)
        pr = live_client.call(repo.get_pull, pr_number)
        assert pr.state == "open"
        assert pr.head.ref == branch
        assert pr.base.ref == repo.default_branch

        rb_result = AddCIWorkflow.rollback(db, live_client, result.op_id)
        per_rb = rb_result.per_repo[0]
        assert per_rb.status == "rolled_back", (
            f"rollback failed: status={per_rb.status} err={per_rb.error_message!r}"
        )

        pr_after = live_client.call(repo.get_pull, pr_number)
        assert pr_after.state == "closed"
        assert not pr_after.merged
        assert _branch_missing(live_client, repo, branch), (
            f"branch {branch!r} should have been deleted but still exists"
        )

    finally:
        repo = live_client.get_repo(write_target_repo)
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
                    "tacon live add-ci-workflow via-pr e2e: emergency cleanup",
                    leftover.sha,
                )
            except Exception as cleanup_err:  # noqa: BLE001
                pytest.fail(
                    f"Test left workflow {path} on default of {write_target_repo} "
                    f"and cleanup failed: {cleanup_err}"
                )
