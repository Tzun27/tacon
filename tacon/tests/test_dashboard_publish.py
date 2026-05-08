"""Tests for `tacon.dashboard.publish.publish_to_gh_pages`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from github import GithubException, UnknownObjectException
from typer.testing import CliRunner

from tacon.cli import app
from tacon.dashboard.publish import (
    PublishError,
    PublishResult,
    publish_to_gh_pages,
)

# ---------- helpers ----------


def _site(tmp_path: Path) -> Path:
    """Build a tiny site with index, css, and a nested page."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>tacon</h1>", encoding="utf-8")
    (site / "style.css").write_text("body{color:#fff}", encoding="utf-8")
    op_dir = site / "op"
    op_dir.mkdir()
    (op_dir / "abc.html").write_text("<p>op</p>", encoding="utf-8")
    return site


def _gh_with_repo(*, branch_exists: bool, blob_shas: list[str]) -> tuple[MagicMock, MagicMock]:
    """A RateLimitedClient + Repository mock pair tuned for one publish call."""
    repo = MagicMock(name="Repository")

    blob_iter = iter(blob_shas)

    def fake_create_blob(*, content: str, encoding: str) -> MagicMock:
        b = MagicMock()
        b.sha = next(blob_iter)
        return b

    repo.create_git_blob.side_effect = fake_create_blob

    if branch_exists:
        ref = MagicMock(name="branch_ref")
        ref.object.sha = "parent-sha"
        repo.get_git_ref.return_value = ref
        parent_commit = MagicMock()
        parent_commit.sha = "parent-sha"
        repo.get_git_commit.return_value = parent_commit
    else:
        # Mimic PyGithub raising UnknownObjectException on missing refs.
        repo.get_git_ref.side_effect = UnknownObjectException(
            status=404, data={"message": "Not Found"}, headers={}
        )

    tree = MagicMock()
    tree.sha = "tree-sha"
    repo.create_git_tree.return_value = tree

    commit = MagicMock()
    commit.sha = "new-commit-sha"
    repo.create_git_commit.return_value = commit

    repo.create_git_ref.return_value = MagicMock()

    gh = MagicMock(name="RateLimitedClient")
    gh.get_repo.return_value = repo
    gh.call.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    return gh, repo


# ---------- input validation ----------


def test_publish_rejects_target_repo_without_slash(tmp_path: Path) -> None:
    site = _site(tmp_path)
    gh = MagicMock()
    with pytest.raises(PublishError, match="<owner>/<repo>"):
        publish_to_gh_pages(gh, "no-slash", site)


def test_publish_rejects_target_repo_with_two_slashes(tmp_path: Path) -> None:
    site = _site(tmp_path)
    gh = MagicMock()
    with pytest.raises(PublishError, match="<owner>/<repo>"):
        publish_to_gh_pages(gh, "a/b/c", site)


def test_publish_rejects_empty_owner_or_repo(tmp_path: Path) -> None:
    site = _site(tmp_path)
    gh = MagicMock()
    with pytest.raises(PublishError):
        publish_to_gh_pages(gh, "/repo", site)
    with pytest.raises(PublishError):
        publish_to_gh_pages(gh, "owner/", site)


def test_publish_rejects_missing_site_dir(tmp_path: Path) -> None:
    gh = MagicMock()
    with pytest.raises(PublishError, match="does not exist"):
        publish_to_gh_pages(gh, "o/r", tmp_path / "missing")


def test_publish_rejects_site_dir_that_is_a_file(tmp_path: Path) -> None:
    gh = MagicMock()
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("hi", encoding="utf-8")
    with pytest.raises(PublishError, match="not a directory"):
        publish_to_gh_pages(gh, "o/r", not_a_dir)


def test_publish_rejects_empty_site_dir(tmp_path: Path) -> None:
    site = tmp_path / "empty"
    site.mkdir()
    gh = MagicMock()
    with pytest.raises(PublishError, match="no files"):
        publish_to_gh_pages(gh, "o/r", site)


