"""AddFile: push a file to N student repos, with diff preview + safe rollback.

API-only (requires_clone=False) because:
  - apply: PyGithub repo.create_file does the create directly
  - rollback: repo.delete_file by content_sha undoes it
  - diff: just "new file with this content" — no merge, no clone needed

Rollback safety: store the blob SHA at apply time. On rollback, fetch the
file's CURRENT blob SHA and compare. Mismatch -> skipped_dirty (no delete).
This eliminates the "student git-reverted and re-added similar content"
silent-data-loss path that commit-lineage checks miss.
"""

from __future__ import annotations

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


OP_NAME = "add-file"
OP_CLASS = "add_file"


class AddFile(Op):
    """Push a single file to every active repo in scope."""

    requires_clone = False
    supports_rollback = True

    # Subclasses (e.g. AddCIWorkflow) override to register a different op_class
    # in the events table and registry without duplicating apply/rollback.
    op_class_name: str = OP_CLASS
    default_revert_message: str = "tacon: add file"

    def __init__(
        self,
        *,
        path: str,
        content: str,
        message: str = "tacon: add file",
        assignment_id: str | None = None,
    ) -> None:
        self.path = path
        self.content = content
        self.message = message
        self.assignment_id = assignment_id

    @property
    def args(self) -> dict[str, object]:
        return {
            "path": self.path,
            # NOTE: content is intentionally hashed in args, not stored, to keep
            # op_args_json small. The actual bytes pushed are fully reconstructable
            # from applied_blob_sha + the GitHub repo.
            "content_len": len(self.content.encode("utf-8")),
            "message": self.message,
            "assignment_id": self.assignment_id,
        }

    # ---------- plan ----------

    def plan(self, db: Database, gh: RateLimitedClient) -> Diff:
        diff = Diff(op_class=self.op_class_name, op_args=self.args, per_repo=[])
        for row in list_active_repos(db, assignment_id=self.assignment_id):
            repo_id = row["id"]
            student_id = row["student_id"]
            try:
                blocked, reason = self._check_blocked(gh, repo_id, row["default_branch"])
            except GithubException as exc:
                # Repo unreachable at plan time — surface as blocked rather than crash
                blocked = True
                reason = f"plan failed: {classify_error(exc)} ({exc})"
            diff.per_repo.append(
                RepoDiff(
                    repo_id=repo_id,
                    student_id=student_id,
                    summary=self._summary(blocked),
                    unified_diff=self._render_diff(blocked, reason),
                    blocked=blocked,
                    blocked_reason=reason,
                )
            )
        return diff

    def _check_blocked(self, gh: RateLimitedClient, repo_id: str, branch: str) -> tuple[bool, str]:
        repo = gh.get_repo(repo_id)
        try:
            existing = gh.call(repo.get_contents, self.path, ref=branch)
            # File already exists at that path — block (don't overwrite)
            existing_sha = getattr(existing, "sha", None) if existing is not None else None
            return True, f"file exists at {self.path} (sha={existing_sha})"
        except UnknownObjectException:
            return False, ""

    def _summary(self, blocked: bool) -> str:
        if blocked:
            return f"BLOCKED: file already present at {self.path}"
        line_count = self.content.count("\n") + (
            1 if self.content and not self.content.endswith("\n") else 0
        )
        return f"+{line_count} -0 in {self.path}"

    def _render_diff(self, blocked: bool, blocked_reason: str) -> str:
        if blocked:
            return f"# blocked: {blocked_reason}\n"
        # Synthetic "new file" unified diff
        lines = [
            f"diff --git a/{self.path} b/{self.path}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{self.path}",
        ]
        body = self.content.splitlines() or [""]
        lines.append(f"@@ -0,0 +1,{len(body)} @@")
        lines.extend("+" + ln for ln in body)
        return "\n".join(lines) + "\n"

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
            # Pre-record: every per-repo decision lands in events, even skips.
            event_id = insert_event(
                db,
                op_id=op_id,
                op_class=self.op_class_name,
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
                    error_class=None,
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
                commit_sha, blob_sha = self._push_file(gh, repo_diff.repo_id)
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

    def _push_file(self, gh: RateLimitedClient, repo_id: str) -> tuple[str, str]:
        """Returns (commit_sha, blob_sha) of the new file."""
        repo = gh.get_repo(repo_id)
        response = gh.call(
            repo.create_file,
            self.path,
            self.message,
            self.content,
        )
        # PyGithub returns {'commit': Commit, 'content': ContentFile}
        commit = response["commit"]
        content = response["content"]
        return commit.sha, content.sha

    # ---------- rollback ----------

    @classmethod
    def rollback(cls, db: Database, gh: RateLimitedClient, op_id: str) -> RollbackResult:
        result = RollbackResult(op_id=op_id, per_repo=[])
        events = get_events_by_op(db, op_id, status="applied")
        if not events:
            return result

        # Recover the file path from op_args_json (same for every event in the op)
        first = events[0]
        try:
            args = json.loads(first["op_args_json"])
            path = args["path"]
            message = args.get("message") or cls.default_revert_message
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
                    error_message=outcome.error_message or "skipped: blob mismatch",
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
        try:
            current = gh.call(repo.get_contents, path)
        except UnknownObjectException:
            # File already gone — treat as success (idempotent rollback)
            return RepoRollbackResult(
                repo_id=repo_id,
                status="rolled_back",
                error_message="file already absent",
            )
        current_sha = getattr(current, "sha", None)
        if not applied_blob_sha or current_sha != applied_blob_sha:
            return RepoRollbackResult(
                repo_id=repo_id,
                status="skipped_dirty",
                error_message=(
                    f"blob sha mismatch (current={current_sha} vs "
                    f"applied={applied_blob_sha}); refusing to delete student work"
                ),
            )
        delete_resp = gh.call(repo.delete_file, path, message, current_sha)
        revert_sha = delete_resp["commit"].sha if delete_resp.get("commit") else None
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
register(OP_NAME, AddFile)
