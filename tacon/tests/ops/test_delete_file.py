"""Tests for DeleteFile: plan, apply, rollback (with mocked PyGithub)."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

from github import GithubException, UnknownObjectException
from sqlite_utils import Database

from tacon.db import get_events_by_op
from tacon.ops import get_op_class
from tacon.ops.delete_file import DeleteFile


def _content_file(sha: str = "blob-sha-1", body: bytes = b"line1\nline2\n") -> MagicMock:
    cf = MagicMock(name="ContentFile")
    cf.sha = sha
    cf.content = base64.b64encode(body).decode("ascii")
    return cf


def _commit(sha: str = "commit-sha-1") -> MagicMock:
    c = MagicMock(name="Commit")
    c.sha = sha
    return c


def _blob(body: bytes) -> MagicMock:
    b = MagicMock(name="GitBlob")
    b.content = base64.b64encode(body).decode("ascii")
    b.encoding = "base64"
    return b


def _missing() -> UnknownObjectException:
    return UnknownObjectException(404, {"message": "Not Found"}, {})


# ---------- registry ----------


def test_delete_file_is_registered() -> None:
    assert get_op_class("delete-file") is DeleteFile


# ---------- plan ----------


def test_plan_unblocked_when_file_present(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file("orig-sha", b"a\nb\nc\n")

    op = DeleteFile(path="STARTER.md")
    diff = op.plan(tmp_db, fake_gh)

    assert len(diff.per_repo) == 3
    assert not any(r.blocked for r in diff.per_repo)
    assert all("+0 -3" in r.summary for r in diff.per_repo)
    assert all("STARTER.md" in r.unified_diff for r in diff.per_repo)


def test_plan_blocked_when_file_absent(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing()

    op = DeleteFile(path="STARTER.md")
    diff = op.plan(tmp_db, fake_gh)

    assert all(r.blocked for r in diff.per_repo)
    assert all("file absent" in r.blocked_reason for r in diff.per_repo)


def test_plan_handles_repo_unreachable(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = GithubException(500, {"message": "boom"}, {})

    op = DeleteFile(path="STARTER.md")
    diff = op.plan(tmp_db, fake_gh)

    assert all(r.blocked for r in diff.per_repo)
    assert all("plan failed" in r.blocked_reason for r in diff.per_repo)


def test_plan_respects_assignment_filter(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file()
    op = DeleteFile(path="X", assignment_id="asn-missing")
    diff = op.plan(tmp_db, fake_gh)
    assert diff.per_repo == []


# ---------- apply ----------


def test_apply_deletes_and_records_blob_sha(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file("orig-sha", b"x\n")
    fake_repo.delete_file.return_value = {"commit": _commit("c1")}

    op = DeleteFile(path="X")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "applied" for r in result.per_repo)
    assert all(r.applied_blob_sha == "orig-sha" for r in result.per_repo)

    events = get_events_by_op(tmp_db, result.op_id)
    assert len(events) == 3
    assert all(e["applied_blob_sha"] == "orig-sha" for e in events)
    assert all(e["op_class"] == "delete_file" for e in events)


def test_apply_skips_blocked_without_calling_delete(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing()

    op = DeleteFile(path="X")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "skipped" for r in result.per_repo)
    fake_repo.delete_file.assert_not_called()


def test_apply_skips_when_confirm_returns_false(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file()
    op = DeleteFile(path="X")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: False)

    assert all(r.status == "skipped" for r in result.per_repo)
    fake_repo.delete_file.assert_not_called()


def test_apply_marks_failed_on_github_exception(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file()
    fake_repo.delete_file.side_effect = GithubException(
        409, {"message": "conflict"}, {}
    )

    op = DeleteFile(path="X")
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    assert all(r.status == "failed" for r in result.per_repo)
    assert all(r.error_class == "conflict" for r in result.per_repo)


# ---------- rollback ----------


def test_rollback_recreates_file_from_blob(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    body = b"original-content\n"
    # Apply (delete)
    fake_repo.get_contents.return_value = _content_file("orig-sha", body)
    fake_repo.delete_file.return_value = {"commit": _commit("c1")}
    op = DeleteFile(path="X")
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Rollback: file currently absent (good — we did delete it), blob still
    # fetchable, recreate via create_file.
    fake_repo.get_contents.side_effect = _missing()
    fake_repo.get_contents.return_value = None
    fake_repo.get_git_blob.return_value = _blob(body)
    fake_repo.create_file.return_value = {"commit": _commit("revert-c1")}

    result = DeleteFile.rollback(tmp_db, fake_gh, apply_result.op_id)

    assert all(r.status == "rolled_back" for r in result.per_repo)
    assert fake_repo.create_file.call_count == 3
    # The bytes passed to create_file must match the original body
    for call in fake_repo.create_file.call_args_list:
        assert call.args[2] == body

    events = get_events_by_op(tmp_db, apply_result.op_id)
    assert all(e["status"] == "rolled_back" for e in events)
    assert all(e["rolled_back_at"] is not None for e in events)


def test_rollback_skipped_dirty_when_file_reappeared(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    body = b"original\n"
    fake_repo.get_contents.return_value = _content_file("orig-sha", body)
    fake_repo.delete_file.return_value = {"commit": _commit("c1")}
    op = DeleteFile(path="X")
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Rollback: a file is now present at the path (someone re-added it).
    # Don't overwrite — skipped_dirty.
    fake_repo.get_contents.side_effect = None
    fake_repo.get_contents.return_value = _content_file("new-sha", b"different\n")

    result = DeleteFile.rollback(tmp_db, fake_gh, apply_result.op_id)

    assert all(r.status == "skipped_dirty" for r in result.per_repo)
    fake_repo.create_file.assert_not_called()
    events = get_events_by_op(tmp_db, apply_result.op_id, status="failed")
    assert len(events) == 3
    assert all(e["error_class"] == "conflict" for e in events)


def test_rollback_returns_empty_for_unknown_op_id(tmp_db: Database, fake_gh: MagicMock) -> None:
    result = DeleteFile.rollback(tmp_db, fake_gh, "op-nonexistent")
    assert result.per_repo == []


# ---------- via_pr ----------


def test_apply_via_pr_creates_branch_and_opens_pr_for_delete(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Happy path: file exists in default branch; via-pr creates a branch,
    deletes the file ON THE BRANCH, opens a PR. Default branch unchanged."""
    fake_repo.get_contents.return_value = _content_file("blob-original")
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.delete_file.return_value = {"commit": _commit("c-del")}
    new_pr = MagicMock(name="PR")
    new_pr.number = 17
    fake_repo.create_pull.return_value = new_pr

    op = DeleteFile(path="OLD.md", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "applied" for r in result.per_repo)
    fake_repo.create_git_ref.assert_called()
    fake_repo.create_pull.assert_called()
    # delete_file should have been called WITH branch=
    del_kwargs = fake_repo.delete_file.call_args.kwargs
    assert del_kwargs["branch"].startswith("tacon/delete-file-")
    # The PR head should match the branch we deleted on
    head_kwarg = fake_repo.create_pull.call_args.kwargs["head"]
    assert head_kwarg == del_kwargs["branch"]
    events = get_events_by_op(tmp_db, result.op_id)
    assert all(e["pr_number"] == 17 for e in events)
    assert all(e["pr_branch"] == head_kwarg for e in events)


