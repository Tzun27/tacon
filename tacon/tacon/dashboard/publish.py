"""Push a rendered tacon dashboard to a target repo's `gh-pages` branch.

Why git-data API instead of `repo.update_file`/`create_file`: a publish
must be atomic. The contents API creates one commit per file, which
leaves the branch in a half-published state if any single call fails.
The git-data API (blob → tree → commit → ref) builds the entire snapshot
locally and updates the branch ref in one final call.

The published tree REPLACES the prior tree (no `base_tree`), so stale
files from a previous publish don't linger. New publishes still parent
to the previous commit when one exists, so the branch keeps an audit
trail rather than rewriting history.

Example:
    >>> gh = RateLimitedClient()
    >>> publish_to_gh_pages(gh, "myorg/dashboard", Path("./tacon-dashboard"))
    PublishResult(target_repo='myorg/dashboard', branch='gh-pages', ...)
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from github import GithubException, InputGitTreeElement, UnknownObjectException

from tacon.db import now_iso

if TYPE_CHECKING:
    from github.GitRef import GitRef
    from github.Repository import Repository

    from tacon.github_client import RateLimitedClient


# ---------- public API ----------


@dataclass(frozen=True)
class PublishResult:
    target_repo: str
    branch: str
    commit_sha: str
    files_published: int
    branch_status: str  # 'created' | 'updated'
    pages_url: str | None


class PublishError(Exception):
    """Raised for input-validation failures (bad target_repo, empty site_dir).

    PyGithub exceptions are NOT wrapped — callers want the original status
    code + message for diagnostics. Wrapping would obscure rate-limit and
    auth signals that look identical from a generic exception type.
    """


def publish_to_gh_pages(
    gh: RateLimitedClient,
    target_repo: str,
    site_dir: Path,
    *,
    branch: str = "gh-pages",
    commit_message: str | None = None,
) -> PublishResult:
    """Atomically publish `site_dir`'s contents to the branch on `target_repo`.

    Args:
        gh: RateLimitedClient with a token that has push access to target_repo.
        target_repo: ``"<owner>/<repo>"``. The DASHBOARD's repo, NOT a
            classroom repo. Common pattern: a dedicated reporting repo.
        site_dir: Local directory holding the rendered dashboard
            (typically the output of :func:`tacon.dashboard.render`).
        branch: Branch to publish to. Defaults to ``"gh-pages"``.
        commit_message: Optional override; defaults to a timestamped
            ``"tacon: publish dashboard <iso>"``.

    Returns:
        PublishResult with the new commit SHA and a guess at the Pages URL.

    Raises:
        PublishError: target_repo malformed or site_dir empty/missing.
        GithubException: any API failure (auth, network, rate limit, etc.) —
            unwrapped so the original status code reaches the caller.
    """
    _validate_target_repo(target_repo)
    files = _collect_site_files(site_dir)

    repo = gh.get_repo(target_repo)
    blob_elements = _upload_blobs(gh, repo, files)

    branch_ref, parents = _resolve_branch_ref_and_parents(gh, repo, branch)

    tree = gh.call(repo.create_git_tree, blob_elements)
    commit = gh.call(
        repo.create_git_commit,
        message=commit_message or f"tacon: publish dashboard {now_iso()}",
        tree=tree,
        parents=parents,
    )

    if branch_ref is None:
        gh.call(
            repo.create_git_ref,
            ref=f"refs/heads/{branch}",
            sha=commit.sha,
        )
        branch_status = "created"
    else:
        gh.call(branch_ref.edit, sha=commit.sha)
        branch_status = "updated"

    return PublishResult(
        target_repo=target_repo,
        branch=branch,
        commit_sha=commit.sha,
        files_published=len(files),
        branch_status=branch_status,
        pages_url=_default_pages_url(target_repo) if branch == "gh-pages" else None,
    )


# ---------- helpers ----------


def _validate_target_repo(target_repo: str) -> None:
    if "/" not in target_repo or target_repo.count("/") != 1:
        raise PublishError(
            f"target_repo must be '<owner>/<repo>', got {target_repo!r}"
        )
    owner, repo = target_repo.split("/", 1)
    if not owner or not repo:
        raise PublishError(
            f"target_repo must be '<owner>/<repo>', got {target_repo!r}"
        )


def _collect_site_files(site_dir: Path) -> list[tuple[str, bytes]]:
    """Walk site_dir and return [(rel_posix_path, file_bytes), ...].

    Skips directories and dotfiles at any level (.git, .DS_Store, etc.) —
    they have no business being on a public Pages branch.
    """
    if not site_dir.exists():
        raise PublishError(f"site_dir does not exist: {site_dir}")
    if not site_dir.is_dir():
        raise PublishError(f"site_dir is not a directory: {site_dir}")

    out: list[tuple[str, bytes]] = []
    for path in sorted(site_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(site_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        out.append((rel.as_posix(), path.read_bytes()))

    if not out:
        raise PublishError(
            f"site_dir contains no files to publish: {site_dir}. "
            "Run `tacon dashboard --out <dir>` first."
        )
    return out


def _upload_blobs(
    gh: RateLimitedClient,
    repo: Repository,
    files: list[tuple[str, bytes]],
) -> list[InputGitTreeElement]:
    """Create one blob per file and return tree elements ready for create_git_tree."""
    elements: list[InputGitTreeElement] = []
    for rel_path, content in files:
        encoded = base64.b64encode(content).decode("ascii")
        blob = gh.call(repo.create_git_blob, content=encoded, encoding="base64")
        elements.append(
            InputGitTreeElement(
                path=rel_path,
                mode="100644",
                type="blob",
                sha=blob.sha,
            )
        )
    return elements


def _resolve_branch_ref_and_parents(
    gh: RateLimitedClient,
    repo: Repository,
    branch: str,
) -> tuple[GitRef | None, list[object]]:
    """Look up the branch ref. Returns (ref_or_None, parents_list).

    Existing branch → (ref, [prior_commit]) so the new commit chains on.
    Missing branch  → (None, []) so we'll create an orphan root commit.
    """
    try:
        ref = gh.call(repo.get_git_ref, f"heads/{branch}")
    except UnknownObjectException:
        return None, []
    except GithubException as exc:
        # 404 surfaces as UnknownObjectException above; some PyGithub
        # versions still raise generic GithubException with status 404
        # for missing refs, so handle that fallback explicitly.
        if getattr(exc, "status", None) == 404:
            return None, []
        raise
    parent_sha = ref.object.sha
    parent_commit = gh.call(repo.get_git_commit, parent_sha)
    return ref, [parent_commit]


def _default_pages_url(target_repo: str) -> str:
    """Best-effort guess at the public Pages URL.

    Doesn't account for custom CNAMEs or org-level pages; the CLI
    surfaces this with "(default URL — custom domains may differ)" so
    users aren't misled.
    """
    owner, repo = target_repo.split("/", 1)
    return f"https://{owner}.github.io/{repo}/"
