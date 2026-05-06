"""Tests for tacon.classroom: gh shell-out + CSV fallback + persistence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlite_utils import Database

from tacon.classroom import (
    GhClassroomError,
    discover_via_csv,
    discover_via_gh_classroom,
    persist_discovered,
)

# ---------- CSV path ----------


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    import csv

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["assignment_slug", "student_username", "repo_url"])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def test_csv_basic_parse(tmp_path: Path) -> None:
    p = tmp_path / "roster.csv"
    _write_csv(
        p,
        [
            {
                "assignment_slug": "hw3",
                "student_username": "Alice",
                "repo_url": "https://github.com/cs101/hw3-alice",
            },
            {
                "assignment_slug": "hw3",
                "student_username": "bob",
                "repo_url": "https://github.com/cs101/hw3-bob.git",
            },
        ],
    )
    result = discover_via_csv(p)
    assert len(result) == 2
    assert result[0].repo_full_name == "cs101/hw3-alice"
    assert result[1].repo_full_name == "cs101/hw3-bob"
    assert result[0].student_username == "Alice"


def test_csv_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(GhClassroomError, match="does not exist"):
        discover_via_csv(tmp_path / "nope.csv")


def test_csv_missing_header_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv"
    p.write_text("foo,bar\n1,2\n")
    with pytest.raises(GhClassroomError, match="missing required columns"):
        discover_via_csv(p)


def test_csv_empty_field_raises(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    _write_csv(
        p,
        [
            {
                "assignment_slug": "hw3",
                "student_username": "",
                "repo_url": "https://github.com/cs101/hw3-alice",
            }
        ],
    )
    with pytest.raises(GhClassroomError, match="empty field"):
        discover_via_csv(p)


def test_csv_unparseable_url_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad-url.csv"
    _write_csv(
        p, [{"assignment_slug": "hw3", "student_username": "alice", "repo_url": "not-a-url"}]
    )
    with pytest.raises(GhClassroomError, match="cannot parse"):
        discover_via_csv(p)


# ---------- gh classroom path ----------


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@patch("tacon.classroom.shutil.which", return_value="/usr/bin/gh")
@patch("tacon.classroom.subprocess.run")
def test_gh_classroom_happy_path(mock_run, mock_which) -> None:  # noqa: ARG001
    mock_run.side_effect = [
        _proc(json.dumps([{"id": "1", "slug": "hw3", "title": "HW3"}])),
        _proc(
            json.dumps(
                [
                    {"repository_full_name": "cs101/hw3-alice", "login": "alice"},
                    {"repository_full_name": "cs101/hw3-bob", "login": "bob"},
                ]
            )
        ),
    ]
    result = discover_via_gh_classroom("cls-1")
    assert len(result) == 2
    assert {r.student_username for r in result} == {"alice", "bob"}


@patch("tacon.classroom.shutil.which", return_value=None)
def test_gh_classroom_missing_cli_raises(mock_which) -> None:  # noqa: ARG001
    with pytest.raises(GhClassroomError, match="not installed"):
        discover_via_gh_classroom("cls-1")


@patch("tacon.classroom.shutil.which", return_value="/usr/bin/gh")
@patch("tacon.classroom.subprocess.run")
def test_gh_classroom_nonzero_exit_raises(mock_run, mock_which) -> None:  # noqa: ARG001
    mock_run.return_value = _proc("", returncode=1, stderr="boom")
    with pytest.raises(GhClassroomError, match="exited 1"):
        discover_via_gh_classroom("cls-1")


@patch("tacon.classroom.shutil.which", return_value="/usr/bin/gh")
@patch("tacon.classroom.subprocess.run")
def test_gh_classroom_invalid_json_raises(mock_run, mock_which) -> None:  # noqa: ARG001
    mock_run.return_value = _proc("not json {{{")
    with pytest.raises(GhClassroomError, match="invalid JSON"):
        discover_via_gh_classroom("cls-1")


@patch("tacon.classroom.shutil.which", return_value="/usr/bin/gh")
@patch("tacon.classroom.subprocess.run")
def test_gh_classroom_derives_user_from_repo_name(mock_run, mock_which) -> None:  # noqa: ARG001
    # Simulate API not returning login; we should fall back to slug parsing
    mock_run.side_effect = [
        _proc(json.dumps([{"id": "1", "slug": "hw3", "title": "HW3"}])),
        _proc(json.dumps([{"repository_full_name": "cs101/hw3-carol"}])),
    ]
    result = discover_via_gh_classroom("cls-1")
    assert len(result) == 1
    assert result[0].student_username == "carol"


# ---------- persistence ----------


def test_persist_discovered_creates_assignments_students_repos(
    tmp_db: Database, tmp_path: Path
) -> None:
    p = tmp_path / "roster.csv"
    _write_csv(
        p,
        [
            {
                "assignment_slug": "hw3",
                "student_username": "Alice",
                "repo_url": "https://github.com/cs101/hw3-alice",
            },
            {
                "assignment_slug": "hw3",
                "student_username": "alice",  # duplicate (case)
                "repo_url": "https://github.com/cs101/hw3-alice",
            },
            {
                "assignment_slug": "hw4",
                "student_username": "Bob",
                "repo_url": "https://github.com/cs101/hw4-bob",
            },
        ],
    )
    discovered = discover_via_csv(p)
    persist_discovered(tmp_db, discovered)

    # 2 assignments, 2 students (alice de-duped via lowercase), 2 repos
    assert tmp_db["assignments"].count == 2
    assert tmp_db["students"].count == 2
    assert tmp_db["repos"].count == 2

    alice = tmp_db["students"].get("alice")
    assert alice["display_name"] in {"Alice", "alice"}
