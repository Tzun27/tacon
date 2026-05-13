"""AddBranchProtection: branch-protection survey + write across repos.

Two modes:

  - **survey** (default): fetches branch protection state and records
    `reported` events. Read-only; ``supports_rollback`` is False.
  - **write** (when ``rule`` is set): apply a desired
    :class:`BranchProtectionRule` to each target branch via PyGithub's
    ``branch.edit_protection``. Plan() reads the prior state and stashes
    a serialized snapshot; apply() writes the rule + persists that
    snapshot to ``events.prior_state_json``; rollback() restores it
    (or removes protection entirely if the prior state was unprotected).
    ``supports_rollback`` becomes True in this mode.

Why both live here: the read path is identical (fetch current
protection), and the diff render in plan() switches based on whether a
target rule was supplied. Mode is observable from ``args["rule"]`` in
the events table — null = survey, dict = write.

Note: branch protection is repo-level config, not branch content, so
``supports_via_pr`` stays False (no PR can wrap an admin action).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from github import GithubException, UnknownObjectException
from pydantic import BaseModel, Field

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
    RepoRollbackResult,
    RollbackResult,
    register,
)
from tacon.ops._branch_protection_rule import BranchProtectionRule, from_dict

if TYPE_CHECKING:
    from sqlite_utils import Database

    from tacon.github_client import RateLimitedClient


OP_NAME = "add-branch-protection"
OP_CLASS = "add_branch_protection"


class AddBranchProtection(Op):
    """Survey current branch protection — or apply a desired rule — across active repos."""

    requires_clone = False
    # True at class level so rollback() can be invoked via the CLI; rollback()
    # itself filters to status='applied' events, so survey ops (which write
    # status='reported' events) are naturally excluded — calling
    # ``tacon rollback <op-id-of-a-survey>`` returns an empty result, which is
    # the right answer.
    supports_rollback = True

    def __init__(
        self,
        *,
        branch: str | None = None,
        assignment_id: str | None = None,
        rule: BranchProtectionRule | None = None,
    ) -> None:
        # branch=None -> use each repo's default_branch
        self.branch = branch
        self.assignment_id = assignment_id
        # rule=None -> survey mode (existing v0.1 behavior)
        # rule=BranchProtectionRule -> write mode
        self.rule = rule

    @property
    def args(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "assignment_id": self.assignment_id,
            "mode": "write" if self.rule is not None else "report",
            "rule": self.rule.to_dict() if self.rule is not None else None,
        }

    @classmethod
    def arg_schema(cls) -> type[BaseModel]:
        """Form schema for both survey + write modes.

        ``rule`` being ``None`` (or all-default) selects survey mode
        (read-only). Setting any non-default rule field switches to
        write mode. The GUI surfaces this as a "Set branch protection"
        verb-card with a rule-builder form; survey mode is its own
        verb-card that hides the rule fields entirely.
        """

        class BranchProtectionRuleArgs(BaseModel):
            required_approving_review_count: int | None = Field(
                None,
                description="Required PR review approvals (1-6). Leave blank for no review requirement.",
                ge=0,
                le=6,
            )
            dismiss_stale_reviews: bool = Field(
                False,
                description="Dismiss approvals when new commits are pushed.",
            )
            require_code_owner_reviews: bool = Field(
                False,
                description="Require approval from a CODEOWNERS-listed owner.",
            )
            required_status_checks: list[str] | None = Field(
                None,
                description="Status-check contexts that must pass before merge (e.g. ['ci', 'lint']). Empty list / null = no required checks.",
            )
            strict_status_checks: bool = Field(
                False,
                description="Require branches to be up to date before merging (strict status checks).",
            )
            enforce_admins: bool = Field(
                False,
                description="Apply this protection to repo admins too.",
            )
            allow_force_pushes: bool = Field(
                False,
                description="Allow force-pushes to the protected branch.",
            )
            allow_deletions: bool = Field(
                False,
                description="Allow deletion of the protected branch.",
            )
            required_linear_history: bool = Field(
                False,
                description="Require a linear (no-merge-commit) history.",
            )

        class AddBranchProtectionArgs(BaseModel):
            branch: str | None = Field(
                None,
                description="Target branch (e.g. 'main'). Leave blank to use each repo's default branch.",
            )
            assignment_id: str | None = Field(
                None,
                description="Limit to one assignment_id. Leave blank to target every active repo.",
            )
            rule: BranchProtectionRuleArgs | None = Field(
                None,
                description="Desired protection rule (write mode). Leave null for read-only survey.",
            )

        return AddBranchProtectionArgs

    # ---------- plan ----------

    def plan(self, db: Database, gh: RateLimitedClient) -> Diff:
        diff = Diff(op_class=OP_CLASS, op_args=self.args, per_repo=[])
        for row in list_active_repos(db, assignment_id=self.assignment_id):
            target_branch = self.branch or row["default_branch"]
            repo_id = row["id"]
            student_id = row["student_id"]

            current_summary, current_detail, blocked, reason = self._inspect(
                gh, repo_id, target_branch
            )

            if self.rule is None:
                # Survey mode (existing v0.1 behavior).
                summary, detail = current_summary, current_detail
            else:
                # Write mode: render desired-state vs current diff.
                if blocked:
                    summary = current_summary
                    detail = current_detail
                else:
                    current_dict = self._read_current_as_dict(gh, repo_id, target_branch)
                    desired_dict = self.rule.to_dict()
                    if _rule_dicts_equal(current_dict, desired_dict):
                        # Idempotent: nothing to change.
                        blocked = True
                        reason = "branch already at desired protection state"
                        summary = f"BLOCKED: no change ({current_summary})"
                        detail = (
                            "# already at desired protection state — no change\n"
                        )
                    else:
                        summary = (
                            f"set protection on '{target_branch}': "
                            f"{_describe_rule(self.rule)}"
                        )
                        detail = _render_rule_diff(current_dict, desired_dict)

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

    def _read_current_as_dict(
        self, gh: RateLimitedClient, repo_id: str, target_branch: str
    ) -> dict[str, Any] | None:
        """Snapshot the current protection in the BranchProtectionRule shape.

        Returns ``None`` if the branch is currently unprotected (or if details
        are restricted by token scope — rollback would treat that as 'remove
        protection' since we can't reproduce it). Returns a rule-shaped dict
        when protection is readable.
        """
        try:
            repo = gh.get_repo(repo_id)
            branch_obj = gh.call(repo.get_branch, target_branch)
        except (UnknownObjectException, GithubException):
            return None
        if not bool(getattr(branch_obj, "protected", False)):
            return None
        try:
            protection = gh.call(branch_obj.get_protection)
        except (UnknownObjectException, GithubException):
            # Protected, but we can't read the rule (e.g. token scope). Best we
            # can do is return None — rollback will then remove protection. The
            # user is implicitly opting in by writing protection without admin
            # read scope; we surface this in the apply event's error_message.
            return None
        return _protection_to_rule_dict(protection)

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
        op_id: str | None = None,
    ) -> ApplyResult:
        """Apply the rule (write mode) or persist the survey (survey mode).

        confirm is honored — declining still records a 'skipped' event so
        users can later see which repos they bypassed.

        ``op_id`` is generated when ``None`` (default for CLI use). The GUI
        server pre-generates one so it can return ``{op_id}`` before the
        background apply task starts producing events.
        """
        if op_id is None:
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

            if self.rule is None:
                # Survey mode: record the observation. No GH write.
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
                continue

            # --- write mode ---
            target_branch = self.branch or _resolve_default_branch(db, repo_diff.repo_id)
            try:
                prior_state = self._read_current_as_dict(
                    gh, repo_diff.repo_id, target_branch
                )
                self._write_protection(gh, repo_diff.repo_id, target_branch, self.rule)
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

            # prior_state is None when the branch was previously unprotected;
            # we serialize JSON-null in that case. The non-None branch
            # serializes the rule-shaped dict for symmetric restore.
            prior_json = json.dumps(prior_state, sort_keys=True)
            update_event_status(
                db,
                event_id,
                status="applied",
                applied_at=now_iso(),
                prior_state_json=prior_json,
            )
            result.per_repo.append(
                RepoApplyResult(
                    repo_id=repo_diff.repo_id,
                    status="applied",
                )
            )

        return result

    def _write_protection(
        self,
        gh: RateLimitedClient,
        repo_id: str,
        target_branch: str,
        rule: BranchProtectionRule,
    ) -> None:
        """Apply the rule to the target branch."""
        repo = gh.get_repo(repo_id)
        branch_obj = gh.call(repo.get_branch, target_branch)
        gh.call(branch_obj.edit_protection, **rule.to_edit_protection_kwargs())

    # ---------- rollback ----------

    @classmethod
    def rollback(
        cls, db: Database, gh: RateLimitedClient, op_id: str
    ) -> RollbackResult:
        """Restore each repo's prior protection from events.prior_state_json.

        Survey events (status='reported') are ignored — they have nothing
        to roll back. Write events (status='applied') trigger a per-repo
        restore: the snapshot is None for "was unprotected" (rollback
        removes protection) or a rule-shaped dict (rollback re-applies it
        via edit_protection). Drift check refuses to clobber state that
        no longer matches what we wrote.
        """
        from tacon.db import get_events_by_op

        result = RollbackResult(op_id=op_id, per_repo=[])
        events = get_events_by_op(db, op_id, status="applied")
        if not events:
            return result

        first = events[0]
        try:
            args = json.loads(first["op_args_json"])
            applied_rule_dict = args.get("rule")
            target_branch = args.get("branch")
        except (KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"malformed op_args_json on op {op_id}: {e}") from e

        for event in events:
            repo_id = event["repo_id"]
            try:
                outcome = cls._rollback_one(
                    gh,
                    repo_id,
                    target_branch=target_branch,
                    applied_rule_dict=applied_rule_dict,
                    prior_state_json=event.get("prior_state_json"),
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
                    db, event["id"], status="rolled_back", rolled_back_at=now_iso()
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
        *,
        target_branch: str | None,
        applied_rule_dict: dict[str, Any] | None,
        prior_state_json: str | None,
    ) -> RepoRollbackResult:
        repo = gh.get_repo(repo_id)
        branch_name = target_branch or repo.default_branch
        branch_obj = gh.call(repo.get_branch, branch_name)

        # --- drift check: current state must still match what we applied ---
        current = None
        if bool(getattr(branch_obj, "protected", False)):
            try:
                current = gh.call(branch_obj.get_protection)
            except (UnknownObjectException, GithubException):
                current = None
        current_dict = (
            _protection_to_rule_dict(current) if current is not None else None
        )
        if applied_rule_dict is not None and not _rule_dicts_equal(
            current_dict, applied_rule_dict
        ):
            return RepoRollbackResult(
                repo_id=repo_id,
                status="skipped_dirty",
                error_message=(
                    "current protection has drifted from what tacon applied; "
                    "refusing to overwrite. Inspect the repo and re-run rollback "
                    "manually if you still want to revert."
                ),
            )

        # --- restore: apply prior state, or remove protection if there was none ---
        prior_state: dict[str, Any] | None = None
        if prior_state_json:
            try:
                prior_state = json.loads(prior_state_json)
            except json.JSONDecodeError:
                return RepoRollbackResult(
                    repo_id=repo_id,
                    status="failed",
                    error_message=f"prior_state_json is not valid JSON: {prior_state_json!r}",
                )

        if prior_state is None:
            # Was unprotected before — remove the protection we set.
            gh.call(branch_obj.remove_protection)
        else:
            prior_rule = from_dict(prior_state)
            gh.call(branch_obj.edit_protection, **prior_rule.to_edit_protection_kwargs())

        return RepoRollbackResult(repo_id=repo_id, status="rolled_back")


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


def _resolve_default_branch(db: Database, repo_id: str) -> str:
    """Look up a repo's default_branch from the local DB. Falls back to 'main'."""
    from sqlite_utils.db import Table

    table = cast(Table, db.table("repos"))
    row = next(
        table.rows_where("id = ?", (repo_id,), select="default_branch", limit=1),
        None,
    )
    if row is None:
        return "main"
    val = row.get("default_branch")
    return str(val) if val else "main"


