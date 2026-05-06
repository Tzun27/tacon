"""Smoke tests for the Typer CLI wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from tacon.cli import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "tacon" in result.stdout


def test_run_unknown_op_exits_2() -> None:
    result = runner.invoke(app, ["run", "delete-everything"])
    assert result.exit_code == 2


def test_run_add_file_missing_args_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "add-file"])
    assert result.exit_code == 2


def test_ui_command_is_stub(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ui"])
    assert result.exit_code == 2
    # Stub message lands in stderr; runner mixes stdout and stderr by default
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
