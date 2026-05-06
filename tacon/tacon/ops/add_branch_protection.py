"""AddBranchProtection: read-only survey of branch protection across repos.

v0.1 ships read-only. The op fetches branch protection settings for the
target branch (default: each repo's default_branch) and renders a
status table. It writes one event per repo with status='reported' so
you have an audit trail of "what protections did we see on day X?".

Why read-only: setting branch protection requires an admin-scoped token
which most TA/maintainer tokens don't have. Surveying first lets us see
which repos are misconfigured before deciding whether to escalate to a
write op (planned for v0.2).

supports_rollback = False — there's nothing to roll back from a survey.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from github import GithubException, UnknownObjectException

from tacon import __version__
from tacon.db import insert_event, list_active_repos, now_iso, update_event_status
from tacon.github_client import classify_error
from tacon.ops import (
    ApplyResult,
    ConfirmCallback,
    Diff,
    Op,
    RepoApplyResult,
    RepoDiff,
    register,
)

if TYPE_CHECKING:
    from sqlite_utils import Database

    from tacon.github_client import RateLimitedClient


OP_NAME = "add-branch-protection"
OP_CLASS = "add_branch_protection"


class AddBranchProtection(Op):
    """Survey current branch protection across active repos in scope."""

    requires_clone = False
    supports_rollback = False

    def __init__(
        self,
        *,
        branch: str | None = None,
        assignment_id: str | None = None,
    ) -> None:
        # branch=None -> use each repo's default_branch
        self.branch = branch
        self.assignment_id = assignment_id

    @property
    def args(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "assignment_id": self.assignment_id,
            "mode": "report",
        }

    # ---------- plan ----------

    def plan(self, db: Database, gh: RateLimitedClient) -> Diff:
        diff = Diff(op_class=OP_CLASS, op_args=self.args, per_repo=[])
        for row in list_active_repos(db, assignment_id=self.assignment_id):
            target_branch = self.branch or row["default_branch"]
            repo_id = row["id"]
            student_id = row["student_id"]

            summary, detail, blocked, reason = self._inspect(gh, repo_id, target_branch)
            diff.per_repo.append(
                RepoDiff(
                    repo_id=repo_id,
                    student_id=student_id,
                    summary=summary,
                    unified_diff=detail,
                    blocked=blocked,
                    blocked_reason=reason,
                )
            )
        return diff

    def _inspect(
        self, gh: RateLimitedClient, repo_id: str, branch: str
    ) -> tuple[str, str, bool, str]:
        """Returns (summary, detail_text, blocked, blocked_reason)."""
        try:
            repo = gh.get_repo(repo_id)
            branch_obj = gh.call(repo.get_branch, branch)
        except UnknownObjectException:
            return (
                f"branch '{branch}' missing",
                f"# branch '{branch}' does not exist in {repo_id}\n",
                True,
                f"branch '{branch}' missing",
            )
        except GithubException as exc:
            err = classify_error(exc)
            return (
                f"unreachable: {err}",
                f"# inspect failed: {err} ({exc})\n",
                True,
                f"inspect failed: {err}",
            )

        is_protected = bool(getattr(branch_obj, "protected", False))
        if not is_protected:
            return ("protected: NO", "# no protection rules\n", False, "")

        try:
            protection = gh.call(branch_obj.get_protection)
        except UnknownObjectException:
            # PyGithub sometimes flags branch.protected=True but get_protection()
            # 404s when the user lacks admin scope to read it.
            return (
                "protected: yes (details restricted)",
                "# protected, but cannot read details (token lacks admin scope)\n",
                False,
                "",
            )
        except GithubException as exc:
            err = classify_error(exc)
            return (
                f"protected: yes (read failed: {err})",
                f"# protected, but get_protection failed: {err} ({exc})\n",
                False,
                "",
            )

        return _format_protection(protection)

    # ---------- apply ----------

    def apply(
        self,
        db: Database,
        gh: RateLimitedClient,
        diff: Diff,
        confirm: ConfirmCallback,
    ) -> ApplyResult:
        """Persist the read-only survey to events for audit purposes.

        confirm is honored — declining still records a 'skipped' event so
        users can later see which repos they bypassed.
        """
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

            update_event_status(
                db,
                event_id,
                status="reported",
                error_message=repo_diff.summary,  # store the summary for audit
                applied_at=now_iso(),
            )
            result.per_repo.append(
                RepoApplyResult(
                    repo_id=repo_diff.repo_id,
                    status="reported",
                    error_message=repo_diff.summary,
                )
            )

        return result


def _format_protection(protection: object) -> tuple[str, str, bool, str]:
    """Render the protection state. Returns (summary, detail, blocked, reason)."""
    bits = []
    detail_lines = ["# branch protection enabled"]

    pr_reviews = getattr(protection, "required_pull_request_reviews", None)
    if pr_reviews is not None:
        approvals = getattr(pr_reviews, "required_approving_review_count", None)
        if approvals is not None:
            bits.append(f"{approvals} approval(s)")
            detail_lines.append(f"#   required_approving_review_count: {approvals}")
        if getattr(pr_reviews, "dismiss_stale_reviews", False):
            bits.append("dismiss-stale")
            detail_lines.append("#   dismiss_stale_reviews: true")

    status_checks = getattr(protection, "required_status_checks", None)
    if status_checks is not None:
        contexts = list(getattr(status_checks, "contexts", []) or [])
        if contexts:
            bits.append(f"{len(contexts)} status check(s)")
            detail_lines.append(f"#   required_status_checks: {contexts}")
        if getattr(status_checks, "strict", False):
            bits.append("strict")
            detail_lines.append("#   strict: true")

    enforce_admins = getattr(protection, "enforce_admins", None)
    if enforce_admins and getattr(enforce_admins, "enabled", False):
        bits.append("admins enforced")
        detail_lines.append("#   enforce_admins: true")

    suffix = f" ({', '.join(bits)})" if bits else ""
    return (
        f"protected: YES{suffix}",
        "\n".join(detail_lines) + "\n",
        False,
        "",
    )


def _new_op_id() -> str:
    import uuid

    return str(uuid.uuid4())


# Register on import so cli.run can find the class by name
register(OP_NAME, AddBranchProtection)
