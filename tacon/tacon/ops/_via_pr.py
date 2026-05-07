"""Shared helpers for `--via-pr` mode.

The branch + PR dance is op-agnostic at its boundaries (create branch,
open PR, close PR, delete branch). This module owns it. Each op's
apply()/rollback() calls these helpers; the file-write itself stays in
the op (just with a `branch=` kwarg threaded through PyGithub).

Why a helper module instead of a wrapper class around `Op`: AddCIWorkflow
subclasses AddFile and inherits its apply path. A wrapper would have to
intercept the parent's apply() while staying compatible with subclass
overrides, which is fragile. Plain helpers + a `via_pr` flag on the op
keep each op self-contained.

PyGithub specifics worth knowing while reading this code:
- `repo.create_git_ref(ref="refs/heads/<name>", sha=<sha>)` — note the
  "refs/" prefix.
- `repo.get_git_ref(ref="heads/<name>")` — note the LACK of "refs/"
  prefix here. Yes, PyGithub is asymmetric.
- `repo.create_pull(...)` raises `GithubException(422)` when an open PR
  for the same head already exists; we recover by listing.
- `pull.edit(state="closed")` is idempotent on already-closed PRs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from github import GithubException, UnknownObjectException

if TYPE_CHECKING:
    from github.PullRequest import PullRequest
    from github.Repository import Repository

    from tacon.github_client import RateLimitedClient


# ---------- branch naming ----------


def via_pr_branch_name(op_class: str, op_id: str) -> str:
    """Single source of truth for the `--via-pr` branch name format.

    Shape: ``tacon/<op-class-kebab>-<8-hex-prefix>``. Example:
    ``tacon/add-file-bc247dc1``. The op-class is kebab-cased (matching
    the CLI registry name); the op_id prefix is the first 8 hex chars
    of the UUIDv4, which is unique enough across the ~200 repos in a
    single classroom op.
    """
    op_class_kebab = op_class.replace("_", "-")
    prefix = op_id.replace("-", "")[:8]
    return f"tacon/{op_class_kebab}-{prefix}"


# ---------- ensure_branch ----------


class BranchConflictError(Exception):
    """A branch with the target name already exists pointing at a different SHA."""

    def __init__(self, branch: str, existing_sha: str, expected_sha: str) -> None:
        super().__init__(
            f"branch {branch!r} already exists at {existing_sha} "
            f"(expected {expected_sha}); refusing to overwrite"
        )
        self.branch = branch
        self.existing_sha = existing_sha
        self.expected_sha = expected_sha


def ensure_branch(
    gh: RateLimitedClient,
    repo: Repository,
    branch_name: str,
    base_sha: str,
) -> str:
    """Create the branch at base_sha, or detect-and-no-op an identical existing one.

    Returns:
        ``"created"`` if we just created the branch; ``"exists_same"`` if
        the branch already existed pointing at base_sha (idempotent re-apply).

    Raises:
        BranchConflictError: branch exists but points at a different SHA.
            Caller should treat as a per-repo skipped_dirty.
        GithubException: any other failure (auth, network, etc.). Caller
            handles via the op's existing classify_error path.
    """
    try:
        gh.call(
            repo.create_git_ref,
            ref=f"refs/heads/{branch_name}",
            sha=base_sha,
        )
        return "created"
    except GithubException as exc:
        if not _is_ref_already_exists(exc):
            raise
    # Branch exists; compare SHAs to decide between idempotent-skip and conflict.
    existing_sha = _get_ref_sha(gh, repo, branch_name)
    if existing_sha == base_sha:
        return "exists_same"
    raise BranchConflictError(branch_name, existing_sha, base_sha)


def _is_ref_already_exists(exc: GithubException) -> bool:
    """GitHub returns 422 with a specific message when a ref already exists."""
    if getattr(exc, "status", None) != 422:
        return False
    data = getattr(exc, "data", {}) or {}
    message = (data.get("message") or "").lower() if isinstance(data, dict) else ""
    return "reference already exists" in message


def _get_ref_sha(gh: RateLimitedClient, repo: Repository, branch_name: str) -> str:
    ref = gh.call(repo.get_git_ref, f"heads/{branch_name}")
    sha: str = ref.object.sha
    return sha


# ---------- open_or_find_pr ----------


def open_or_find_pr(
    gh: RateLimitedClient,
    repo: Repository,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
) -> int:
    """Open a PR head→base, or detect an existing open PR for the same head.

    Idempotent: if `repo.create_pull` raises 422 because a PR already
    exists for our head, list open PRs scoped to that head and return
    the existing one's number. PyGithub's `get_pulls(head=...)` accepts
    either the bare branch or "owner:branch"; we use bare-branch since
    we're operating on the branch within the same repo.
    """
    try:
        pr = gh.call(
            repo.create_pull,
            title=title,
            body=body,
            base=base,
            head=branch,
        )
        return int(pr.number)
    except GithubException as exc:
        if not _is_pull_already_exists(exc):
            raise
    # Find the existing open PR with our head branch.
    pulls = gh.call(repo.get_pulls, state="open", head=branch)
    for existing in pulls:
        if getattr(existing, "head", None) and existing.head.ref == branch:
            return int(existing.number)
    # Should not reach here: the 422 said one exists, but we couldn't find it.
    raise RuntimeError(
        f"create_pull reported a PR exists for head={branch!r} but "
        "get_pulls did not return it"
    )


def _is_pull_already_exists(exc: GithubException) -> bool:
    """422 with the 'A pull request already exists' / 'pull_request_already_exists' marker."""
    if getattr(exc, "status", None) != 422:
        return False
    data = getattr(exc, "data", {}) or {}
    if not isinstance(data, dict):
        return False
    message = (data.get("message") or "").lower()
    if "pull request already exists" in message:
        return True
    errors = data.get("errors") or []
    if isinstance(errors, list):
        for e in errors:
            if isinstance(e, dict) and e.get("message", "").lower().startswith(
                "a pull request already exists"
            ):
                return True
    return False


# ---------- close_pr_and_delete_branch (rollback) ----------


@dataclass
class RollbackOutcome:
    """Result of `close_pr_and_delete_branch` for one repo."""

    status: str  # 'rolled_back' | 'skipped_dirty' | 'failed'
    pr_state: str  # 'open' | 'closed' | 'merged' | 'not_found'
    branch_deleted: bool
    note: str = ""


def close_pr_and_delete_branch(
    gh: RateLimitedClient,
    repo: Repository,
    *,
    pr_number: int,
    branch: str,
) -> RollbackOutcome:
    """Reverse a `--via-pr` apply: close the PR, then delete the branch.

    State table (matches via_pr.md plan):

    | PR state    | branch state     | action                      | status         |
    |-------------|------------------|-----------------------------|----------------|
    | open        | exists           | close + delete              | rolled_back    |
    | open        | missing          | close                       | rolled_back    |
    | closed      | exists           | delete                      | rolled_back    |
    | closed      | missing          | no-op                       | rolled_back    |
    | merged      | (any)            | refuse                      | skipped_dirty  |
    | not_found   | (any)            | best-effort delete branch   | rolled_back    |

    Branch-delete failures are logged in `note` but do NOT downgrade
    rolled_back to failed — the PR closure was the rollback's main
    payload.
    """
    pr = _get_pull(gh, repo, pr_number)
    if pr is None:
        # PR vanished. Best-effort branch cleanup; treat the rollback as done.
        deleted = _delete_branch_best_effort(gh, repo, branch)
        return RollbackOutcome(
            status="rolled_back",
            pr_state="not_found",
            branch_deleted=deleted,
            note=f"PR #{pr_number} not found",
        )

    if getattr(pr, "merged", False):
        merge_sha = getattr(pr, "merge_commit_sha", None) or "<unknown>"
        return RollbackOutcome(
            status="skipped_dirty",
            pr_state="merged",
            branch_deleted=False,
            note=(
                f"PR #{pr_number} already merged at {merge_sha[:8]}; "
                "revert manually with `git revert` or open a counter-PR"
            ),
        )

    pr_state = "open" if pr.state == "open" else "closed"
    if pr_state == "open":
        gh.call(pr.edit, state="closed")
    deleted = _delete_branch_best_effort(gh, repo, branch)
    return RollbackOutcome(
        status="rolled_back",
        pr_state=pr_state,
        branch_deleted=deleted,
        note="" if deleted else f"branch {branch!r} could not be deleted (left in place)",
    )


def _get_pull(
    gh: RateLimitedClient, repo: Repository, pr_number: int
) -> PullRequest | None:
    try:
        pr: PullRequest = gh.call(repo.get_pull, pr_number)
        return pr
    except UnknownObjectException:
        return None


def _delete_branch_best_effort(
    gh: RateLimitedClient, repo: Repository, branch: str
) -> bool:
    """Delete `refs/heads/<branch>`. Treat 404 (already gone) as success.

    Other failures (e.g. token lacks delete-branch permission) return
    False — the caller surfaces this as a soft note, not a hard failure,
    because the PR closure was the rollback's main payload.
    """
    try:
        ref = gh.call(repo.get_git_ref, f"heads/{branch}")
    except UnknownObjectException:
        return True  # already absent
    except GithubException:
        return False
    try:
        gh.call(ref.delete)
        return True
    except GithubException:
        return False
