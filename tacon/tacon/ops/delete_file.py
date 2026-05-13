"""DeleteFile: remove a file from N student repos, with diff preview + safe rollback.

API-only (requires_clone=False). Inverse of AddFile:
  - apply: PyGithub repo.delete_file by current blob sha
  - rollback: re-create the file from the deleted blob's content (fetched via
    repo.get_git_blob, which still resolves after the delete because the blob
    is preserved in git's object store as long as some commit references it).

Rollback safety: at rollback time we require the file to currently NOT exist.
If anyone (student or another tool) has put a file back at the same path
between apply and rollback, we refuse to overwrite it (skipped_dirty).
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from github import GithubException, UnknownObjectException

from tacon.db import (
    get_events_by_op,
    list_active_repos,
    now_iso,
    update_event_status,
)
from tacon.github_client import classify_error
from tacon.ops import (
    ApplyResult,
    ConfirmCallback,
    Diff,
    Op,
    RepoDiff,
    RepoRollbackResult,
    RollbackResult,
    register,
)
from tacon.ops._apply_runner import WriteOutcome, run_per_repo_apply
from tacon.ops._via_pr import (
    close_pr_and_delete_branch,
    ensure_branch,
    open_or_find_pr,
)

if TYPE_CHECKING:
    from sqlite_utils import Database

    from tacon.github_client import RateLimitedClient


OP_NAME = "delete-file"
OP_CLASS = "delete_file"


class DeleteFile(Op):
    """Remove a single file from every active repo in scope."""

    requires_clone = False
    supports_rollback = True
    supports_via_pr = True

    def __init__(
        self,
        *,
        path: str,
        message: str = "tacon: delete file",
        assignment_id: str | None = None,
        via_pr: bool = False,
    ) -> None:
        self.path = path
        self.message = message
        self.assignment_id = assignment_id
        self.via_pr = via_pr

    @property
    def args(self) -> dict[str, object]:
        return {
            "path": self.path,
            "message": self.message,
            "assignment_id": self.assignment_id,
            "via_pr": self.via_pr,
        }

    # ---------- plan ----------

    def plan(self, db: Database, gh: RateLimitedClient) -> Diff:
        diff = Diff(op_class=OP_CLASS, op_args=self.args, per_repo=[])
        for row in list_active_repos(db, assignment_id=self.assignment_id):
            repo_id = row["id"]
            student_id = row["student_id"]
            try:
                blocked, reason, line_count = self._inspect(gh, repo_id, row["default_branch"])
            except GithubException as exc:
                blocked, reason, line_count = (
                    True,
                    f"plan failed: {classify_error(exc)} ({exc})",
                    0,
                )
            diff.per_repo.append(
                RepoDiff(
                    repo_id=repo_id,
                    student_id=student_id,
                    summary=self._summary(blocked, line_count),
                    unified_diff=self._render_diff(blocked, reason, line_count),
                    blocked=blocked,
                    blocked_reason=reason,
                )
            )
        return diff

    def _inspect(
        self, gh: RateLimitedClient, repo_id: str, branch: str
    ) -> tuple[bool, str, int]:
        repo = gh.get_repo(repo_id)
        try:
            current = gh.call(repo.get_contents, self.path, ref=branch)
        except UnknownObjectException:
            return True, f"file absent at {self.path} (nothing to delete)", 0
        # Decode current content to estimate the line count for the summary.
        # If decoding fails we still allow the delete; the summary just gets a fallback.
        line_count = 0
        try:
            raw = base64.b64decode(getattr(current, "content", "") or "")
            line_count = raw.count(b"\n") + (
                1 if raw and not raw.endswith(b"\n") else 0
            )
        except (ValueError, TypeError):
            line_count = 0
        return False, "", line_count

    def _summary(self, blocked: bool, line_count: int) -> str:
        if blocked:
            return f"BLOCKED: nothing to delete at {self.path}"
        return f"+0 -{line_count} in {self.path}"

    def _render_diff(self, blocked: bool, blocked_reason: str, line_count: int) -> str:
        if blocked:
            return f"# blocked: {blocked_reason}\n"
        # Synthetic "deleted file" unified diff. We don't fetch full body just for
        # rendering — the dashboard / TUI can pull contents lazily if needed.
        return (
            f"diff --git a/{self.path} b/{self.path}\n"
            "deleted file mode 100644\n"
            f"--- a/{self.path}\n"
            "+++ /dev/null\n"
            f"@@ -1,{line_count} +0,0 @@\n"
            "# (file body omitted from preview)\n"
        )

    # ---------- apply ----------

    def apply(
        self,
        db: Database,
        gh: RateLimitedClient,
        diff: Diff,
        confirm: ConfirmCallback,
    ) -> ApplyResult:
        return run_per_repo_apply(
            op_class_name=OP_CLASS,
            op_args=self.args,
            via_pr=self.via_pr,
            db=db,
            gh=gh,
            diff=diff,
            confirm=confirm,
            direct_write=self._direct_write,
            via_pr_write=self._apply_via_pr,
        )

    def _direct_write(
        self, gh: RateLimitedClient, repo_diff: RepoDiff
    ) -> WriteOutcome:
        commit_sha, blob_sha = self._delete(gh, repo_diff.repo_id)
        return WriteOutcome(commit_sha=commit_sha, blob_sha=blob_sha)

    def _delete(
        self, gh: RateLimitedClient, repo_id: str, *, branch: str | None = None
    ) -> tuple[str, str]:
        """Returns (commit_sha, blob_sha_of_deleted_content).

        ``branch=None`` (default) targets the repo's default branch (existing
        v0.1 behavior). When set, get_contents and delete_file both run
        against the named branch — used by ``--via-pr`` mode.
        """
        repo = gh.get_repo(repo_id)
        # Refetch so the SHA we pass to delete_file is current (the file may
        # have changed between plan and apply).
        get_kwargs: dict[str, object] = {}
        del_kwargs: dict[str, object] = {}
        if branch is not None:
            get_kwargs["ref"] = branch
            del_kwargs["branch"] = branch
        current = gh.call(repo.get_contents, self.path, **get_kwargs)
        current_sha = current.sha
        delete_resp = gh.call(
            repo.delete_file, self.path, self.message, current_sha, **del_kwargs
        )
        commit = delete_resp["commit"]
        return commit.sha, current_sha

    def _apply_via_pr(
        self,
        gh: RateLimitedClient,
        repo_diff: RepoDiff,
        branch_name: str,
        op_id: str,
    ) -> WriteOutcome:
        """Create branch + delete file on it + open PR."""
        repo = gh.get_repo(repo_diff.repo_id)
        default_branch = repo.default_branch
        head = gh.call(repo.get_branch, default_branch)
        base_sha = head.commit.sha

        ensure_branch(gh, repo, branch_name, base_sha)
        commit_sha, blob_sha = self._delete(gh, repo_diff.repo_id, branch=branch_name)
        pr_number = open_or_find_pr(
            gh,
            repo,
            branch=branch_name,
            base=default_branch,
            title=f"tacon: {self.message}",
            body=_build_pr_body(self, repo_diff, op_id),
        )
        return WriteOutcome(
            commit_sha=commit_sha,
            blob_sha=blob_sha,
            pr_number=pr_number,
            pr_branch=branch_name,
        )

    # ---------- rollback ----------

    @classmethod
    def rollback(cls, db: Database, gh: RateLimitedClient, op_id: str) -> RollbackResult:
        result = RollbackResult(op_id=op_id, per_repo=[])
        events = get_events_by_op(db, op_id, status="applied")
        if not events:
            return result

        first = events[0]
        try:
            args = json.loads(first["op_args_json"])
            path = args["path"]
            message = args.get("message") or "tacon: delete file"
        except (KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"malformed op_args_json on op {op_id}: {e}") from e

        revert_message = f"Revert: {message} (tacon op {op_id})"

        for event in events:
            repo_id = event["repo_id"]
            try:
                if event.get("pr_number") is not None:
                    outcome = cls._rollback_via_pr(gh, repo_id, event)
                else:
                    outcome = cls._rollback_one(
                        gh, repo_id, path, event["applied_blob_sha"], revert_message
                    )
            except GithubException as exc:
                err_class = classify_error(exc)
                update_event_status(
                    db,
                    event["id"],
                    status="failed",
                    error_class=err_class,
                    error_message=f"rollback failed: {exc}",
                )
                result.per_repo.append(
                    RepoRollbackResult(
                        repo_id=repo_id,
                        status="failed",
                        error_class=err_class,
                        error_message=str(exc),
                    )
                )
                continue

            if outcome.status == "rolled_back":
                update_event_status(
                    db,
                    event["id"],
                    status="rolled_back",
                    rolled_back_at=now_iso(),
                )
            else:
                update_event_status(
                    db,
                    event["id"],
                    status="failed",
                    error_class="conflict",
                    error_message=outcome.error_message or "skipped: file present at rollback time",
                )
            result.per_repo.append(outcome)
        return result

    @staticmethod
    def _rollback_one(
        gh: RateLimitedClient,
        repo_id: str,
        path: str,
        applied_blob_sha: str | None,
        message: str,
    ) -> RepoRollbackResult:
        repo = gh.get_repo(repo_id)

        # Safety: if a file currently exists at this path, refuse to overwrite
        # whatever someone else put there.
        try:
            existing = gh.call(repo.get_contents, path)
        except UnknownObjectException:
            existing = None
        if existing is not None:
            existing_sha = getattr(existing, "sha", None)
            return RepoRollbackResult(
                repo_id=repo_id,
                status="skipped_dirty",
                error_message=(
                    f"file present at {path} (sha={existing_sha}); "
                    "refusing to overwrite student work"
                ),
            )

        if not applied_blob_sha:
            return RepoRollbackResult(
                repo_id=repo_id,
                status="failed",
                error_message="missing applied_blob_sha; cannot reconstruct deleted content",
            )

        # Fetch the original blob bytes (still reachable in git's object store)
        # and re-create the file with them.
        blob = gh.call(repo.get_git_blob, applied_blob_sha)
        try:
            content_bytes = base64.b64decode(blob.content or "")
        except (ValueError, TypeError) as e:
            return RepoRollbackResult(
                repo_id=repo_id,
                status="failed",
                error_message=f"could not decode blob {applied_blob_sha}: {e}",
            )

        create_resp = gh.call(repo.create_file, path, message, content_bytes)
        revert_sha = create_resp["commit"].sha if create_resp.get("commit") else None
        return RepoRollbackResult(
            repo_id=repo_id,
            status="rolled_back",
            revert_sha=revert_sha,
        )


    @staticmethod
    def _rollback_via_pr(
        gh: RateLimitedClient, repo_id: str, event: dict[str, object]
    ) -> RepoRollbackResult:
        """Rollback for a via-pr DeleteFile event: close PR, delete branch."""
        pr_number_raw = event["pr_number"]
        pr_number = int(pr_number_raw) if isinstance(pr_number_raw, (int, str)) else 0
        branch = str(event["pr_branch"])
        repo = gh.get_repo(repo_id)
        outcome = close_pr_and_delete_branch(
            gh, repo, pr_number=pr_number, branch=branch
        )
        if outcome.status == "skipped_dirty":
            return RepoRollbackResult(
                repo_id=repo_id,
                status="skipped_dirty",
                error_message=outcome.note,
            )
        return RepoRollbackResult(
            repo_id=repo_id,
            status="rolled_back",
            error_message=outcome.note or None,
        )


# ---------- helpers ----------


def _build_pr_body(op: DeleteFile, repo_diff: RepoDiff, op_id: str) -> str:
    """Render the PR body shown in the GitHub UI for a delete-file via-pr."""
    summary = (
        f"`tacon` would like to **delete** `{op.path}` from this repo "
        f"(op id `{op_id}`).\n\nMessage: {op.message}"
    )
    diff_block = (
        f"\n\n## Proposed change\n\n```diff\n{repo_diff.unified_diff.rstrip()}\n```"
    )
    footer = (
        f"\n\n---\n_Generated by `tacon`. To roll back this PR, run "
        f"`tacon rollback {op_id}` locally._"
    )
    return summary + diff_block + footer


# Register on import so cli.rollback / cli.run can find the class by name
register(OP_NAME, DeleteFile)
