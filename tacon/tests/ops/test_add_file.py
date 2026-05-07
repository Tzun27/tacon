"""Tests for AddFile: plan, apply, rollback (with mocked PyGithub)."""

from __future__ import annotations

from unittest.mock import MagicMock

from github import UnknownObjectException
from sqlite_utils import Database

from tacon.db import get_events_by_op
from tacon.ops import get_op_class
from tacon.ops.add_file import AddFile


def _content_file(sha: str = "blob-sha-1") -> MagicMock:
    cf = MagicMock(name="ContentFile")
    cf.sha = sha
    return cf


def _commit(sha: str = "commit-sha-1") -> MagicMock:
    c = MagicMock(name="Commit")
    c.sha = sha
    return c


def _missing_file_exc() -> UnknownObjectException:
    return UnknownObjectException(404, {"message": "Not Found"}, {})


# ---------- registry ----------


def test_add_file_is_registered() -> None:
    assert get_op_class("add-file") is AddFile


# ---------- plan ----------


def test_plan_marks_blocked_when_file_exists(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file("existing-sha")

    op = AddFile(path="STARTER.md", content="hello\n")
    diff = op.plan(tmp_db, fake_gh)

    assert len(diff.per_repo) == 3
    assert all(r.blocked for r in diff.per_repo)
    assert all("file exists" in r.blocked_reason for r in diff.per_repo)


def test_plan_unblocked_when_file_absent(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing_file_exc()

    op = AddFile(path="STARTER.md", content="line1\nline2\n")
    diff = op.plan(tmp_db, fake_gh)

    assert len(diff.per_repo) == 3
    assert not any(r.blocked for r in diff.per_repo)
    assert all("STARTER.md" in r.unified_diff for r in diff.per_repo)
    assert all(r.summary.startswith("+2 -0 in STARTER.md") for r in diff.per_repo)


def test_plan_respects_assignment_filter(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing_file_exc()
    op = AddFile(path="X", content="x", assignment_id="asn-missing")
    diff = op.plan(tmp_db, fake_gh)
    assert diff.per_repo == []


def test_plan_excludes_archived_repos(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    from tacon.db import archive_repo

    archive_repo(tmp_db, "cs101/alice-hw3")
    fake_repo.get_contents.side_effect = _missing_file_exc()
    op = AddFile(path="X", content="x")
    diff = op.plan(tmp_db, fake_gh)
    repo_ids = {r.repo_id for r in diff.per_repo}
    assert "cs101/alice-hw3" not in repo_ids


# ---------- apply ----------


def test_apply_writes_events_and_records_blob_sha(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }

    op = AddFile(path="X", content="x")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert len(result.per_repo) == 3
    assert all(r.status == "applied" for r in result.per_repo)
    assert all(r.applied_blob_sha == "blob-1" for r in result.per_repo)

    events = get_events_by_op(tmp_db, result.op_id)
    assert len(events) == 3
    assert all(e["status"] == "applied" for e in events)
    assert all(e["applied_blob_sha"] == "blob-1" for e in events)
    assert all(e["commit_sha"] == "c1" for e in events)


def test_apply_skips_blocked_without_calling_create(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file("existing")

    op = AddFile(path="X", content="x")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "skipped" for r in result.per_repo)
    fake_repo.create_file.assert_not_called()


def test_apply_skips_when_confirm_returns_false(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing_file_exc()

    op = AddFile(path="X", content="x")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: False)

    assert all(r.status == "skipped" for r in result.per_repo)
    fake_repo.create_file.assert_not_called()


def test_apply_marks_failed_on_github_exception(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    from github import GithubException

    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.side_effect = GithubException(
        422, {"message": "branch protection rule violated"}, {}
    )

    op = AddFile(path="X", content="x")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "failed" for r in result.per_repo)
    assert all(r.error_class == "permission" for r in result.per_repo)


# ---------- rollback ----------


def test_rollback_deletes_when_blob_matches(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    # Apply first
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    op = AddFile(path="X", content="x")
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Now rollback: file present with the same blob sha (no edits since)
    fake_repo.get_contents.side_effect = None
    fake_repo.get_contents.return_value = _content_file("blob-1")
    fake_repo.delete_file.return_value = {"commit": _commit("revert-c1")}

    result = AddFile.rollback(tmp_db, fake_gh, apply_result.op_id)

    assert all(r.status == "rolled_back" for r in result.per_repo)
    assert fake_repo.delete_file.call_count == 3

    events = get_events_by_op(tmp_db, apply_result.op_id)
    assert all(e["status"] == "rolled_back" for e in events)
    assert all(e["rolled_back_at"] is not None for e in events)


def test_rollback_skipped_dirty_when_blob_changed(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    # Apply
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-original"),
    }
    op = AddFile(path="X", content="x")
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Rollback: file present but with a DIFFERENT blob sha (student edited)
    fake_repo.get_contents.side_effect = None
    fake_repo.get_contents.return_value = _content_file("blob-student-edited")

    result = AddFile.rollback(tmp_db, fake_gh, apply_result.op_id)

    assert all(r.status == "skipped_dirty" for r in result.per_repo)
    fake_repo.delete_file.assert_not_called()
    # Events should reflect the conflict (status='failed', error_class='conflict')
    events = get_events_by_op(tmp_db, apply_result.op_id, status="failed")
    assert len(events) == 3
    assert all(e["error_class"] == "conflict" for e in events)


def test_rollback_idempotent_when_file_already_gone(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    # Apply
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    op = AddFile(path="X", content="x")
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Rollback: file already absent (perhaps student deleted it manually)
    fake_repo.get_contents.side_effect = _missing_file_exc()

    result = AddFile.rollback(tmp_db, fake_gh, apply_result.op_id)

    assert all(r.status == "rolled_back" for r in result.per_repo)
    fake_repo.delete_file.assert_not_called()


def test_apply_via_pr_creates_branch_and_opens_pr(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """The happy path for via-pr: branch created at default-branch SHA, file
    pushed onto the branch, PR opened, event records pr_number + pr_branch."""

    fake_repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha-1"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.create_file.return_value = {
        "commit": _commit("c-pr"),
        "content": _content_file("blob-pr"),
    }
    new_pr = MagicMock(name="PR")
    new_pr.number = 11
    fake_repo.create_pull.return_value = new_pr

    op = AddFile(path="STARTER.md", content="hello\n", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "applied" for r in result.per_repo)
    fake_repo.create_git_ref.assert_called()
    fake_repo.create_pull.assert_called()
    # The branch name in create_pull's head should match the recorded branch.
    head_kwarg = fake_repo.create_pull.call_args.kwargs["head"]
    assert head_kwarg.startswith("tacon/add-file-")
    # Event row holds pr_number + pr_branch + via_pr=true in op_args.
    events = get_events_by_op(tmp_db, result.op_id)
    assert all(e["pr_number"] == 11 for e in events)
    assert all(e["pr_branch"] == head_kwarg for e in events)
    # Defensive: also verify create_file was called with branch=
    file_kwargs = fake_repo.create_file.call_args.kwargs
    assert file_kwargs["branch"] == head_kwarg


def test_apply_via_pr_skips_when_branch_exists_with_different_sha(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Different-SHA branch conflict produces a per-repo skipped event."""
    from github import GithubException

    fake_repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    head = MagicMock(name="Branch")
    head.commit.sha = "expected-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Reference already exists"}, {}
    )
    existing_ref = MagicMock()
    existing_ref.object.sha = "different-sha"  # conflict
    fake_repo.get_git_ref.return_value = existing_ref

    op = AddFile(path="X.md", content="x\n", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "skipped" for r in result.per_repo)
    assert all(r.error_class == "conflict" for r in result.per_repo)
    # No file write happened
    fake_repo.create_file.assert_not_called()
    fake_repo.create_pull.assert_not_called()


def test_apply_via_pr_idempotent_when_branch_exists_with_same_sha(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Same-SHA branch: ensure_branch returns 'exists_same'; we proceed to push +
    open PR. open_or_find_pr handles the case where a PR is already open."""
    from github import GithubException

    fake_repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    head = MagicMock(name="Branch")
    head.commit.sha = "same-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Reference already exists"}, {}
    )
    existing_ref = MagicMock()
    existing_ref.object.sha = "same-sha"  # idempotent re-apply
    fake_repo.get_git_ref.return_value = existing_ref
    fake_repo.create_file.return_value = {
        "commit": _commit("c-idem"),
        "content": _content_file("blob-idem"),
    }
    new_pr = MagicMock()
    new_pr.number = 22
    fake_repo.create_pull.return_value = new_pr

    op = AddFile(path="Y.md", content="y\n", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "applied" for r in result.per_repo)
    # create_file should still have run, on the existing branch
    assert fake_repo.create_file.called


def test_apply_via_pr_failed_on_pr_open_error(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """A non-422 GithubException from create_pull surfaces as a per-repo failure."""
    from github import GithubException

    fake_repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.create_file.return_value = {
        "commit": _commit("c-fail"),
        "content": _content_file("blob-fail"),
    }
    fake_repo.create_pull.side_effect = GithubException(
        500, {"message": "Internal Server Error"}, {}
    )

    op = AddFile(path="Z.md", content="z\n", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "failed" for r in result.per_repo)


def test_args_includes_via_pr_flag() -> None:
    """`via_pr` flows through op.args so resume can reconstruct it."""
    op = AddFile(path="A", content="a", via_pr=True)
    assert op.args["via_pr"] is True
    op_off = AddFile(path="A", content="a", via_pr=False)
    assert op_off.args["via_pr"] is False


def test_rollback_via_pr_closes_pr_and_deletes_branch(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Apply via-pr, then rollback: close the PR + delete the branch."""

    fake_repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.create_file.return_value = {
        "commit": _commit("c-r"),
        "content": _content_file("blob-r"),
    }
    new_pr = MagicMock()
    new_pr.number = 33
    fake_repo.create_pull.return_value = new_pr

    op = AddFile(path="R.md", content="r\n", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    apply_res = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    # Set up rollback: PRs are open, branches exist.
    pr_for_rollback = MagicMock()
    pr_for_rollback.number = 33
    pr_for_rollback.state = "open"
    pr_for_rollback.merged = False
    fake_repo.get_pull.return_value = pr_for_rollback
    branch_ref = MagicMock()
    fake_repo.get_git_ref.return_value = branch_ref

    rb_res = AddFile.rollback(tmp_db, fake_gh, apply_res.op_id)
    assert all(r.status == "rolled_back" for r in rb_res.per_repo)
    # PR.edit was called once per repo with state=closed
    assert pr_for_rollback.edit.call_count == 3
    pr_for_rollback.edit.assert_called_with(state="closed")


def test_rollback_via_pr_skipped_dirty_when_pr_merged(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Merged PR should not auto-revert; surface a clear skipped_dirty."""
    fake_repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.create_file.return_value = {
        "commit": _commit("c-m"),
        "content": _content_file("blob-m"),
    }
    new_pr = MagicMock()
    new_pr.number = 44
    fake_repo.create_pull.return_value = new_pr

    op = AddFile(path="M.md", content="m\n", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    apply_res = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    merged_pr = MagicMock()
    merged_pr.number = 44
    merged_pr.state = "closed"
    merged_pr.merged = True
    merged_pr.merge_commit_sha = "abcd1234deadbeef"
    fake_repo.get_pull.return_value = merged_pr

    rb_res = AddFile.rollback(tmp_db, fake_gh, apply_res.op_id)
    assert all(r.status == "skipped_dirty" for r in rb_res.per_repo)
    assert all("merged" in (r.error_message or "").lower() for r in rb_res.per_repo)
    merged_pr.edit.assert_not_called()


def test_rollback_returns_empty_for_unknown_op_id(tmp_db: Database, fake_gh: MagicMock) -> None:
    result = AddFile.rollback(tmp_db, fake_gh, "op-nonexistent")
    assert result.per_repo == []