def test_publish_skips_dotfiles(tmp_path: Path) -> None:
    """Dotfiles + dot-dirs at any level shouldn't reach gh-pages."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    (site / ".DS_Store").write_text("noise", encoding="utf-8")
    git_dir = site / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

    gh, repo = _gh_with_repo(branch_exists=False, blob_shas=["b1"])
    result = publish_to_gh_pages(gh, "o/r", site)
    assert result.files_published == 1  # only index.html
    # Verify only one blob was created (the dotfile + .git/HEAD were skipped)
    assert repo.create_git_blob.call_count == 1


# ---------- happy path: branch missing (orphan commit) ----------


def test_publish_creates_branch_when_missing(tmp_path: Path) -> None:
    site = _site(tmp_path)  # 3 files
    gh, repo = _gh_with_repo(
        branch_exists=False, blob_shas=["b1", "b2", "b3"]
    )

    result = publish_to_gh_pages(gh, "myorg/dashboard", site)

    assert isinstance(result, PublishResult)
    assert result.target_repo == "myorg/dashboard"
    assert result.branch == "gh-pages"
    assert result.commit_sha == "new-commit-sha"
    assert result.files_published == 3
    assert result.branch_status == "created"
    assert result.pages_url == "https://myorg.github.io/dashboard/"

    # Orphan commit: no parents.
    _msg, kwargs = _commit_call_args(repo)
    assert kwargs["parents"] == []

    # Branch created (not edited).
    repo.create_git_ref.assert_called_once()
    args, kwargs = repo.create_git_ref.call_args
    assert kwargs.get("ref") == "refs/heads/gh-pages"
    assert kwargs.get("sha") == "new-commit-sha"


def test_publish_falls_back_to_create_when_get_git_ref_returns_404_github_exc(
    tmp_path: Path,
) -> None:
    """Some PyGithub paths raise generic GithubException(404) instead of UnknownObjectException."""
    site = _site(tmp_path)
    gh, repo = _gh_with_repo(branch_exists=False, blob_shas=["b1", "b2", "b3"])
    repo.get_git_ref.side_effect = GithubException(
        status=404, data={"message": "Not Found"}, headers={}
    )

    result = publish_to_gh_pages(gh, "o/r", site)
    assert result.branch_status == "created"


def test_publish_propagates_non_404_github_exc_on_branch_lookup(tmp_path: Path) -> None:
    site = _site(tmp_path)
    gh, repo = _gh_with_repo(branch_exists=False, blob_shas=["b1", "b2", "b3"])
    repo.get_git_ref.side_effect = GithubException(
        status=403, data={"message": "permission denied"}, headers={}
    )

    with pytest.raises(GithubException) as excinfo:
        publish_to_gh_pages(gh, "o/r", site)
    assert excinfo.value.status == 403


# ---------- happy path: branch exists (parented commit) ----------


def test_publish_updates_existing_branch(tmp_path: Path) -> None:
    site = _site(tmp_path)
    gh, repo = _gh_with_repo(branch_exists=True, blob_shas=["b1", "b2", "b3"])

    result = publish_to_gh_pages(gh, "myorg/dashboard", site)

    assert result.branch_status == "updated"

    # Commit chains to the prior tip.
    _msg, kwargs = _commit_call_args(repo)
    parents = kwargs["parents"]
    assert len(parents) == 1
    assert parents[0].sha == "parent-sha"

    # Branch ref edited (not created).
    repo.create_git_ref.assert_not_called()
    repo.get_git_ref.return_value.edit.assert_called_once()
    edit_kwargs = repo.get_git_ref.return_value.edit.call_args.kwargs
    edit_args = repo.get_git_ref.return_value.edit.call_args.args
    sha_arg = edit_kwargs.get("sha", edit_args[0] if edit_args else None)
    assert sha_arg == "new-commit-sha"


def test_publish_uploads_one_blob_per_file_and_builds_tree(tmp_path: Path) -> None:
    site = _site(tmp_path)  # index.html, style.css, op/abc.html
    gh, repo = _gh_with_repo(branch_exists=False, blob_shas=["bl1", "bl2", "bl3"])

    publish_to_gh_pages(gh, "o/r", site)

    # 3 blobs uploaded, base64-encoded
    assert repo.create_git_blob.call_count == 3
    for call in repo.create_git_blob.call_args_list:
        assert call.kwargs["encoding"] == "base64"

    # Tree built from those blob shas with subdirectory paths preserved
    repo.create_git_tree.assert_called_once()
    tree_elements = repo.create_git_tree.call_args.args[0]
    paths = {_tree_el_path(el) for el in tree_elements}
    assert paths == {"index.html", "style.css", "op/abc.html"}


def test_publish_default_commit_message_is_timestamped(tmp_path: Path) -> None:
    site = _site(tmp_path)
    gh, repo = _gh_with_repo(branch_exists=False, blob_shas=["b1", "b2", "b3"])

    publish_to_gh_pages(gh, "o/r", site)

    msg = repo.create_git_commit.call_args.kwargs["message"]
    assert msg.startswith("tacon: publish dashboard ")


def test_publish_respects_custom_commit_message(tmp_path: Path) -> None:
    site = _site(tmp_path)
    gh, repo = _gh_with_repo(branch_exists=False, blob_shas=["b1", "b2", "b3"])

    publish_to_gh_pages(
        gh, "o/r", site, commit_message="custom: weekly snapshot"
    )

    msg = repo.create_git_commit.call_args.kwargs["message"]
    assert msg == "custom: weekly snapshot"


def test_publish_to_non_default_branch_omits_pages_url(tmp_path: Path) -> None:
    """pages_url is only meaningful for `gh-pages`. Other branches: skip it."""
    site = _site(tmp_path)
    gh, repo = _gh_with_repo(branch_exists=False, blob_shas=["b1", "b2", "b3"])

    result = publish_to_gh_pages(gh, "o/r", site, branch="docs-deploy")
    assert result.branch == "docs-deploy"
    assert result.pages_url is None
    assert repo.create_git_ref.call_args.kwargs["ref"] == "refs/heads/docs-deploy"


# ---------- CLI integration ----------


def _patch_publish(monkeypatch, **result_kwargs) -> MagicMock:
    """Stub `publish_to_gh_pages` with a MagicMock that returns a fake PublishResult."""
    captured = MagicMock()

    def fake_publish(gh, target_repo, site_dir, **kwargs):
        captured(gh, target_repo, site_dir, **kwargs)
        return PublishResult(
            target_repo=target_repo,
            branch=kwargs.get("branch", "gh-pages"),
            commit_sha=result_kwargs.get("commit_sha", "deadbeefcafe"),
            files_published=result_kwargs.get("files_published", 5),
            branch_status=result_kwargs.get("branch_status", "updated"),
            pages_url=result_kwargs.get("pages_url", "https://o.github.io/r/"),
        )

    # Patch where the CLI imports it (cli.py does a local import at runtime).
    import tacon.dashboard as dash
    import tacon.dashboard.publish as pub

    monkeypatch.setattr(dash, "publish_to_gh_pages", fake_publish)
    monkeypatch.setattr(pub, "publish_to_gh_pages", fake_publish)
    return captured


def _populate_db(db_path: Path) -> None:
    """Minimal DB so render() has something to write."""
    from tacon.db import (
        insert_event,
        open_db,
        update_event_status,
        upsert_assignment,
        upsert_repo,
        upsert_student,
    )

    db = open_db(db_path)
    upsert_assignment(
        db,
        id="asn-1",
        classroom_id="cls-1",
        title="HW3",
        slug="hw3",
        starter_repo=None,
        created_at="2026-05-01T00:00:00Z",
    )
    sid = upsert_student(db, username="alice")
    upsert_repo(db, id="cs101/alice-hw3", assignment_id="asn-1", student_id=sid)
    eid = insert_event(
        db,
        op_id="op-1",
        op_class="add_file",
        op_args_json='{"path": "X"}',
        tacon_version="0.0.1",
        repo_id="cs101/alice-hw3",
        student_id=sid,
        status="planned",
    )
    update_event_status(db, eid, status="applied", commit_sha="abc")


def _patch_rate_limited_client(monkeypatch) -> MagicMock:
    """Stub RateLimitedClient so the CLI doesn't try to load a real token."""
    fake_gh = MagicMock(name="RateLimitedClient")
    monkeypatch.setattr(
        "tacon.cli.RateLimitedClient", lambda *a, **kw: fake_gh
    )
    return fake_gh


