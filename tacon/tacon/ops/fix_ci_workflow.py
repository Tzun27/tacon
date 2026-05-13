"""FixCIWorkflow: patch an existing CI workflow across N student repos.

Caller supplies a `transform: Callable[[bytes], bytes | None]` that takes the
current workflow bytes and returns the patched bytes (or None / unchanged
to mark the repo as a no-op).

Pre-baked transforms live in this module:
  - `make_bump_action_transform("actions/checkout@v3", "actions/checkout@v4")`
    returns a transform that replaces every occurrence of the old reference
    with the new one. Safe regex-free string substitution on bytes.

Rollback uses git history: at rollback time, fetch the file from the parent
of the apply commit (still in the object store). No schema change required.

Safety: at rollback time we verify that the current blob still matches the
post-apply blob we recorded; if a student edited the workflow since, we
refuse to overwrite (skipped_dirty).
"""

from __future__ import annotations

import base64
import difflib
import json
from collections.abc import Callable
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
from tacon.ops.add_ci_workflow import _NAME_RE, WorkflowValidationError

if TYPE_CHECKING:
    from sqlite_utils import Database

    from tacon.github_client import RateLimitedClient


OP_NAME = "fix-ci-workflow"
OP_CLASS = "fix_ci_workflow"


Transform = Callable[[bytes], bytes | None]


