"""Integration tests for the Typer CLI.

These tests mock the RateLimitedClient at tacon.cli's import boundary so
the CLI orchestration paths (run/rollback/resume + printers + confirm
callback) get real coverage without hitting the GitHub API.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from github import GithubException, UnknownObjectException
from typer.testing import CliRunner

from tacon.cli import _make_confirm, app
from tacon.db import open_db, upsert_assignment, upsert_repo, upsert_student
from tacon.ops import RepoDiff

runner = CliRunner()


# ---------- fixtures ----------


@pytest.fixture
def seeded_db_path(tmp_path: Path) -> Path:
    """A tmp tacon DB with one assignment + 3 student repos."""
    db_path = tmp_path / "tacon.db"
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
    for user in ("alice", "bob", "carol"):
        sid = upsert_student(db, username=user)
        upsert_repo(db, id=f"cs101/{user}-hw3", assignment_id="asn-1", student_id=sid)
    return db_path


@pytest.fixture
def fake_repo() -> MagicMock:
    repo = MagicMock(name="Repository")
    repo.default_branch = "main"
    return repo


@pytest.fixture
def fake_gh(fake_repo: MagicMock) -> MagicMock:
    """Mocks tacon.cli.RateLimitedClient so the CLI never hits real GitHub."""
    gh = MagicMock(name="RateLimitedClient")
    gh.get_repo.return_value = fake_repo
    gh.call.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    return gh


def _content_file(sha: str = "blob-1", body: bytes = b"hello\n") -> MagicMock:
    cf = MagicMock(name="ContentFile")
    cf.sha = sha
    cf.content = base64.b64encode(body).decode("ascii")
    return cf


def _commit(sha: str = "commit-1") -> MagicMock:
    c = MagicMock(name="Commit")
    c.sha = sha
    return c


def _missing() -> UnknownObjectException:
    return UnknownObjectException(404, {"message": "Not Found"}, {})


# ---------- existing smoke tests ----------


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "tacon" in result.stdout


def test_run_unknown_op_exits_2() -> None:
    result = runner.invoke(app, ["run", "delete-everything"])
    assert result.exit_code == 2


def test_run_add_file_missing_args_exits_2() -> None:
    result = runner.invoke(app, ["run", "add-file"])
    assert result.exit_code == 2


def test_ui_command_is_stub() -> None:
    result = runner.invoke(app, ["ui"])
    assert result.exit_code == 2
    output = (result.stdout or "") + (result.stderr or "")
    assert "not implemented" in output


def test_dashboard_command_is_stub() -> None:
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 2


@patch("tacon.cli.discover_via_csv")
@patch("tacon.cli.persist_discovered")
def test_sync_from_csv(mock_persist, mock_discover, tmp_path: Path) -> None:
    mock_discover.return_value = []
    db_path = tmp_path / "test.db"
    csv_path = tmp_path / "roster.csv"
    csv_path.write_text("assignment_slug,student_username,repo_url\n")
    result = runner.invoke(app, ["sync", "--from-csv", str(csv_path), "--db", str(db_path)])
    assert result.exit_code == 0
    mock_discover.assert_called_once()


def test_sync_no_classroom_no_csv_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["sync", "--db", str(tmp_path / "test.db")])
    assert result.exit_code == 2


# ---------- sync error paths ----------


@patch("tacon.cli.discover_via_gh_classroom")
def test_sync_gh_classroom_failure_prints_csv_hint(
    mock_discover: MagicMock, tmp_path: Path
) -> None:
    from tacon.classroom import GhClassroomError

    mock_discover.side_effect = GhClassroomError("gh extension not installed")
    db_path = tmp_path / "test.db"
    result = runner.invoke(app, ["sync", "cls-1", "--db", str(db_path)])

    assert result.exit_code == 1
    output = (result.stdout or "") + (result.stderr or "")
    assert "sync failed" in output
    assert "--from-csv" in output


# ---------- run: add-file ----------


@patch("tacon.cli.RateLimitedClient")
def test_run_add_file_dry_run_prints_plan(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    mock_rl.return_value = fake_gh
    fake_repo.get_contents.side_effect = _missing()  # file absent → unblocked
    content_file = tmp_path / "STARTER.md"
    content_file.write_text("hello\nworld\n")

    result = runner.invoke(
        app,
        [
            "run", "add-file",
            "--path", "STARTER.md",
            "--content-from", str(content_file),
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 0
    assert "Plan: add_file" in result.stdout
    # 3 repos all ready
    assert "3 ready" in result.stdout or "3" in result.stdout
    assert "dry run" in result.stdout.lower()


@patch("tacon.cli.RateLimitedClient")
def test_run_add_file_apply_yes_writes_events(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    mock_rl.return_value = fake_gh
    fake_repo.get_contents.side_effect = _missing()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    content_file = tmp_path / "STARTER.md"
    content_file.write_text("hello\n")

    result = runner.invoke(
        app,
        [
            "run", "add-file",
            "--path", "STARTER.md",
            "--content-from", str(content_file),
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 0
    assert "op_id" in result.stdout
    assert "3 applied" in result.stdout


# ---------- run: delete-file ----------


def test_run_delete_file_missing_path_exits_2(seeded_db_path: Path) -> None:
    result = runner.invoke(app, ["run", "delete-file", "--db", str(seeded_db_path)])
    assert result.exit_code == 2


@patch("tacon.cli.RateLimitedClient")
def test_run_delete_file_dry_run(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    mock_rl.return_value = fake_gh
    fake_repo.get_contents.return_value = _content_file("orig", b"x\ny\n")

    result = runner.invoke(
        app,
        ["run", "delete-file", "--path", "OLD.md", "--db", str(seeded_db_path)],
    )
    assert result.exit_code == 0
    assert "delete_file" in result.stdout


# ---------- run: add-ci-workflow ----------


VALID_WORKFLOW_YAML = """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""


