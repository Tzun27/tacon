"""Roster + assignment discovery.

Two paths:
  1. Shell out to `gh classroom` (the official GitHub extension). Primary.
  2. Read a CSV the user provides via `tacon sync --from-csv repos.csv`.
     Fallback for when `gh classroom` is missing, broken, or unavailable
     (Windows quirks, locked-down lab machines, etc.).

Both paths populate the same db rows so downstream code is path-agnostic.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlite_utils import Database

from tacon.db import now_iso, upsert_assignment, upsert_repo, upsert_student

# Repo names produced by GitHub Classroom typically follow this pattern:
#   <assignment-slug>-<github-username>
# We use this as a fallback for deriving student_id when the API doesn't give it.
_REPO_NAME_RE = re.compile(r"^(?P<slug>.+)-(?P<user>[A-Za-z0-9-]+)$")


class GhClassroomError(RuntimeError):
    """Raised when `gh classroom` is missing, broken, or returns unexpected data."""


@dataclass
class DiscoveredRepo:
    """A single (assignment, student, repo) triple, source-agnostic."""

    assignment_id: str
    assignment_slug: str
    assignment_title: str
    classroom_id: str
    student_username: str  # original case
    repo_full_name: str  # owner/repo


# ---------- gh classroom path ----------


def gh_available() -> bool:
    return shutil.which("gh") is not None


def discover_via_gh_classroom(classroom_id: str) -> list[DiscoveredRepo]:
    """Use the gh-classroom extension to discover all repos under a classroom.

    Raises GhClassroomError on any problem so callers can fall back cleanly.
    """
    if not gh_available():
        raise GhClassroomError(
            "`gh` CLI is not installed. Install it from https://cli.github.com/."
        )

    assignments = _gh_json(["gh", "classroom", "assignments", "-c", classroom_id, "--json"])
    if not isinstance(assignments, list):
        raise GhClassroomError(
            f"`gh classroom assignments` returned unexpected payload: {type(assignments).__name__}"
        )

    discovered: list[DiscoveredRepo] = []
    for asn in assignments:
        aid = str(asn.get("id") or asn.get("assignment_id") or "")
        slug = str(asn.get("slug") or "")
        title = str(asn.get("title") or slug or aid)
        if not aid:
            continue

        repos = _gh_json(["gh", "classroom", "assignment", aid, "--json"])
        if not isinstance(repos, list):
            continue

        for r in repos:
            full = r.get("repository_full_name") or r.get("full_name")
            if not full:
                continue
            user = r.get("login") or r.get("student_username")
            if not user:
                user = _derive_username_from_repo(full, slug)
            if not user:
                continue
            discovered.append(
                DiscoveredRepo(
                    assignment_id=aid,
                    assignment_slug=slug,
                    assignment_title=title,
                    classroom_id=classroom_id,
                    student_username=str(user),
                    repo_full_name=str(full),
                )
            )

    return discovered


def _gh_json(cmd: list[str]) -> object:
    """Run a gh subcommand expected to return JSON; raise GhClassroomError on any failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GhClassroomError(f"`{' '.join(cmd)}` failed to launch: {e}") from e
    if proc.returncode != 0:
        raise GhClassroomError(
            f"`{' '.join(cmd)}` exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    out = proc.stdout.strip()
    if not out:
        raise GhClassroomError(f"`{' '.join(cmd)}` returned empty output.")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise GhClassroomError(f"`{' '.join(cmd)}` returned invalid JSON: {e}") from e


def _derive_username_from_repo(full_name: str, slug: str) -> str | None:
    """Best-effort: parse `<slug>-<username>` from the repo name."""
    name = full_name.rsplit("/", 1)[-1]
    if slug and name.startswith(slug + "-"):
        return name[len(slug) + 1 :]
    m = _REPO_NAME_RE.match(name)
    return m.group("user") if m else None


# ---------- CSV fallback path ----------


_CSV_HEADERS = {"assignment_slug", "student_username", "repo_url"}


def discover_via_csv(path: str | Path) -> list[DiscoveredRepo]:
    """Parse a CSV roster. Header REQUIRED: assignment_slug,student_username,repo_url."""
    p = Path(path)
    if not p.exists():
        raise GhClassroomError(f"--from-csv path does not exist: {p}")
    discovered: list[DiscoveredRepo] = []
    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or not _CSV_HEADERS.issubset(reader.fieldnames):
            missing = _CSV_HEADERS - set(reader.fieldnames or [])
            raise GhClassroomError(
                f"CSV missing required columns: {sorted(missing)}. "
                f"Header must be: {sorted(_CSV_HEADERS)}"
            )
        for row_idx, row in enumerate(reader, start=2):
            slug = (row.get("assignment_slug") or "").strip()
            user = (row.get("student_username") or "").strip()
            url = (row.get("repo_url") or "").strip()
            if not (slug and user and url):
                raise GhClassroomError(
                    f"CSV row {row_idx} has empty field(s); all three columns required."
                )
            full = _full_name_from_url(url)
            if not full:
                raise GhClassroomError(
                    f"CSV row {row_idx}: cannot parse owner/repo from repo_url={url!r}"
                )
            # CSV-only mode has no separate assignment_id, so we use the slug
            # (which is unique within a class) as the id too.
            discovered.append(
                DiscoveredRepo(
                    assignment_id=slug,
                    assignment_slug=slug,
                    assignment_title=slug,
                    classroom_id="csv-import",
                    student_username=user,
                    repo_full_name=full,
                )
            )
    return discovered


def _full_name_from_url(url: str) -> str | None:
    """Convert https://github.com/owner/repo[.git] to owner/repo."""
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
        cleaned = cleaned.split("/", 1)[1] if "/" in cleaned else ""
    parts = cleaned.split("/")
    if len(parts) >= 2 and parts[-2] and parts[-1]:
        return f"{parts[-2]}/{parts[-1]}"
    return None


# ---------- Persistence ----------


def persist_discovered(db: Database, discovered: list[DiscoveredRepo]) -> None:
    """Upsert assignments, students, repos. CI status + last_push_at filled by github_client later."""
    seen_assignments: set[str] = set()
    for d in discovered:
        if d.assignment_id not in seen_assignments:
            upsert_assignment(
                db,
                id=d.assignment_id,
                classroom_id=d.classroom_id,
                title=d.assignment_title,
                slug=d.assignment_slug,
                starter_repo=None,
                created_at=now_iso(),
            )
            seen_assignments.add(d.assignment_id)
        sid = upsert_student(db, username=d.student_username)
        upsert_repo(
            db,
            id=d.repo_full_name,
            assignment_id=d.assignment_id,
            student_id=sid,
        )