class FixCIWorkflow(Op):
    """Patch an existing workflow file in every active repo in scope."""

    requires_clone = False
    supports_rollback = True
    supports_via_pr = True

    def __init__(
        self,
        *,
        name: str,
        transform: Transform,
        transform_id: str,
        message: str = "tacon: fix CI workflow",
        assignment_id: str | None = None,
        via_pr: bool = False,
    ) -> None:
        if not _NAME_RE.match(name):
            raise WorkflowValidationError(
                f"invalid workflow name {name!r}: must match {_NAME_RE.pattern}"
            )
        self.workflow_name = name
        self.transform = transform
        self.transform_id = transform_id
        self.message = message
        self.assignment_id = assignment_id
        self.via_pr = via_pr

        filename = name if name.endswith((".yml", ".yaml")) else f"{name}.yml"
        self.path = f".github/workflows/{filename}"

    @property
    def args(self) -> dict[str, object]:
        # transform itself is not serializable; we record its identifier so a
        # human reading the events table can reconstruct what the op did.
        return {
            "path": self.path,
            "workflow_name": self.workflow_name,
            "transform_id": self.transform_id,
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
                blocked, reason, summary, ud = self._project(
                    gh, repo_id, row["default_branch"]
                )
            except GithubException as exc:
                blocked = True
                reason = f"plan failed: {classify_error(exc)} ({exc})"
                summary = f"BLOCKED: plan failed for {self.path}"
                ud = f"# blocked: {reason}\n"
            diff.per_repo.append(
                RepoDiff(
                    repo_id=repo_id,
                    student_id=student_id,
                    summary=summary,
                    unified_diff=ud,
                    blocked=blocked,
                    blocked_reason=reason,
                )
            )
        return diff

    def _project(
        self, gh: RateLimitedClient, repo_id: str, branch: str
    ) -> tuple[bool, str, str, str]:
        """Returns (blocked, reason, summary, unified_diff)."""
        repo = gh.get_repo(repo_id)
        try:
            current = gh.call(repo.get_contents, self.path, ref=branch)
        except UnknownObjectException:
            return (
                True,
                f"workflow not present at {self.path}",
                f"BLOCKED: nothing to fix at {self.path}",
                f"# blocked: workflow absent at {self.path}\n",
            )
        old_bytes = base64.b64decode(getattr(current, "content", "") or "")
        new_bytes = self.transform(old_bytes)
        if new_bytes is None or new_bytes == old_bytes:
            return (
                True,
                "transform is a no-op for this repo",
                f"BLOCKED: {self.transform_id} not applicable",
                f"# blocked: {self.transform_id} produced no change\n",
            )
        return (
            False,
            "",
            self._summary(old_bytes, new_bytes),
            self._unified_diff(old_bytes, new_bytes),
        )

    def _summary(self, old: bytes, new: bytes) -> str:
        added, removed = _count_changed_lines(old, new)
        return (
            f"+{added} -{removed} in {self.path} via {self.transform_id}"
        )

    def _unified_diff(self, old: bytes, new: bytes) -> str:
        old_lines = old.decode("utf-8", errors="replace").splitlines(keepends=True)
        new_lines = new.decode("utf-8", errors="replace").splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{self.path}",
                tofile=f"b/{self.path}",
                n=3,
            )
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
            race_skipped_message="transform no longer applies (state changed since plan)",
        )

    def _direct_write(
        self, gh: RateLimitedClient, repo_diff: RepoDiff
    ) -> WriteOutcome | None:
        outcome = self._patch(gh, repo_diff.repo_id)
        if outcome is None:
            return None  # signals race-skip to the helper
        commit_sha, blob_sha = outcome
        return WriteOutcome(commit_sha=commit_sha, blob_sha=blob_sha)

    def _patch(
        self, gh: RateLimitedClient, repo_id: str, *, branch: str | None = None
    ) -> tuple[str, str] | None:
        """Returns (commit_sha, new_blob_sha) on patch, or None if nothing to do.

        ``branch=None`` (default) targets the repo's default branch (existing
        v0.1 behavior). When set, get_contents and update_file both run
        against the named branch — used by ``--via-pr`` mode.
        """
        repo = gh.get_repo(repo_id)
        get_kwargs: dict[str, object] = {}
        if branch is not None:
            get_kwargs["ref"] = branch
        current = gh.call(repo.get_contents, self.path, **get_kwargs)
        old_bytes = base64.b64decode(getattr(current, "content", "") or "")
        new_bytes = self.transform(old_bytes)
        if new_bytes is None or new_bytes == old_bytes:
            return None
        update_kwargs: dict[str, object] = {}
        if branch is not None:
            update_kwargs["branch"] = branch
        resp = gh.call(
            repo.update_file,
            self.path,
            self.message,
            new_bytes,
            current.sha,
            **update_kwargs,
        )
        commit = resp["commit"]
        content = resp["content"]
        return commit.sha, content.sha

    def _apply_via_pr(
        self,
        gh: RateLimitedClient,
        repo_diff: RepoDiff,
        branch_name: str,
        op_id: str,
    ) -> WriteOutcome | None:
        """Branch + patch + open PR. Returns the WriteOutcome, or None on race.

        Threads `branch=` into `_patch`. If `_patch` returns None (the
        transform is now a no-op against this repo's HEAD — race between
        plan and apply), we skip without opening a PR; the helper maps
        None to a `skipped` event with the configured race message.
        """
        repo = gh.get_repo(repo_diff.repo_id)
        default_branch = repo.default_branch
        head = gh.call(repo.get_branch, default_branch)
        base_sha = head.commit.sha

        ensure_branch(gh, repo, branch_name, base_sha)
        patch_outcome = self._patch(gh, repo_diff.repo_id, branch=branch_name)
        if patch_outcome is None:
            return None
        commit_sha, blob_sha = patch_outcome
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
            message = args.get("message") or "tacon: fix CI workflow"
        except (KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"malformed op_args_json on op {op_id}: {e}") from e

        revert_message = f"Revert: {message} (tacon op {op_id})"

        for event in events:
            try:
                if event.get("pr_number") is not None:
                    outcome = cls._rollback_via_pr(gh, event["repo_id"], event)
                else:
                    outcome = cls._rollback_one(
                        gh,
                        event["repo_id"],
                        path,
                        event["applied_blob_sha"],
                        event["commit_sha"],
                        revert_message,
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
                        repo_id=event["repo_id"],
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
                    error_message=outcome.error_message or "skipped",
                )
            result.per_repo.append(outcome)
        return result

    @staticmethod
    def _rollback_one(
        gh: RateLimitedClient,
        repo_id: str,
        path: str,
        applied_blob_sha: str | None,
        commit_sha: str | None,
        message: str,
    ) -> RepoRollbackResult:
        repo = gh.get_repo(repo_id)

        if not applied_blob_sha or not commit_sha:
            return RepoRollbackResult(
                repo_id=repo_id,
                status="failed",
                error_message="missing applied_blob_sha or commit_sha; cannot revert",
            )

        # Current state must still match what we applied — otherwise a student
        # has edited the workflow and we won't clobber their work.
        try:
            current = gh.call(repo.get_contents, path)
        except UnknownObjectException:
            return RepoRollbackResult(
                repo_id=repo_id,
                status="skipped_dirty",
                error_message=f"workflow no longer exists at {path} — refusing to revert",
            )
        current_sha = getattr(current, "sha", None)
        if current_sha != applied_blob_sha:
            return RepoRollbackResult(
                repo_id=repo_id,
                status="skipped_dirty",
                error_message=(
                    f"blob sha mismatch (current={current_sha} vs "
                    f"applied={applied_blob_sha}); refusing to overwrite student work"
                ),
            )

        # Reconstruct the prior content from the parent of the apply commit.
        # Git preserves blobs reachable from any commit, so this is well-defined.
        apply_commit = gh.call(repo.get_commit, commit_sha)
        parents = getattr(apply_commit, "parents", []) or []
        if not parents:
            return RepoRollbackResult(
                repo_id=repo_id,
                status="failed",
                error_message=f"apply commit {commit_sha} has no parent; cannot revert",
            )
        parent_sha = parents[0].sha
        try:
            prior = gh.call(repo.get_contents, path, ref=parent_sha)
        except UnknownObjectException:
            # The file did NOT exist before our apply. That means our apply
            # was effectively a create, not a fix — full revert means delete.
            # FixCIWorkflow only patches; it never creates. So this state means
            # someone replayed an apply onto a delete. Best-effort: refuse.
            return RepoRollbackResult(
                repo_id=repo_id,
                status="failed",
                error_message=(
                    f"file did not exist at parent commit {parent_sha[:8]}; "
                    "FixCIWorkflow rollback can't infer create-vs-update history"
                ),
            )
        prior_bytes = base64.b64decode(getattr(prior, "content", "") or "")

        resp = gh.call(repo.update_file, path, message, prior_bytes, current_sha)
        revert_sha = resp["commit"].sha if resp.get("commit") else None
        return RepoRollbackResult(
            repo_id=repo_id,
            status="rolled_back",
            revert_sha=revert_sha,
        )


    @staticmethod
    def _rollback_via_pr(
        gh: RateLimitedClient, repo_id: str, event: dict[str, object]
    ) -> RepoRollbackResult:
        """Rollback for a via-pr FixCIWorkflow event: close PR + delete branch.

        Symmetric with AddFile/DeleteFile via-pr rollbacks. The merged-PR
        case is handled the same way: skipped_dirty + manual-revert hint
        rather than auto-revert against default branch.
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


# ---------- pr body ----------


def _build_pr_body(op: FixCIWorkflow, repo_diff: RepoDiff, op_id: str) -> str:
    """Render the PR body for a fix-ci-workflow via-pr."""
    summary = (
        f"`tacon` would like to apply **{op.transform_id}** to "
        f"`{op.path}` in this repo (op id `{op_id}`).\n\nMessage: {op.message}"
    )
    diff_block = (
        f"\n\n## Proposed change\n\n```diff\n{repo_diff.unified_diff.rstrip()}\n```"
    )
    footer = (
        f"\n\n---\n_Generated by `tacon`. To roll back this PR, run "
        f"`tacon rollback {op_id}` locally._"
    )
    return summary + diff_block + footer


# ---------- pre-baked transforms ----------


def make_bump_action_transform(from_ref: str, to_ref: str) -> Transform:
    """Build a transform that replaces every `from_ref` with `to_ref`.

    Both args must be byte-decodable (e.g. ``actions/checkout@v3``).
    The substitution is plain literal — no regex — so e.g. bumping
    ``actions/checkout@v3`` won't accidentally match ``v33``.
    """
    if not from_ref or not to_ref:
        raise ValueError("bump-action requires non-empty from_ref and to_ref")
    if from_ref == to_ref:
        raise ValueError("bump-action from_ref and to_ref are identical (no-op)")

    from_b = from_ref.encode("utf-8")
    to_b = to_ref.encode("utf-8")

    def transform(content: bytes) -> bytes | None:
        if from_b not in content:
            return None
        return content.replace(from_b, to_b)

    return transform


# ---------- helpers ----------


def _count_changed_lines(old: bytes, new: bytes) -> tuple[int, int]:
    old_lines = old.decode("utf-8", errors="replace").splitlines()
    new_lines = new.decode("utf-8", errors="replace").splitlines()
    diff = list(difflib.ndiff(old_lines, new_lines))
    added = sum(1 for line in diff if line.startswith("+ "))
    removed = sum(1 for line in diff if line.startswith("- "))
    return added, removed


# Register on import so cli.rollback / cli.run can find the class by name
register(OP_NAME, FixCIWorkflow)