@patch("tacon.cli.RateLimitedClient")
def test_run_add_ci_workflow_dry_run(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    mock_rl.return_value = fake_gh
    fake_repo.get_contents.side_effect = _missing()
    wf_file = tmp_path / "ci.yml"
    wf_file.write_text(VALID_WORKFLOW_YAML)

    result = runner.invoke(
        app,
        [
            "run", "add-ci-workflow",
            "--workflow-name", "ci",
            "--content-from", str(wf_file),
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 0
    assert "add_ci_workflow" in result.stdout


def test_run_add_ci_workflow_invalid_yaml_exits_2(
    seeded_db_path: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "broken.yml"
    bad.write_text("not yaml: [unbalanced\n")

    result = runner.invoke(
        app,
        [
            "run", "add-ci-workflow",
            "--workflow-name", "ci",
            "--content-from", str(bad),
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 2
    output = (result.stdout or "") + (result.stderr or "")
    assert "invalid workflow" in output


def test_run_add_ci_workflow_missing_args_exits_2(seeded_db_path: Path) -> None:
    result = runner.invoke(
        app, ["run", "add-ci-workflow", "--db", str(seeded_db_path)]
    )
    assert result.exit_code == 2


# ---------- run: fix-ci-workflow ----------


@patch("tacon.cli.RateLimitedClient")
def test_run_fix_ci_workflow_dry_run(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    mock_rl.return_value = fake_gh
    body = b"steps:\n  - uses: actions/checkout@v3\n"
    fake_repo.get_contents.return_value = _content_file("orig", body)

    result = runner.invoke(
        app,
        [
            "run", "fix-ci-workflow",
            "--workflow-name", "ci",
            "--bump-action", "actions/checkout@v3=actions/checkout@v4",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 0
    assert "fix_ci_workflow" in result.stdout


def test_run_fix_ci_workflow_missing_args_exits_2(seeded_db_path: Path) -> None:
    result = runner.invoke(
        app, ["run", "fix-ci-workflow", "--db", str(seeded_db_path)]
    )
    assert result.exit_code == 2


def test_run_fix_ci_workflow_bad_bump_action_exits_2(seeded_db_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run", "fix-ci-workflow",
            "--workflow-name", "ci",
            "--bump-action", "no-equals-sign",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 2


def test_run_fix_ci_workflow_identical_bump_action_exits_2(seeded_db_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run", "fix-ci-workflow",
            "--workflow-name", "ci",
            "--bump-action", "a=a",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 2


# ---------- run: add-branch-protection ----------


@patch("tacon.cli.RateLimitedClient")
def test_run_add_branch_protection_dry_run(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    mock_rl.return_value = fake_gh
    branch = MagicMock(name="Branch")
    branch.protected = False
    fake_repo.get_branch.return_value = branch

    result = runner.invoke(
        app,
        ["run", "add-branch-protection", "--db", str(seeded_db_path)],
    )
    assert result.exit_code == 0
    assert "add_branch_protection" in result.stdout


# ---------- rollback ----------


def test_rollback_unknown_op_id_exits_1(seeded_db_path: Path) -> None:
    result = runner.invoke(app, ["rollback", "op-nonexistent", "--db", str(seeded_db_path)])
    assert result.exit_code == 1
    output = (result.stdout or "") + (result.stderr or "")
    assert "No events" in output


@patch("tacon.cli.RateLimitedClient")
def test_rollback_unsupported_op_for_read_only(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """add-branch-protection is read-only; rollback should error cleanly."""
    mock_rl.return_value = fake_gh
    branch = MagicMock(name="Branch")
    branch.protected = False
    fake_repo.get_branch.return_value = branch

    # First "apply" the read-only op so events exist
    apply_result = runner.invoke(
        app,
        [
            "run", "add-branch-protection",
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert apply_result.exit_code == 0
    # Extract op_id from the output
    op_id = _extract_op_id(apply_result.stdout)

    rollback_result = runner.invoke(
        app, ["rollback", op_id, "--db", str(seeded_db_path)]
    )
    assert rollback_result.exit_code == 1
    output = (rollback_result.stdout or "") + (rollback_result.stderr or "")
    assert "does not support rollback" in output


@patch("tacon.cli.RateLimitedClient")
def test_rollback_full_flow_for_add_file(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    """Apply add-file, then rollback. Verify _print_rollback_result executes."""
    mock_rl.return_value = fake_gh
    fake_repo.get_contents.side_effect = _missing()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    content_file = tmp_path / "STARTER.md"
    content_file.write_text("hi\n")

    apply_res = runner.invoke(
        app,
        [
            "run", "add-file",
            "--path", "STARTER.md",
            "--content-from", str(content_file),
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert apply_res.exit_code == 0
    op_id = _extract_op_id(apply_res.stdout)

    # Now rollback: file present with same blob, will delete cleanly
    fake_repo.get_contents.side_effect = None
    fake_repo.get_contents.return_value = _content_file("blob-1")
    fake_repo.delete_file.return_value = {"commit": _commit("revert-c1")}

    rb_res = runner.invoke(app, ["rollback", op_id, "--db", str(seeded_db_path)])
    assert rb_res.exit_code == 0
    assert "Rollback" in rb_res.stdout
    assert "rolled_back" in rb_res.stdout


# ---------- resume ----------


def test_resume_unknown_op_id_no_failed(seeded_db_path: Path) -> None:
    """No events at all → 'No failed events' message, exit 0."""
    result = runner.invoke(
        app, ["resume", "op-nonexistent", "--db", str(seeded_db_path)]
    )
    assert result.exit_code == 0
    assert "No failed events" in result.stdout


@patch("tacon.cli.RateLimitedClient")
def test_resume_lists_failed_repos(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    """Apply that fails → resume prints the failed repos."""
    mock_rl.return_value = fake_gh
    fake_repo.get_contents.side_effect = _missing()
    fake_repo.create_file.side_effect = GithubException(
        422, {"message": "branch protection"}, {}
    )
    content_file = tmp_path / "X"
    content_file.write_text("x\n")

    apply_res = runner.invoke(
        app,
        [
            "run", "add-file",
            "--path", "X",
            "--content-from", str(content_file),
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert apply_res.exit_code == 0
    # All 3 should have failed, so we need a non-empty op_id
    op_id = _extract_op_id(apply_res.stdout)
    assert "3 failed" in apply_res.stdout

    resume_res = runner.invoke(
        app, ["resume", op_id, "--db", str(seeded_db_path)]
    )
    # resume currently exits 0 even though it prints the workaround hint
    assert resume_res.exit_code == 0
    output = (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "resume not yet wired" in output
    # All 3 failed repos should appear
    for username in ("alice", "bob", "carol"):
        assert f"cs101/{username}-hw3" in output


# ---------- _make_confirm state machine ----------


def test_make_confirm_yes_short_circuits() -> None:
    confirm = _make_confirm(yes=True)
    diff = RepoDiff(repo_id="r", student_id="s", summary="x", unified_diff="")
    assert confirm(diff) is True


def test_make_confirm_y_returns_true(monkeypatch) -> None:
    confirm = _make_confirm(yes=False)
    monkeypatch.setattr("builtins.input", lambda _="": "y")
    diff = RepoDiff(repo_id="r", student_id="s", summary="x", unified_diff="")
    assert confirm(diff) is True


def test_make_confirm_n_returns_false(monkeypatch) -> None:
    confirm = _make_confirm(yes=False)
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    diff = RepoDiff(repo_id="r", student_id="s", summary="x", unified_diff="")
    assert confirm(diff) is False


def test_make_confirm_a_then_subsequent_calls_skip_prompt(monkeypatch) -> None:
    """[a]ll: returns True forever without re-prompting."""
    confirm = _make_confirm(yes=False)
    answers = iter(["a"])
    monkeypatch.setattr("builtins.input", lambda _="": next(answers))
    d1 = RepoDiff(repo_id="r1", student_id="s1", summary="x", unified_diff="")
    d2 = RepoDiff(repo_id="r2", student_id="s2", summary="x", unified_diff="")
    assert confirm(d1) is True
    # input would StopIteration if it were called again — it must not be
    assert confirm(d2) is True


def test_make_confirm_q_then_subsequent_calls_return_false(monkeypatch) -> None:
    confirm = _make_confirm(yes=False)
    answers = iter(["q"])
    monkeypatch.setattr("builtins.input", lambda _="": next(answers))
    d1 = RepoDiff(repo_id="r1", student_id="s1", summary="x", unified_diff="")
    d2 = RepoDiff(repo_id="r2", student_id="s2", summary="x", unified_diff="")
    assert confirm(d1) is False
    assert confirm(d2) is False


def test_make_confirm_eof_aborts(monkeypatch) -> None:
    """A non-interactive shell raises EOFError on input(); we treat as quit."""
    def raise_eof(_prompt: str = "") -> str:
        raise EOFError

    confirm = _make_confirm(yes=False)
    monkeypatch.setattr("builtins.input", raise_eof)
    diff = RepoDiff(repo_id="r", student_id="s", summary="x", unified_diff="")
    assert confirm(diff) is False
    # And subsequent calls also return False without re-prompting
    assert confirm(diff) is False


def test_make_confirm_invalid_then_valid(monkeypatch) -> None:
    """Garbage input gets a re-prompt; valid input then settles the answer."""
    answers = iter(["maybe", "y"])
    monkeypatch.setattr("builtins.input", lambda _="": next(answers))
    confirm = _make_confirm(yes=False)
    diff = RepoDiff(repo_id="r", student_id="s", summary="x", unified_diff="")
    assert confirm(diff) is True


# ---------- helpers ----------


def _extract_op_id(output: str) -> str:
    """Pull the op_id UUID out of the apply-result printout."""
    import re

    # Format: "op_id: <uuid>" — the bold tag may or may not be stripped depending
    # on terminal width; CliRunner strips ANSI but not bold tags. Use a UUID regex.
    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        output,
    )
    assert match, f"no op_id found in output: {output[:300]}"
    return match.group(0)