def test_dashboard_cli_publish_calls_publish_helper(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "tacon.db"
    _populate_db(db_path)
    out_dir = tmp_path / "site"

    captured = _patch_publish(monkeypatch)
    fake_gh = _patch_rate_limited_client(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "dashboard",
            "--out", str(out_dir),
            "--db", str(db_path),
            "--publish", "myorg/dashboard",
        ],
    )

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    captured.assert_called_once()
    args, kwargs = captured.call_args
    assert args[0] is fake_gh
    assert args[1] == "myorg/dashboard"
    assert args[2] == out_dir
    assert kwargs["branch"] == "gh-pages"
    assert kwargs.get("commit_message") is None


def test_dashboard_cli_publish_branch_and_message_forwarded(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "tacon.db"
    _populate_db(db_path)
    out_dir = tmp_path / "site"

    captured = _patch_publish(monkeypatch)
    _patch_rate_limited_client(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "dashboard",
            "--out", str(out_dir),
            "--db", str(db_path),
            "--publish", "myorg/dashboard",
            "--publish-branch", "deploy",
            "--publish-message", "weekly: 2026-W19",
        ],
    )

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    args, kwargs = captured.call_args
    assert kwargs["branch"] == "deploy"
    assert kwargs["commit_message"] == "weekly: 2026-W19"