def test_apply_via_pr_skipped_on_branch_conflict_for_delete(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Branch already exists with a different SHA → skipped_dirty per repo."""
    fake_repo.get_contents.return_value = _content_file("blob-original")
    head = MagicMock(name="Branch")
    head.commit.sha = "expected-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Reference already exists"}, {}
    )
    existing_ref = MagicMock()
    existing_ref.object.sha = "different-sha"
    fake_repo.get_git_ref.return_value = existing_ref

    op = DeleteFile(path="X", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    result = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    assert all(r.status == "skipped" for r in result.per_repo)
    assert all(r.error_class == "conflict" for r in result.per_repo)
    fake_repo.delete_file.assert_not_called()


def test_args_includes_via_pr_for_delete() -> None:
    op = DeleteFile(path="A", via_pr=True)
    assert op.args["via_pr"] is True
    assert DeleteFile(path="A", via_pr=False).args["via_pr"] is False


def test_rollback_via_pr_closes_pr_and_deletes_branch_for_delete(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Apply via-pr, then rollback: PR closes + branch deletes."""
    fake_repo.get_contents.return_value = _content_file("blob-original")
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.delete_file.return_value = {"commit": _commit("c-del")}
    new_pr = MagicMock()
    new_pr.number = 25
    fake_repo.create_pull.return_value = new_pr

    op = DeleteFile(path="X", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    apply_res = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    pr_for_rollback = MagicMock()
    pr_for_rollback.number = 25
    pr_for_rollback.state = "open"
    pr_for_rollback.merged = False
    fake_repo.get_pull.return_value = pr_for_rollback
    branch_ref = MagicMock()
    fake_repo.get_git_ref.return_value = branch_ref

    rb_res = DeleteFile.rollback(tmp_db, fake_gh, apply_res.op_id)
    assert all(r.status == "rolled_back" for r in rb_res.per_repo)
    assert pr_for_rollback.edit.call_count == 3


def test_rollback_via_pr_skipped_dirty_when_merged_for_delete(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Merged via-pr DeleteFile: skipped_dirty (TA must revert manually)."""
    fake_repo.get_contents.return_value = _content_file("blob-original")
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.delete_file.return_value = {"commit": _commit("c-m")}
    new_pr = MagicMock()
    new_pr.number = 26
    fake_repo.create_pull.return_value = new_pr

    op = DeleteFile(path="X", via_pr=True)
    diff = op.plan(tmp_db, fake_gh)
    apply_res = op.apply(tmp_db, fake_gh, diff, lambda r: True)

    merged_pr = MagicMock()
    merged_pr.number = 26
    merged_pr.state = "closed"
    merged_pr.merged = True
    merged_pr.merge_commit_sha = "abcd1234deadbeef"
    fake_repo.get_pull.return_value = merged_pr

    rb_res = DeleteFile.rollback(tmp_db, fake_gh, apply_res.op_id)
    assert all(r.status == "skipped_dirty" for r in rb_res.per_repo)
    merged_pr.edit.assert_not_called()
