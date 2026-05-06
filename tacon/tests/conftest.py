"""Shared fixtures for tacon tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlite_utils import Database

from tacon.db import open_db, upsert_assignment, upsert_repo, upsert_student


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    """A fresh tacon DB in a per-test tmp dir."""
    return open_db(tmp_path / "test.db")


@pytest.fixture
def seed_repos(tmp_db: Database) -> dict[str, Any]:
    """Pre-populate one assignment + 3 students/repos."""
    upsert_assignment(
        tmp_db,
        id="asn-1",
        classroom_id="cls-1",
        title="HW3 Recursion",
        slug="hw3-recursion",
        starter_repo=None,
        created_at="2026-05-01T00:00:00Z",
    )
    for user in ("Alice", "bob", "carol"):
        sid = upsert_student(tmp_db, username=user)
        upsert_repo(
            tmp_db,
            id=f"cs101/{user.lower()}-hw3",
            assignment_id="asn-1",
            student_id=sid,
        )
    return {"assignment_id": "asn-1", "repo_count": 3}


@pytest.fixture
def fake_repo() -> MagicMock:
    """A PyGithub Repository mock with create_file/delete_file/get_contents."""
    repo = MagicMock(name="Repository")
    repo.default_branch = "main"
    return repo


@pytest.fixture
def fake_gh(fake_repo: MagicMock) -> MagicMock:
    """A RateLimitedClient mock that returns the fake repo and pass-throughs call()."""
    gh = MagicMock(name="RateLimitedClient")
    gh.get_repo.return_value = fake_repo
    # call(fn, *a, **kw) -> fn(*a, **kw); strips throttle/retry for tests
    gh.call.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    return gh