def test_dashboard_cli_publish_error_exits_two(tmp_path: Path, monkeypatch) -> None:
    """PublishError -> exit 2 with the message on stderr."""
    db_path = tmp_path / "tacon.db"
    _populate_db(db_path)
    out_dir = tmp_path / "site"

    def boom(*a, **kw):
        raise PublishError("target_repo must be '<owner>/<repo>', got 'bad'")

    import tacon.dashboard as dash
    import tacon.dashboard.publish as pub

    monkeypatch.setattr(dash, "publish_to_gh_pages", boom)
    monkeypatch.setattr(pub, "publish_to_gh_pages", boom)
    _patch_rate_limited_client(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "dashboard",
            "--out", str(out_dir),
            "--db", str(db_path),
            "--publish", "bad",
        ],
    )

    assert result.exit_code == 2


def test_dashboard_cli_no_publish_flag_does_not_construct_client(
    tmp_path: Path, monkeypatch
) -> None:
    """If --publish is absent, no RateLimitedClient should be instantiated.

    Regression guard: the render-only path used to be free of any GitHub
    token requirement. We don't want a refactor to silently regress that.
    """
    db_path = tmp_path / "tacon.db"
    _populate_db(db_path)
    out_dir = tmp_path / "site"

    constructed = MagicMock()
    monkeypatch.setattr("tacon.cli.RateLimitedClient", constructed)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["dashboard", "--out", str(out_dir), "--db", str(db_path)],
    )

    assert result.exit_code == 0
    constructed.assert_not_called()


# ---------- helpers for tree-element introspection ----------


def _tree_el_path(el: object) -> str:
    """Pull the path back out of a PyGithub InputGitTreeElement.

    PyGithub 2.x exposes `_identity` as a dict {'path', 'mode', 'type', 'sha'};
    older versions had a private mangled `_InputGitTreeElement__path` attr.
    """
    identity = getattr(el, "_identity", None)
    if isinstance(identity, dict) and "path" in identity:
        return str(identity["path"])
    for name in ("_InputGitTreeElement__path", "path", "_path"):
        v = getattr(el, name, None)
        if isinstance(v, str):
            return v
    raise AssertionError(f"could not extract path from {el!r}")


def _commit_call_args(repo: MagicMock) -> tuple[str, dict[str, object]]:
    """Pull (message, kwargs) out of repo.create_git_commit.call_args."""
    repo.create_git_commit.assert_called_once()
    kwargs = dict(repo.create_git_commit.call_args.kwargs)
    msg = kwargs["message"]
    return msg, kwargs
