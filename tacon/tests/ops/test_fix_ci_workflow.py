"""Tests for FixCIWorkflow: transform plumbing, plan/apply/rollback."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from github import GithubException, UnknownObjectException
from sqlite_utils import Database

from tacon.db import get_events_by_op
from tacon.ops import get_op_class
from tacon.ops.add_ci_workflow import WorkflowValidationError
from tacon.ops.fix_ci_workflow import FixCIWorkflow, make_bump_action_transform

OLD_WORKFLOW = b"""\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: pytest
"""

NEW_WORKFLOW = OLD_WORKFLOW.replace(b"actions/checkout@v3", b"actions/checkout@v4")


def _content_file(sha: str = "blob-1", body: bytes = OLD_WORKFLOW) -> MagicMock:
    cf = MagicMock(name="ContentFile")
    cf.sha = sha
    cf.content = base64.b64encode(body).decode("ascii")
    return cf


def _commit(sha: str = "commit-1", parent_sha: str | None = "parent-1") -> MagicMock:
    c = MagicMock(name="Commit")
    c.sha = sha
    if parent_sha is None:
        c.parents = []
    else:
        parent = MagicMock(name="ParentCommit")
        parent.sha = parent_sha
        c.parents = [parent]
    return c


def _missing() -> UnknownObjectException:
    return UnknownObjectException(404, {"message": "Not Found"}, {})


# ---------- registry + transform builder ----------


def test_fix_ci_workflow_is_registered() -> None:
    assert get_op_class("fix-ci-workflow") is FixCIWorkflow


def test_bump_action_transform_replaces() -> None:
    t = make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4")
    assert t(OLD_WORKFLOW) == NEW_WORKFLOW


def test_bump_action_transform_returns_none_when_not_present() -> None:
    t = make_bump_action_transform("actions/checkout@v9", "actions/checkout@v10")
    assert t(OLD_WORKFLOW) is None


def test_bump_action_transform_rejects_identical_refs() -> None:
    with pytest.raises(ValueError, match="identical"):
        make_bump_action_transform("a", "a")


def test_bump_action_transform_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        make_bump_action_transform("", "x")


def test_init_rejects_unsafe_workflow_name() -> None:
    with pytest.raises(WorkflowValidationError):
        FixCIWorkflow(
            name="../../etc/passwd",
            transform=lambda b: b,
            transform_id="noop",
        )


# ---------- plan ----------


def test_plan_unblocked_when_transform_changes_content(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file()
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-checkout-v3-to-v4",
    )
    diff = op.plan(tmp_db, fake_gh)

    assert len(diff.per_repo) == 3
    assert not any(r.blocked for r in diff.per_repo)
    assert all("bump-checkout-v3-to-v4" in r.summary for r in diff.per_repo)
    # unified diff should mention the path and the change
    assert all("actions/checkout@v3" in r.unified_diff for r in diff.per_repo)
    assert all("actions/checkout@v4" in r.unified_diff for r in diff.per_repo)


def test_plan_blocked_when_workflow_absent(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing()
    op = FixCIWorkflow(
        name="ci",
        transform=lambda b: b + b"# patched\n",
        transform_id="append-comment",
    )
    diff = op.plan(tmp_db, fake_gh)
    assert all(r.blocked for r in diff.per_repo)
    assert all("workflow not present" in r.blocked_reason for r in diff.per_repo)


def test_plan_blocked_when_transform_is_noop(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file()
    op = FixCIWorkflow(
        name="ci",
        transform=lambda b: b,  # returns unchanged
        transform_id="identity",
    )
    diff = op.plan(tmp_db, fake_gh)
    assert all(r.blocked for r in diff.per_repo)
    assert all("identity" in r.blocked_reason or "no-op" in r.blocked_reason for r in diff.per_repo)


def test_plan_blocked_when_transform_returns_none(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file()
    op = FixCIWorkflow(
        name="ci",
        transform=lambda _b: None,
        transform_id="never-applies",
    )
    diff = op.plan(tmp_db, fake_gh)
    assert all(r.blocked for r in diff.per_repo)


def test_plan_handles_unreachable_repo(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = GithubException(500, {"message": "boom"}, {})
    op = FixCIWorkflow(
        name="ci",
        transform=lambda b: b + b"\n",
        transform_id="append-newline",
    )
    diff = op.plan(tmp_db, fake_gh)
    assert all(r.blocked for r in diff.per_repo)
    assert all("plan failed" in r.blocked_reason for r in diff.per_repo)


# ---------- apply ----------


def test_apply_patches_and_records_blob_sha(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file("orig-blob")
    fake_repo.update_file.return_value = {
        "commit": _commit("apply-commit"),
        "content": _content_file("new-blob", NEW_WORKFLOW),
    }
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
    )
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "applied" for r in result.per_repo)
    assert all(r.applied_blob_sha == "new-blob" for r in result.per_repo)

    events = get_events_by_op(tmp_db, result.op_id)
    assert all(e["op_class"] == "fix_ci_workflow" for e in events)
    assert all(e["commit_sha"] == "apply-commit" for e in events)


def test_apply_skips_when_state_changed_since_plan(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """If a student already patched the file between plan and apply, skip."""
    # Plan sees the old workflow.
    fake_repo.get_contents.return_value = _content_file("orig-blob")
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
    )
    diff = op.plan(tmp_db, fake_gh)

    # Apply re-fetches and finds the file already at v4.
    fake_repo.get_contents.return_value = _content_file("new-blob", NEW_WORKFLOW)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "skipped" for r in result.per_repo)
    fake_repo.update_file.assert_not_called()


def test_apply_marks_failed_on_github_exception(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file()
    fake_repo.update_file.side_effect = GithubException(409, {"message": "conflict"}, {})
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
    )
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)
    assert all(r.status == "failed" for r in result.per_repo)


# ---------- rollback ----------


def _setup_apply(
    fake_repo: MagicMock,
) -> None:
    fake_repo.get_contents.return_value = _content_file("orig-blob")
    fake_repo.update_file.return_value = {
        "commit": _commit("apply-commit", parent_sha="parent-1"),
        "content": _content_file("new-blob", NEW_WORKFLOW),
    }


def test_rollback_reverts_via_parent_commit(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    _setup_apply(fake_repo)
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
    )
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Configure rollback flow:
    #   1. get_contents(path) -> current file with sha == applied (matches, safe)
    #   2. get_commit(commit_sha) -> commit with parent
    #   3. get_contents(path, ref=parent_sha) -> old file
    #   4. update_file(...) writes the old content back
    def get_contents_side_effect(*args, **kwargs):
        if "ref" in kwargs and kwargs["ref"] == "parent-1":
            return _content_file("orig-blob", OLD_WORKFLOW)
        return _content_file("new-blob", NEW_WORKFLOW)

    fake_repo.get_contents.side_effect = get_contents_side_effect
    fake_repo.get_contents.return_value = None
    fake_repo.get_commit.return_value = _commit("apply-commit", parent_sha="parent-1")
    fake_repo.update_file.return_value = {
        "commit": _commit("revert-commit", parent_sha="apply-commit"),
        "content": _content_file("revert-blob", OLD_WORKFLOW),
    }

    result = FixCIWorkflow.rollback(tmp_db, fake_gh, apply_result.op_id)

    assert all(r.status == "rolled_back" for r in result.per_repo)
    # The bytes written by the revert update_file must equal OLD_WORKFLOW
    update_calls = [
        c for c in fake_repo.update_file.call_args_list if "Revert" in c.args[1]
    ]
    assert len(update_calls) == 3
    for call in update_calls:
        assert call.args[2] == OLD_WORKFLOW

    events = get_events_by_op(tmp_db, apply_result.op_id)
    assert all(e["status"] == "rolled_back" for e in events)


def test_rollback_skipped_dirty_when_blob_changed(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    _setup_apply(fake_repo)
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
    )
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Student has since edited; current sha != applied sha.
    fake_repo.get_contents.side_effect = None
    fake_repo.get_contents.return_value = _content_file("student-edited-blob")

    result = FixCIWorkflow.rollback(tmp_db, fake_gh, apply_result.op_id)
    assert all(r.status == "skipped_dirty" for r in result.per_repo)
    # Should not have called update_file at all in rollback (we reset above)
    update_calls = [
        c for c in fake_repo.update_file.call_args_list if "Revert" in c.args[1]
    ]
    assert update_calls == []


def test_rollback_skipped_dirty_when_file_disappeared(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    _setup_apply(fake_repo)
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
    )
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # File deleted between apply and rollback.
    fake_repo.get_contents.side_effect = _missing()

    result = FixCIWorkflow.rollback(tmp_db, fake_gh, apply_result.op_id)
    assert all(r.status == "skipped_dirty" for r in result.per_repo)


def test_rollback_returns_empty_for_unknown_op_id(tmp_db: Database, fake_gh: MagicMock) -> None:
    result = FixCIWorkflow.rollback(tmp_db, fake_gh, "op-nonexistent")
    assert result.per_repo == []
