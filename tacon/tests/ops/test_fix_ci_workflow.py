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


def _blob(body: bytes) -> MagicMock:
    """Mock a git blob object returned by repo.get_git_blob."""
    b = MagicMock(name="GitBlob")
    b.content = base64.b64encode(body).decode("ascii")
    return b


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


def test_rollback_uses_cached_previous_blob_sha_fast_path(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Schema v4: apply records previous_blob_sha; rollback fetches the blob
    directly via repo.get_git_blob — no parent-commit walk needed."""
    _setup_apply(fake_repo)
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
    )
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Rollback flow on the fast path:
    #   1. get_contents(path) -> current file with sha == applied (matches, safe)
    #   2. get_git_blob(previous_blob_sha) -> blob with OLD_WORKFLOW content
    #   3. update_file(...) writes the old content back
    # No get_commit / parent-walk get_contents required.
    fake_repo.get_contents.side_effect = None
    fake_repo.get_contents.return_value = _content_file("new-blob", NEW_WORKFLOW)
    fake_repo.get_git_blob.return_value = _blob(OLD_WORKFLOW)
    fake_repo.update_file.return_value = {
        "commit": _commit("revert-commit", parent_sha="apply-commit"),
        "content": _content_file("revert-blob", OLD_WORKFLOW),
    }

    result = FixCIWorkflow.rollback(tmp_db, fake_gh, apply_result.op_id)

    assert all(r.status == "rolled_back" for r in result.per_repo)
    # Each repo's revert wrote the OLD_WORKFLOW bytes
    update_calls = [
        c for c in fake_repo.update_file.call_args_list if "Revert" in c.args[1]
    ]
    assert len(update_calls) == 3
    for call in update_calls:
        assert call.args[2] == OLD_WORKFLOW

    # Fast path: get_git_blob was used, parent walk wasn't.
    assert fake_repo.get_git_blob.called
    assert not fake_repo.get_commit.called

    # And the apply event stored previous_blob_sha for use here.
    events = get_events_by_op(tmp_db, apply_result.op_id)
    assert all(e["previous_blob_sha"] == "orig-blob" for e in events)
    assert all(e["status"] == "rolled_back" for e in events)


def test_rollback_falls_back_to_parent_walk_for_pre_v4_events(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Backwards compat: events with NULL previous_blob_sha (pre-v0.2.1
    apply runs against a v3 DB) still revert via the parent-commit walk."""
    _setup_apply(fake_repo)
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
    )
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Simulate pre-v4 events by clearing the cached sha. (Real pre-v4 events
    # would have NULL because the column didn't exist when they were
    # written; after the v4 migration the column is NULL for them.)
    tmp_db.execute(
        "UPDATE events SET previous_blob_sha = NULL WHERE op_id = ?",
        [apply_result.op_id],
    )

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
    # Slow path: get_commit + parent-walk get_contents both called.
    assert fake_repo.get_commit.called
    assert not fake_repo.get_git_blob.called

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


# ---------- via_pr ----------


def test_apply_via_pr_creates_branch_patches_and_opens_pr(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Happy path: branch created at default-branch SHA, transform applied
    on the branch (NOT default), PR opened."""
    fake_repo.get_contents.return_value = _content_file("orig-blob")
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.update_file.return_value = {
        "commit": _commit("c-pr"),
        "content": _content_file("blob-pr", NEW_WORKFLOW),
    }
    pr = MagicMock(name="PR")
    pr.number = 31
    fake_repo.create_pull.return_value = pr

    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
        via_pr=True,
    )
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "applied" for r in result.per_repo)
    fake_repo.create_git_ref.assert_called()
    # update_file should have been called with branch=
    upd_kwargs = fake_repo.update_file.call_args.kwargs
    assert upd_kwargs["branch"].startswith("tacon/fix-ci-workflow-")
    head_kwarg = fake_repo.create_pull.call_args.kwargs["head"]
    assert head_kwarg == upd_kwargs["branch"]
    events = get_events_by_op(tmp_db, result.op_id)
    assert all(e["pr_number"] == 31 for e in events)
    assert all(e["pr_branch"] == head_kwarg for e in events)


def test_apply_via_pr_skips_when_transform_no_op_on_branch(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Plan sees the old workflow; before apply runs, the file got patched
    on the branch already. _patch returns None; we skip without opening PR."""
    # Plan sees old.
    fake_repo.get_contents.return_value = _content_file("orig-blob")
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
        via_pr=True,
    )
    diff = op.plan(tmp_db, fake_gh)

    # Now mid-flight, the branch already contains the new content.
    fake_repo.get_contents.return_value = _content_file("new-blob", NEW_WORKFLOW)

    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "skipped" for r in result.per_repo)
    fake_repo.update_file.assert_not_called()
    fake_repo.create_pull.assert_not_called()


def test_apply_via_pr_branch_conflict_skipped(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file("orig-blob")
    head = MagicMock(name="Branch")
    head.commit.sha = "expected-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Reference already exists"}, {}
    )
    existing_ref = MagicMock()
    existing_ref.object.sha = "different-sha"
    fake_repo.get_git_ref.return_value = existing_ref

    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
        via_pr=True,
    )
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "skipped" for r in result.per_repo)
    assert all(r.error_class == "conflict" for r in result.per_repo)
    fake_repo.update_file.assert_not_called()


def test_args_includes_via_pr_for_fix() -> None:
    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("a", "b"),
        transform_id="x",
        via_pr=True,
    )
    assert op.args["via_pr"] is True


def test_rollback_via_pr_closes_pr_and_deletes_branch_for_fix(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file("orig-blob")
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.update_file.return_value = {
        "commit": _commit("c-r"),
        "content": _content_file("blob-r", NEW_WORKFLOW),
    }
    pr = MagicMock()
    pr.number = 41
    fake_repo.create_pull.return_value = pr

    op = FixCIWorkflow(
        name="ci",
        transform=make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4"),
        transform_id="bump-v3-v4",
        via_pr=True,
    )
    diff = op.plan(tmp_db, fake_gh)
    apply_res = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    pr_for_rollback = MagicMock()
    pr_for_rollback.number = 41
    pr_for_rollback.state = "open"
    pr_for_rollback.merged = False
    fake_repo.get_pull.return_value = pr_for_rollback
    branch_ref = MagicMock()
    fake_repo.get_git_ref.return_value = branch_ref

    rb_res = FixCIWorkflow.rollback(tmp_db, fake_gh, apply_res.op_id)
    assert all(r.status == "rolled_back" for r in rb_res.per_repo)
    assert pr_for_rollback.edit.call_count == 3