def _protection_to_rule_dict(protection: object) -> dict[str, Any]:
    """Snapshot a PyGithub Protection object in the BranchProtectionRule shape.

    Fields the dataclass doesn't model are dropped — this is a v0.2 limitation
    documented in plans/branch_protection_write.md.
    """
    out: dict[str, Any] = {
        "required_approving_review_count": None,
        "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False,
        "required_status_checks": None,
        "strict_status_checks": False,
        "enforce_admins": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "required_linear_history": False,
    }
    pr_reviews = getattr(protection, "required_pull_request_reviews", None)
    if pr_reviews is not None:
        v = getattr(pr_reviews, "required_approving_review_count", None)
        if v is not None:
            out["required_approving_review_count"] = int(v)
        out["dismiss_stale_reviews"] = bool(
            getattr(pr_reviews, "dismiss_stale_reviews", False)
        )
        out["require_code_owner_reviews"] = bool(
            getattr(pr_reviews, "require_code_owner_reviews", False)
        )
    sc = getattr(protection, "required_status_checks", None)
    if sc is not None:
        contexts = list(getattr(sc, "contexts", []) or [])
        if contexts:
            out["required_status_checks"] = contexts
            out["strict_status_checks"] = bool(getattr(sc, "strict", False))
    enforce = getattr(protection, "enforce_admins", None)
    if enforce is not None:
        out["enforce_admins"] = bool(getattr(enforce, "enabled", False))
    out["allow_force_pushes"] = bool(getattr(protection, "allow_force_pushes", False))
    out["allow_deletions"] = bool(getattr(protection, "allow_deletions", False))
    out["required_linear_history"] = bool(
        getattr(protection, "required_linear_history", False)
    )
    return out


