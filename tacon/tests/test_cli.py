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


def test_ui_command_constructs_tui_app(tmp_path: Path, monkeypatch) -> None:
    """`tacon ui` constructs and runs TaconApp. We mock .run() so the test
    doesn't try to attach to a tty."""
    db_path = tmp_path / "tacon.db"
    open_db(db_path)  # create empty schema

    captured: dict[str, object] = {}

    class FakeTUI:
        def __init__(self, db_path: Path) -> None:
            captured["db_path"] = db_path

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr("tacon.tui.TaconApp", FakeTUI)
    result = runner.invoke(app, ["ui", "--db", str(db_path)])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert captured["ran"] is True
    assert captured["db_path"] == db_path


# --publish is now wired (v0.2). Behavior tests live in
# tests/test_dashboard_publish.py — they cover the helper, the CLI wiring,
# malformed-target-repo errors, and the no-publish-flag regression guard.


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


# ---------- run: --via-pr ----------


@patch("tacon.cli.RateLimitedClient")
def test_run_add_file_via_pr_flag_threads_through(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    """`--via-pr --apply` triggers the branch+PR dance instead of direct push."""
    mock_rl.return_value = fake_gh
    fake_repo.get_contents.side_effect = _missing()
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.create_file.return_value = {
        "commit": _commit("c-pr"),
        "content": _content_file("blob-pr"),
    }
    new_pr = MagicMock(name="PR")
    new_pr.number = 13
    fake_repo.create_pull.return_value = new_pr
    content_file = tmp_path / "S.md"
    content_file.write_text("hi\n")

    result = runner.invoke(
        app,
        [
            "run", "add-file",
            "--path", "S.md",
            "--content-from", str(content_file),
            "--via-pr",
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert "3 applied" in result.stdout
    fake_repo.create_pull.assert_called()
    fake_repo.create_git_ref.assert_called()


def test_run_add_branch_protection_with_via_pr_exits_2(seeded_db_path: Path) -> None:
    """add-branch-protection writes repo-level config, not branch content; --via-pr → exit 2."""
    result = runner.invoke(
        app,
        ["run", "add-branch-protection", "--via-pr", "--db", str(seeded_db_path)],
    )
    assert result.exit_code == 2
    output = (result.stdout or "") + (result.stderr or "")
    flat = " ".join(output.split())
    assert "repo-level config" in flat or "does not apply" in flat


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


# ---------- run: add-branch-protection write mode ----------


def test_run_add_branch_protection_rejects_both_rule_flags(
    seeded_db_path: Path, tmp_path: Path
) -> None:
    """--rule-from and --rule-template are mutually exclusive."""
    rule_file = tmp_path / "rule.yaml"
    rule_file.write_text("required_approving_review_count: 1\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run", "add-branch-protection",
            "--rule-from", str(rule_file),
            "--rule-template", "tacon-default",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 2
    flat = " ".join(((result.stdout or "") + (result.stderr or "")).split())
    assert "mutually exclusive" in flat


def test_run_add_branch_protection_rule_from_missing_file_exits_2(
    seeded_db_path: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "run", "add-branch-protection",
            "--rule-from", str(tmp_path / "nope.yaml"),
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 2
    flat = " ".join(((result.stdout or "") + (result.stderr or "")).split())
    assert "not found" in flat


def test_run_add_branch_protection_rule_from_invalid_yaml_exits_2(
    seeded_db_path: Path, tmp_path: Path
) -> None:
    rule_file = tmp_path / "bad.yaml"
    rule_file.write_text("required_approving_review_count: [unclosed", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run", "add-branch-protection",
            "--rule-from", str(rule_file),
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 2


def test_run_add_branch_protection_unknown_template_exits_2(
    seeded_db_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "run", "add-branch-protection",
            "--rule-template", "does-not-exist",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 2
    flat = " ".join(((result.stdout or "") + (result.stderr or "")).split())
    assert "unknown rule template" in flat


@patch("tacon.cli.RateLimitedClient")
def test_run_add_branch_protection_with_rule_template_dry_run(
    mock_rl: MagicMock,
    seeded_db_path: Path,
    fake_gh: MagicMock,
    fake_repo: MagicMock,
) -> None:
    """--rule-template tacon-default in dry-run mode plans without writing."""
    mock_rl.return_value = fake_gh
    branch = MagicMock(name="Branch")
    branch.protected = False
    fake_repo.get_branch.return_value = branch

    result = runner.invoke(
        app,
        [
            "run", "add-branch-protection",
            "--rule-template", "tacon-default",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 0
    # Plan summary mentions write-mode language.
    assert "set protection" in result.stdout


@patch("tacon.cli.RateLimitedClient")
def test_run_add_branch_protection_with_rule_from_apply(
    mock_rl: MagicMock,
    seeded_db_path: Path,
    fake_gh: MagicMock,
    fake_repo: MagicMock,
    tmp_path: Path,
) -> None:
    """--rule-from FILE --apply writes protection via edit_protection."""
    mock_rl.return_value = fake_gh
    branch = MagicMock(name="Branch")
    branch.protected = False
    fake_repo.get_branch.return_value = branch

    rule_file = tmp_path / "my-rule.yaml"
    rule_file.write_text(
        "required_approving_review_count: 2\nenforce_admins: true\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "run", "add-branch-protection",
            "--rule-from", str(rule_file),
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert "applied" in result.stdout
    # The rule made it down to PyGithub.
    assert branch.edit_protection.call_count == 3
    last_kw = branch.edit_protection.call_args.kwargs
    assert last_kw.get("required_approving_review_count") == 2
    assert last_kw.get("enforce_admins") is True


# ---------- rollback ----------


def test_rollback_unknown_op_id_exits_1(seeded_db_path: Path) -> None:
    result = runner.invoke(app, ["rollback", "op-nonexistent", "--db", str(seeded_db_path)])
    assert result.exit_code == 1
    output = (result.stdout or "") + (result.stderr or "")
    assert "No events" in output


@patch("tacon.cli.RateLimitedClient")
def test_rollback_of_survey_op_says_nothing_to_roll_back(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """A survey-mode add-branch-protection op produces 'reported' events; rollback
    filters to 'applied', so it finds nothing and exits 1 with a clear message
    (rather than upfront 'does not support rollback', because the class flag is
    True now that write mode is supported)."""
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
    op_id = _extract_op_id(apply_result.stdout)

    rollback_result = runner.invoke(
        app, ["rollback", op_id, "--db", str(seeded_db_path)]
    )
    assert rollback_result.exit_code == 1
    output = (rollback_result.stdout or "") + (rollback_result.stderr or "")
    # rich line-wraps the message; normalize whitespace before checking phrases.
    flat = " ".join(output.split())
    assert "nothing to roll back" in flat
    assert "read-only survey" in flat


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


def _seed_failed_add_file(
    *,
    seeded_db_path: Path,
    fake_gh: MagicMock,
    fake_repo: MagicMock,
    tmp_path: Path,
    content: str = "x\n",
) -> tuple[str, Path]:
    """Run add-file once with create_file raising; return (op_id, content_file)."""
    fake_repo.get_contents.side_effect = _missing()
    fake_repo.create_file.side_effect = GithubException(
        422, {"message": "branch protection"}, {}
    )
    content_file = tmp_path / "X"
    content_file.write_text(content)

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
    assert apply_res.exit_code == 0, (apply_res.stdout or "") + (apply_res.stderr or "")
    assert "3 failed" in apply_res.stdout
    return _extract_op_id(apply_res.stdout), content_file


@patch("tacon.cli.RateLimitedClient")
def test_resume_add_file_requires_content_from(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    """resume without --content-from for an add-file op exits 2."""
    mock_rl.return_value = fake_gh
    op_id, _ = _seed_failed_add_file(
        seeded_db_path=seeded_db_path, fake_gh=fake_gh, fake_repo=fake_repo, tmp_path=tmp_path
    )
    resume_res = runner.invoke(app, ["resume", op_id, "--db", str(seeded_db_path)])
    assert resume_res.exit_code == 2
    output = (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "requires --content-from" in output


@patch("tacon.cli.RateLimitedClient")
def test_resume_add_file_content_length_mismatch_exits_2(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    """resume with --content-from of a different byte length is rejected."""
    mock_rl.return_value = fake_gh
    op_id, _ = _seed_failed_add_file(
        seeded_db_path=seeded_db_path, fake_gh=fake_gh, fake_repo=fake_repo, tmp_path=tmp_path,
        content="x\n",  # 2 bytes
    )
    wrong = tmp_path / "WRONG"
    wrong.write_text("xxxxxxx\n")  # 8 bytes

    resume_res = runner.invoke(
        app,
        ["resume", op_id, "--content-from", str(wrong), "--db", str(seeded_db_path)],
    )
    assert resume_res.exit_code == 2
    output = (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "byte length" in output
    assert "Wrong file?" in output


@patch("tacon.cli.RateLimitedClient")
def test_resume_add_file_replays_failed_repos(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    """Happy path: 3 failed → resume with correct content → 3 applied under a NEW op_id.

    The original failed events get an error_message annotation pointing at the
    resume op so the audit trail isn't ambiguous.
    """
    mock_rl.return_value = fake_gh
    op_id, content_file = _seed_failed_add_file(
        seeded_db_path=seeded_db_path, fake_gh=fake_gh, fake_repo=fake_repo, tmp_path=tmp_path,
    )

    # Now make the writes succeed for the resume run.
    fake_repo.create_file.side_effect = None
    fake_repo.create_file.return_value = {
        "commit": _commit("c-resume"),
        "content": _content_file("blob-resume"),
    }

    resume_res = runner.invoke(
        app,
        [
            "resume", op_id,
            "--content-from", str(content_file),
            "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert resume_res.exit_code == 0, (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "3 applied" in resume_res.stdout
    new_op_id = _extract_op_id(resume_res.stdout)
    assert new_op_id != op_id

    # Verify the original failed events were annotated with the resume op_id
    db = open_db(seeded_db_path)
    rows = list(db.query("SELECT error_message FROM events WHERE op_id = ?", (op_id,)))
    assert rows, f"original op_id={op_id} has no events"
    for row in rows:
        assert f"resumed in op_id={new_op_id}" in (row["error_message"] or ""), row


@patch("tacon.cli.RateLimitedClient")
def test_resume_delete_file_does_not_require_content_from(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """delete-file resume needs no --content-from (no content in op_args)."""
    mock_rl.return_value = fake_gh
    # Fail apply: get_contents returns the file, but delete_file raises
    fake_repo.get_contents.return_value = _content_file("orig", b"x\ny\n")
    fake_repo.delete_file.side_effect = GithubException(403, {"message": "forbidden"}, {})

    apply_res = runner.invoke(
        app,
        [
            "run", "delete-file",
            "--path", "OLD.md",
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert apply_res.exit_code == 0, (apply_res.stdout or "") + (apply_res.stderr or "")
    assert "3 failed" in apply_res.stdout
    op_id = _extract_op_id(apply_res.stdout)

    # Now let delete succeed
    fake_repo.delete_file.side_effect = None
    fake_repo.delete_file.return_value = {"commit": _commit("c-del")}

    resume_res = runner.invoke(
        app, ["resume", op_id, "--yes", "--db", str(seeded_db_path)]
    )
    assert resume_res.exit_code == 0, (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "3 applied" in resume_res.stdout


@patch("tacon.cli.RateLimitedClient")
def test_resume_fix_ci_workflow_reconstructs_transform(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """fix-ci-workflow resume rebuilds the bump-action transform from transform_id."""
    mock_rl.return_value = fake_gh
    body = b"steps:\n  - uses: actions/checkout@v3\n"
    fake_repo.get_contents.return_value = _content_file("orig", body)
    fake_repo.update_file.side_effect = GithubException(422, {"message": "boom"}, {})

    apply_res = runner.invoke(
        app,
        [
            "run", "fix-ci-workflow",
            "--workflow-name", "ci",
            "--bump-action", "actions/checkout@v3=actions/checkout@v4",
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert apply_res.exit_code == 0, (apply_res.stdout or "") + (apply_res.stderr or "")
    assert "3 failed" in apply_res.stdout
    op_id = _extract_op_id(apply_res.stdout)

    # Now let update succeed for the resume.
    fake_repo.update_file.side_effect = None
    fake_repo.update_file.return_value = {
        "commit": _commit("c-fix"),
        "content": _content_file("blob-fix", b"steps:\n  - uses: actions/checkout@v4\n"),
    }

    resume_res = runner.invoke(
        app, ["resume", op_id, "--yes", "--db", str(seeded_db_path)]
    )
    assert resume_res.exit_code == 0, (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "3 applied" in resume_res.stdout


def test_resume_fix_ci_workflow_unrecognized_transform_id_exits_2(
    seeded_db_path: Path,
) -> None:
    """A synthetic event with a transform_id we can't reconstruct → exit 2."""
    from tacon import __version__
    from tacon.db import insert_event

    db = open_db(seeded_db_path)
    op_id = "synthetic-op-1"
    insert_event(
        db,
        op_id=op_id,
        op_class="fix_ci_workflow",
        op_args_json='{"path": ".github/workflows/ci.yml", "workflow_name": "ci", '
                     '"transform_id": "custom-rewriter v1", "message": "tacon: fix CI workflow", '
                     '"assignment_id": null}',
        tacon_version=__version__,
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="failed",
        error_message="seeded for resume reconstruction test",
    )

    resume_res = runner.invoke(app, ["resume", op_id, "--db", str(seeded_db_path)])
    assert resume_res.exit_code == 2
    output = (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "cannot reconstruct transform_id" in output


@patch("tacon.cli.RateLimitedClient")
def test_resume_add_ci_workflow_replays_failed_repos(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    """add-ci-workflow resume reads --content-from and rebuilds AddCIWorkflow."""
    mock_rl.return_value = fake_gh
    fake_repo.get_contents.side_effect = _missing()
    fake_repo.create_file.side_effect = GithubException(422, {"message": "boom"}, {})
    wf_file = tmp_path / "ci.yml"
    wf_file.write_text(VALID_WORKFLOW_YAML)

    apply_res = runner.invoke(
        app,
        [
            "run", "add-ci-workflow",
            "--workflow-name", "ci",
            "--content-from", str(wf_file),
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert apply_res.exit_code == 0, (apply_res.stdout or "") + (apply_res.stderr or "")
    assert "3 failed" in apply_res.stdout
    op_id = _extract_op_id(apply_res.stdout)

    fake_repo.create_file.side_effect = None
    fake_repo.create_file.return_value = {
        "commit": _commit("c-wf"),
        "content": _content_file("blob-wf"),
    }

    resume_res = runner.invoke(
        app,
        [
            "resume", op_id,
            "--content-from", str(wf_file),
            "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert resume_res.exit_code == 0, (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "3 applied" in resume_res.stdout


def test_resume_add_branch_protection_branch_constructs(
    seeded_db_path: Path,
) -> None:
    """The add_branch_protection branch in _reconstruct_op rebuilds AddBranchProtection.

    Synthetic: the apply() path for AddBranchProtection never writes status='failed'
    naturally, so we seed a failed event directly and check resume reconstructs the op.
    The plan() call (which would hit GitHub) is short-circuited by archiving the repo.
    """
    from tacon import __version__
    from tacon.db import archive_repo, insert_event

    db = open_db(seeded_db_path)
    op_id = "synthetic-bp-1"
    insert_event(
        db,
        op_id=op_id,
        op_class="add_branch_protection",
        op_args_json='{"branch": "main", "assignment_id": null, "mode": "report"}',
        tacon_version=__version__,
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="failed",
        error_message="seeded",
    )
    # Archive the only failed repo so plan() returns nothing for it -> resume hits
    # the "no failed repos still active" branch (exercises lines 314-319 + the
    # AddBranchProtection branch in _reconstruct_op).
    archive_repo(db, "cs101/alice-hw3")

    resume_res = runner.invoke(app, ["resume", op_id, "--db", str(seeded_db_path)])
    assert resume_res.exit_code == 1
    output = (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "no failed repos are still active" in output


@patch("tacon.cli.RateLimitedClient")
def test_resume_add_branch_protection_write_mode_threads_rule(
    mock_rl: MagicMock,
    seeded_db_path: Path,
    fake_gh: MagicMock,
    fake_repo: MagicMock,
) -> None:
    """A resumed write-mode op reads the rule dict from op_args and replays it."""
    from tacon import __version__
    from tacon.db import insert_event

    mock_rl.return_value = fake_gh

    db = open_db(seeded_db_path)
    op_id = "synthetic-bp-write-1"
    op_args_json = (
        '{"assignment_id": null, "branch": "main", '
        '"mode": "write", '
        '"rule": {"required_approving_review_count": 1, '
        '"dismiss_stale_reviews": false, "require_code_owner_reviews": false, '
        '"required_status_checks": null, "strict_status_checks": false, '
        '"enforce_admins": false, "allow_force_pushes": false, '
        '"allow_deletions": false, "required_linear_history": false}}'
    )
    insert_event(
        db,
        op_id=op_id,
        op_class="add_branch_protection",
        op_args_json=op_args_json,
        tacon_version=__version__,
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="failed",
        error_message="seeded",
    )

    branch = MagicMock(name="Branch")
    branch.protected = False
    fake_repo.get_branch.return_value = branch

    resume_res = runner.invoke(
        app, ["resume", op_id, "--yes", "--db", str(seeded_db_path)]
    )
    assert resume_res.exit_code == 0, (resume_res.stdout or "") + (
        resume_res.stderr or ""
    )
    # The replay applied the rule via edit_protection.
    assert branch.edit_protection.called
    last_kw = branch.edit_protection.call_args.kwargs
    assert last_kw.get("required_approving_review_count") == 1


@patch("tacon.cli.RateLimitedClient")
def test_resume_via_pr_op_threads_via_pr_through_reconstruction(
    mock_rl: MagicMock, seeded_db_path: Path, fake_gh: MagicMock, fake_repo: MagicMock, tmp_path: Path
) -> None:
    """A resumed via-pr op reads `via_pr=True` from op_args and replays as via-pr.

    Each resumed repo gets a fresh op_id → fresh branch + PR. We verify by
    asserting the resume run hits create_git_ref + create_pull (proving the
    via-pr flag flowed through `_reconstruct_op`).
    """
    mock_rl.return_value = fake_gh
    # Original apply: 3 failures.
    fake_repo.get_contents.side_effect = _missing()
    head = MagicMock(name="Branch")
    head.commit.sha = "default-sha"
    fake_repo.get_branch.return_value = head
    fake_repo.create_git_ref.return_value = MagicMock()
    # Make create_file fail to leave 3 failed events (mid-flight failure
    # AFTER branch creation succeeded — the orphan-branch case).
    fake_repo.create_file.side_effect = GithubException(500, {"message": "boom"}, {})
    content_file = tmp_path / "S.md"
    content_file.write_text("hi\n")

    apply_res = runner.invoke(
        app,
        [
            "run", "add-file",
            "--path", "S.md",
            "--content-from", str(content_file),
            "--via-pr",
            "--apply", "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert apply_res.exit_code == 0
    assert "3 failed" in apply_res.stdout
    op_id = _extract_op_id(apply_res.stdout)

    # Now make the writes succeed; resume should re-create new branches + PRs
    # with a NEW op_id (ensure_branch returns "exists_same" for the orphan
    # branches at the same SHA, so the create_git_ref will be called by the
    # resume flow but possibly raise 422; we make it succeed cleanly via a
    # different branch prefix the new op_id produces).
    fake_repo.create_git_ref.side_effect = None
    fake_repo.create_git_ref.return_value = MagicMock()
    fake_repo.create_file.side_effect = None
    fake_repo.create_file.return_value = {
        "commit": _commit("c-resume"),
        "content": _content_file("blob-resume"),
    }
    new_pr = MagicMock(name="PR")
    new_pr.number = 99
    fake_repo.create_pull.return_value = new_pr

    resume_res = runner.invoke(
        app,
        [
            "resume", op_id,
            "--content-from", str(content_file),
            "--yes",
            "--db", str(seeded_db_path),
        ],
    )
    assert resume_res.exit_code == 0, (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "3 applied" in resume_res.stdout
    # Resume must have opened PRs (proves via_pr was reconstructed)
    assert fake_repo.create_pull.called


def test_resume_unknown_op_class_exits_1(seeded_db_path: Path) -> None:
    """A synthetic event with an op_class no longer registered → exit 1."""
    from tacon import __version__
    from tacon.db import insert_event

    db = open_db(seeded_db_path)
    op_id = "synthetic-op-2"
    insert_event(
        db,
        op_id=op_id,
        op_class="ghost_op",
        op_args_json='{"foo": "bar"}',
        tacon_version=__version__,
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="failed",
        error_message="seeded",
    )

    resume_res = runner.invoke(app, ["resume", op_id, "--db", str(seeded_db_path)])
    assert resume_res.exit_code == 1
    output = (resume_res.stdout or "") + (resume_res.stderr or "")
    assert "unknown op_class" in output


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
    """Pull the op_id UUID out of the apply-result printout.

    Anchors on the `op_id:` label (printed by _print_apply_result) rather than
    matching any UUID, because resume output also references the *original*
    op_id earlier in the line, and we want the new one.
    """
    import re

    match = re.search(
        r"op_id:[^\n0-9a-f]*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        output,
    )
    assert match, f"no op_id found in output: {output[:300]}"
    return match.group(1)
