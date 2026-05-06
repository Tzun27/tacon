"""Live test harness — scoped to a single GitHub Classroom assignment.

These tests hit the real GitHub API. They are SKIPPED by default and only
run when TACON_LIVE=1. The scope guard refuses to operate on any repo
that isn't in the configured org AND doesn't carry the configured
assignment prefix in its name.

Required env (typically loaded from .env in the project root):
  TACON_GITHUB_TOKEN          — token with `repo` scope
  TACON_TEST_ORG              — the org that owns the classroom repos
  TACON_TEST_ASSIGNMENT_PREFIX — the assignment slug, e.g. "pre-test-hw"
  TACON_TEST_REPO             — (optional) specific repo to write to
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tacon.github_client import RateLimitedClient


class OutOfScopeError(RuntimeError):
    """Raised when a live test would touch a repo outside the configured scope.

    Tests catch this directly. It is NOT a pytest fail — it propagates
    out of any test that ignores it, terminating the test run before any
    write hits the API.
    """


# ---------- env loading ----------


def _load_dotenv() -> None:
    """Look for a .env at the project root; load any missing vars from it.

    Existing env vars take precedence (so CI / explicit shell exports win).
    """
    # tests/live/conftest.py -> tests/live -> tests -> project root
    project_root = Path(__file__).resolve().parents[2]
    dotenv = project_root / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


LIVE_ENABLED = os.environ.get("TACON_LIVE") == "1"

# Frozen scope: tests can never operate outside this assignment.
TEST_ORG = os.environ.get("TACON_TEST_ORG", "")
ASSIGNMENT_PREFIX = os.environ.get("TACON_TEST_ASSIGNMENT_PREFIX", "")
TEST_REPO = os.environ.get("TACON_TEST_REPO", "")
TOKEN = os.environ.get("TACON_GITHUB_TOKEN", "") or os.environ.get(
    "GITHUB_TOKEN", ""
)


# Skip ALL tests in this directory unless explicitly enabled.
collect_ignore_glob: list[str] = []
if not LIVE_ENABLED:
    collect_ignore_glob.append("test_live_*.py")


# ---------- scope guard ----------


def assert_in_scope(repo_full_name: str) -> None:
    """Hard guard: refuse to operate on repos outside the configured scope.

    This is the single most important function in the live test suite.
    Any code that interacts with a real repo MUST call this first with
    the full repo name (owner/repo). It raises OutOfScopeError immediately
    if the repo is outside the configured org or doesn't carry the
    assignment prefix — propagating out aborts the test run before any
    write hits the API.
    """
    if not TEST_ORG:
        raise OutOfScopeError(
            "Refusing to run live test: TACON_TEST_ORG is not set. "
            "Configure it in .env first."
        )
    if not ASSIGNMENT_PREFIX:
        raise OutOfScopeError(
            "Refusing to run live test: TACON_TEST_ASSIGNMENT_PREFIX is not set."
        )
    if "/" not in repo_full_name:
        raise OutOfScopeError(
            f"Refusing live test: malformed repo name {repo_full_name!r}"
        )
    org, repo = repo_full_name.split("/", 1)
    if org != TEST_ORG:
        raise OutOfScopeError(
            f"Refusing live test: repo {repo_full_name!r} is in org {org!r}, "
            f"but the configured TACON_TEST_ORG is {TEST_ORG!r}. "
            "tacon's live tests will not touch repos outside the configured org."
        )
    if ASSIGNMENT_PREFIX not in repo:
        raise OutOfScopeError(
            f"Refusing live test: repo {repo_full_name!r} does not carry "
            f"the configured assignment prefix {ASSIGNMENT_PREFIX!r}. "
            "tacon's live tests will not touch repos outside the configured "
            "assignment."
        )


# ---------- fixtures ----------


@pytest.fixture(scope="session")
def live_client() -> RateLimitedClient:
    if not TOKEN:
        pytest.skip("TACON_GITHUB_TOKEN not set — skipping live test")
    # 1 req/sec is generous for a small classroom and keeps us well below
    # GitHub's secondary rate limits.
    return RateLimitedClient(token=TOKEN, rate_per_sec=1.0, max_retries=2)


@pytest.fixture(scope="session")
def assignment_repos(live_client: RateLimitedClient) -> list[str]:
    """All repos under TEST_ORG matching the assignment prefix.

    Caller must still call assert_in_scope(name) before any write —
    this fixture is a discovery aid, not a substitute for the guard.
    """
    if not TEST_ORG or not ASSIGNMENT_PREFIX:
        pytest.skip("scope env vars not set")
    org = live_client.gh.get_organization(TEST_ORG)
    matches = []
    for repo in live_client.call(org.get_repos):
        if ASSIGNMENT_PREFIX in repo.name:
            full_name = f"{TEST_ORG}/{repo.name}"
            assert_in_scope(full_name)  # double-check: the prefix lives in name
            matches.append(full_name)
    return matches


@pytest.fixture
def write_target_repo(assignment_repos: list[str]) -> str:
    """The single repo the apply+rollback test should operate on."""
    if TEST_REPO:
        assert_in_scope(TEST_REPO)
        return TEST_REPO
    if not assignment_repos:
        pytest.skip(
            f"no repos matching {ASSIGNMENT_PREFIX!r} found in {TEST_ORG!r}"
        )
    target = assignment_repos[0]
    assert_in_scope(target)
    return target
