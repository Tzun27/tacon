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

from tacon import __version__
from tacon.db import (
    get_events_by_op,
    insert_event,
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
    RepoApplyResult,
    RepoDiff,
    RepoRollbackResult,
    RollbackResult,
    register,
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

    def __init__(
        self,
        *,
        path: str,
        message: str = "tacon: delete file",
        assignment_id: str | None = None,
    ) -> None:
        self.path = path
        self.message = message
        self.assignment_id = assignment_id

    @property
    def args(self) -> dict[str, object]:
        return {
            "path": self.path,
            "message": self.message,
            "assignment_id": self.assignment_id,
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
        op_id = _new_op_id()
        op_args_json = json.dumps(self.args, sort_keys=True)
        result = ApplyResult(op_id=op_id, per_repo=[])

        for repo_diff in diff.per_repo:
            event_id = insert_event(
                db,
                op_id=op_id,
                op_class=OP_CLASS,
                op_args_json=op_args_json,
                tacon_version=__version__,
                repo_id=repo_diff.repo_id,
                student_id=repo_diff.student_id,
                status="planned",
            )

            if repo_diff.blocked:
                update_event_status(
                    db,
                    event_id,
                    status="skipped",
                    error_message=repo_diff.blocked_reason,
                )
                result.per_repo.append(
                    RepoApplyResult(
                        repo_id=repo_diff.repo_id,
                        status="skipped",
                        error_message=repo_diff.blocked_reason,
                    )
                )
                continue

            if not confirm(repo_diff):
                update_event_status(
                    db, event_id, status="skipped", error_message="declined by confirm callback"
                )
                result.per_repo.append(RepoApplyResult(repo_id=repo_diff.repo_id, status="skipped"))
                continue

            try:
                commit_sha, blob_sha = self._delete(gh, repo_diff.repo_id)
            except GithubException as exc:
                err_class = classify_error(exc)
                update_event_status(
                    db,
                    event_id,
                    status="failed",
                    error_class=err_class,
                    error_message=str(exc),
                    applied_at=now_iso(),
                )
                result.per_repo.append(
                    RepoApplyResult(
                        repo_id=repo_diff.repo_id,
                        status="failed",
                        error_class=err_class,
                        error_message=str(exc),
                    )
                )
                continue

            update_event_status(
                db,
                event_id,
                status="applied",
                commit_sha=commit_sha,
                applied_blob_sha=blob_sha,
                applied_at=now_iso(),
            )
            result.per_repo.append(
                RepoApplyResult(
                    repo_id=repo_diff.repo_id,
                    status="applied",
                    commit_sha=commit_sha,
                    applied_blob_sha=blob_sha,
                )
            )

        return result

    def _delete(self, gh: RateLimitedClient, repo_id: str) -> tuple[str, str]:
        """Returns (commit_sha, blob_sha_of_deleted_content)."""
        repo = gh.get_repo(repo_id)
        # Refetch so the SHA we pass to delete_file is current (the file may
        # have changed between plan and apply).
        current = gh.call(repo.get_contents, self.path)
        current_sha = current.sha
        delete_resp = gh.call(repo.delete_file, self.path, self.message, current_sha)
        commit = delete_resp["commit"]
        return commit.sha, current_sha

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
            applied_blob_sha = event["applied_blob_sha"]
            try:
                outcome = cls._rollback_one(gh, repo_id, path, applied_blob_sha, revert_message)
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


# ---------- helpers ----------


def _new_op_id() -> str:
    import uuid

    return str(uuid.uuid4())


# Register on import so cli.rollback / cli.run can find the class by name
register(OP_NAME, DeleteFile)