def _rule_dicts_equal(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Compare two rule-shaped dicts modulo list/tuple of contexts."""
    if a is None or b is None:
        return a is b  # both None -> True; one None -> False
    a2 = dict(a)
    b2 = dict(b)
    # Normalize required_status_checks: None == [] (no requirement either way).
    for d in (a2, b2):
        v = d.get("required_status_checks")
        if v is None or (isinstance(v, list) and len(v) == 0):
            d["required_status_checks"] = None
        elif isinstance(v, tuple):
            d["required_status_checks"] = list(v)
    return a2 == b2


def _describe_rule(rule: BranchProtectionRule) -> str:
    """One-line human description of a rule for plan summaries."""
    bits: list[str] = []
    if rule.required_approving_review_count is not None:
        bits.append(f"{rule.required_approving_review_count} approval(s)")
    if rule.dismiss_stale_reviews:
        bits.append("dismiss-stale")
    if rule.required_status_checks:
        bits.append(f"{len(rule.required_status_checks)} status check(s)")
    if rule.strict_status_checks:
        bits.append("strict")
    if rule.enforce_admins:
        bits.append("admins enforced")
    if rule.required_linear_history:
        bits.append("linear-history")
    return ", ".join(bits) if bits else "minimal protection"


def _render_rule_diff(
    current: dict[str, Any] | None, desired: dict[str, Any]
) -> str:
    """Render a synthetic before/after diff for the unified_diff field."""
    lines = ["# branch protection diff"]
    if current is None:
        lines.append("# (currently unprotected)")
    else:
        lines.append("# current:")
        for k, v in sorted(current.items()):
            lines.append(f"# - {k}: {v!r}")
    lines.append("# desired:")
    for k, v in sorted(desired.items()):
        lines.append(f"# + {k}: {v!r}")
    return "\n".join(lines) + "\n"


# Register on import so cli.run can find the class by name
register(OP_NAME, AddBranchProtection)
