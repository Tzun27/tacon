"""AddFile: push a file to N student repos, with diff preview + safe rollback.

API-only (requires_clone=False) because:
  - apply: PyGithub repo.create_file does the create directly
  - rollback: repo.delete_file by content_sha undoes it
  - diff: just "new file with this content" — no merge, no clone needed

Rollback safety: store the blob SHA at apply time. On rollback, fetch the
file's CURRENT blob SHA and compare. Mismatch -> skipped_dirty (no delete).
This eliminates the "student git-reverted and re-added similar content"
silent-data-loss path that commit-lineage checks miss.

`--via-pr` mode (v0.2): instead of pushing to the repo's default branch,
we create `tacon/<op-class>-<op-id-prefix>` at default-branch HEAD, push
the file there, and open a PR. Rollback closes the PR and deletes the
branch. Most of that lives in `tacon.ops._via_pr`; this module just
threads `branch=` through PyGithub's contents API and decides which
helper to call when the via_pr flag is set.
"""

from __future__ import annotations

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


OP_NAME = "add-file"
OP_CLASS = "add_file"


class AddFile(Op):
    """Push a single file to every active repo in scope."""

    requires_clone = False
    supports_rollback = True
    supports_via_pr = True

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
        via_pr: bool = False,
    ) -> None:
        self.path = path
        self.content = content
        self.message = message
        self.assignment_id = assignment_id
        self.via_pr = via_pr

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
            "via_pr": self.via_pr,
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
        return run_per_repo_apply(
            op_class_name=self.op_class_name,
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
        commit_sha, blob_sha = self._push_file(gh, repo_diff.repo_id)
        return WriteOutcome(commit_sha=commit_sha, blob_sha=blob_sha)

    def _push_file(
        self, gh: RateLimitedClient, repo_id: str, *, branch: str | None = None
    ) -> tuple[str, str]:
        """Returns (commit_sha, blob_sha) of the new file.

        ``branch=None`` (default) targets the repo's default branch (existing
        v0.1 behavior). When set, PyGithub's ``create_file`` writes the new
        blob onto the named branch directly — used by ``--via-pr`` mode.
        """
        repo = gh.get_repo(repo_id)
        kwargs: dict[str, object] = {}
        if branch is not None:
            kwargs["branch"] = branch
        response = gh.call(
            repo.create_file,
            self.path,
            self.message,
            self.content,
            **kwargs,
        )
        # PyGithub returns {'commit': Commit, 'content': ContentFile}
        commit = response["commit"]
        content = response["content"]
        return commit.sha, content.sha

    def _apply_via_pr(
        self,
        gh: RateLimitedClient,
        repo_diff: RepoDiff,
        branch_name: str,
        op_id: str,
    ) -> WriteOutcome:
        """The via-pr applies a write on a fresh tacon branch + opens a PR.

        Step order:
        1. Get the default branch HEAD SHA (one call per repo).
        2. ensure_branch(...) creates `branch_name` at that SHA, or no-ops
           if the branch already exists at the same SHA. Different SHA
           raises BranchConflictError (caller maps to skipped).
        3. _push_file with branch=branch_name writes the blob.
        4. open_or_find_pr opens (or recovers) the PR.
        """
        repo = gh.get_repo(repo_diff.repo_id)
        default_branch = repo.default_branch
        head = gh.call(repo.get_branch, default_branch)
        base_sha = head.commit.sha

        ensure_branch(gh, repo, branch_name, base_sha)
        commit_sha, blob_sha = self._push_file(gh, repo_diff.repo_id, branch=branch_name)
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
            via_pr_event = event.get("pr_number") is not None
            try:
                if via_pr_event:
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

    @staticmethod
    def _rollback_via_pr(
        gh: RateLimitedClient, repo_id: str, event: dict[str, object]
    ) -> RepoRollbackResult:
        """Rollback for a via-pr event: close the PR, delete the branch.

        See `tacon.ops._via_pr.close_pr_and_delete_branch` for the state
        table. Merged PRs are surfaced as ``skipped_dirty`` — the student/TA
        merged the change, and we deliberately don't auto-revert against
        default branch (which is the very surface --via-pr exists to avoid).
        """
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


def _build_pr_body(op: AddFile, repo_diff: RepoDiff, op_id: str) -> str:
    """Render the PR body shown in the GitHub UI.

    Sections: 1-line tacon summary, the unified diff from plan(), and a
    correlation footer that names the op_id so a TA reading the PR can
    cross-reference their local events table without machine parsing.
    """
    summary = (
        f"`tacon` would like to apply **{op.op_class_name}** to this repo "
        f"(op id `{op_id}`).\n\nFile: `{op.path}`\nMessage: {op.message}"
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
register(OP_NAME, AddFile)
