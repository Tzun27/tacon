"""SQLite schema + accessors.

The DB is the source of truth. Two consumers read it: the CLI/TUI for
ops + status, and the dashboard renderer for the prof-facing site.

Schema versioning lives in the `meta` table. Migration history:
  v1 (tacon 0.0.1) — initial schema.
  v2 (tacon 0.2.0) — added events.pr_number + events.pr_branch for
                     `--via-pr` mode. Fully nullable; v1 rows stay valid.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from sqlite_utils import Database
from sqlite_utils.db import Table

SCHEMA_VERSION = 2


def _t(db: Database, name: str) -> Table:
    """Get a `Table` (not `Table | View`) by name. tacon never creates views."""
    return cast(Table, db.table(name))


def now_iso() -> str:
    """Single source of truth for timestamps. UTC, ISO-8601, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_uuid() -> str:
    return str(uuid.uuid4())


def open_db(path: str | Path) -> Database:
    """Open a tacon DB. Initializes the schema if absent."""
    db = Database(str(path))
    init_db(db)
    return db


def init_db(db: Database) -> None:
    """Create tables + indexes if they don't exist. Idempotent."""
    if "meta" not in db.table_names():
        _t(db, "meta").create({"key": str, "value": str}, pk="key")
        _t(db, "meta").insert({"key": "schema_version", "value": str(SCHEMA_VERSION)})

    if "assignments" not in db.table_names():
        _t(db, "assignments").create(
            {
                "id": str,
                "classroom_id": str,
                "title": str,
                "slug": str,
                "starter_repo": str,
                "created_at": str,
                "synced_at": str,
            },
            pk="id",
            not_null={"id", "classroom_id", "title", "slug", "created_at", "synced_at"},
        )

    if "students" not in db.table_names():
        # id = lowercased GH username (canonical); display_name keeps original case
        _t(db, "students").create(
            {"id": str, "display_name": str, "discord_id": str, "email": str},
            pk="id",
            not_null={"id"},
        )

    if "repos" not in db.table_names():
        _t(db, "repos").create(
            {
                "id": str,
                "assignment_id": str,
                "student_id": str,
                "default_branch": str,
                "last_push_at": str,
                "last_commit_sha": str,
                "ci_status": str,
                "synced_at": str,
                "archived_at": str,
            },
            pk="id",
            not_null={"id", "assignment_id", "student_id", "default_branch", "synced_at"},
            defaults={"default_branch": "main"},
            foreign_keys=[
                ("assignment_id", "assignments", "id"),
                ("student_id", "students", "id"),
            ],
        )
        _t(db, "repos").create_index(["assignment_id"], if_not_exists=True)
        _t(db, "repos").create_index(["student_id"], if_not_exists=True)
        _t(db, "repos").create_index(["last_push_at"], if_not_exists=True)
        _t(db, "repos").create_index(["archived_at"], if_not_exists=True)

    if "events" not in db.table_names():
        _t(db, "events").create(
            {
                "id": str,
                "op_id": str,
                "op_class": str,
                "op_args_json": str,
                "tacon_version": str,
                "repo_id": str,
                "student_id": str,
                "status": str,
                "commit_sha": str,
                "applied_blob_sha": str,
                "error_class": str,
                "error_message": str,
                "created_at": str,
                "applied_at": str,
                "rolled_back_at": str,
                "pr_number": int,
                "pr_branch": str,
            },
            pk="id",
            not_null={
                "id",
                "op_id",
                "op_class",
                "op_args_json",
                "tacon_version",
                "repo_id",
                "student_id",
                "status",
                "created_at",
            },
            foreign_keys=[
                ("repo_id", "repos", "id"),
                ("student_id", "students", "id"),
            ],
        )
        _t(db, "events").create_index(["op_id"], if_not_exists=True)
        _t(db, "events").create_index(["student_id"], if_not_exists=True)
        _t(db, "events").create_index(["status"], if_not_exists=True)
        _t(db, "events").create_index(["error_class"], if_not_exists=True)

    if "interactions" not in db.table_names():
        # Tacon v2.x surface (Discord/email/issue ingestion). Empty until then.
        _t(db, "interactions").create(
            {
                "id": str,
                "source": str,
                "source_ref": str,
                "student_id": str,
                "ta_response": str,
                "question_text": str,
                "created_at": str,
            },
            pk="id",
            not_null={"id", "source", "source_ref", "created_at"},
            foreign_keys=[("student_id", "students", "id")],
        )

    _migrate_to_v2(db)


def _migrate_to_v2(db: Database) -> None:
    """v1 → v2 (schema): add events.pr_number + events.pr_branch.

    Idempotent. Safe to re-run on a partially-migrated DB (e.g. crash
    after add_column but before the meta-version bump): the cols-set
    guard short-circuits. Fresh v2 DBs created via the events table
    create above already have the columns; only the meta row needs
    updating.

    Note: ``meta.schema_version`` value writes are best-effort. The
    existing v1 meta row was inserted at table-create time. Hand-rolled
    DBs that lack the row at all are also handled (insert+replace).
    """
    cols = {c.name for c in _t(db, "events").columns}
    if "pr_number" not in cols:
        _t(db, "events").add_column("pr_number", int)
    if "pr_branch" not in cols:
        _t(db, "events").add_column("pr_branch", str)
    if get_schema_version(db) < SCHEMA_VERSION:
        _t(db, "meta").insert(
            {"key": "schema_version", "value": str(SCHEMA_VERSION)},
            pk="key",
            replace=True,
        )


# ---------- Upserts ----------


def upsert_assignment(
    db: Database,
    *,
    id: str,
    classroom_id: str,
    title: str,
    slug: str,
    starter_repo: str | None,
    created_at: str,
) -> None:
    # Note: sqlite-utils 3.39's `.upsert()` silently no-ops when the table was
    # created with `not_null=...`. `.insert(..., replace=True)` is upsert-equivalent
    # and not affected.
    _t(db, "assignments").insert(
        {
            "id": id,
            "classroom_id": classroom_id,
            "title": title,
            "slug": slug,
            "starter_repo": starter_repo,
            "created_at": created_at,
            "synced_at": now_iso(),
        },
        pk="id",
        replace=True,
    )


def upsert_student(
    db: Database,
    *,
    username: str,
    display_name: str | None = None,
    discord_id: str | None = None,
    email: str | None = None,
) -> str:
    """Insert or update a student. Returns the canonical (lowercased) id.

    GitHub usernames are case-insensitive but case-preserving — we always store
    the lowercase form as the PK and keep the original case in display_name.
    """
    sid = username.lower()
    _t(db, "students").insert(
        {
            "id": sid,
            "display_name": display_name or username,
            "discord_id": discord_id,
            "email": email,
        },
        pk="id",
        replace=True,
    )
    return sid


def upsert_repo(
    db: Database,
    *,
    id: str,
    assignment_id: str,
    student_id: str,
    default_branch: str = "main",
    last_push_at: str | None = None,
    last_commit_sha: str | None = None,
    ci_status: str | None = None,
) -> None:
    _t(db, "repos").insert(
        {
            "id": id,
            "assignment_id": assignment_id,
            "student_id": student_id.lower(),
            "default_branch": default_branch,
            "last_push_at": last_push_at,
            "last_commit_sha": last_commit_sha,
            "ci_status": ci_status,
            "synced_at": now_iso(),
            "archived_at": None,
        },
        pk="id",
        replace=True,
    )


def archive_repo(db: Database, repo_id: str) -> None:
    """Mark a repo as archived (soft delete). Preserves event history."""
    _t(db, "repos").update(repo_id, {"archived_at": now_iso()})


def list_active_repos(db: Database, assignment_id: str | None = None) -> list[dict[str, Any]]:
    """Return non-archived repos, optionally filtered to one assignment."""
    where = "archived_at IS NULL"
    args: tuple[Any, ...] = ()
    if assignment_id is not None:
        where += " AND assignment_id = ?"
        args = (assignment_id,)
    return list(_t(db, "repos").rows_where(where, args, order_by="id"))


# ---------- Events ----------


def insert_event(
    db: Database,
    *,
    op_id: str,
    op_class: str,
    op_args_json: str,
    tacon_version: str,
    repo_id: str,
    student_id: str,
    status: str,
    commit_sha: str | None = None,
    applied_blob_sha: str | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
    applied_at: str | None = None,
    pr_number: int | None = None,
    pr_branch: str | None = None,
) -> str:
    eid = new_uuid()
    _t(db, "events").insert(
        {
            "id": eid,
            "op_id": op_id,
            "op_class": op_class,
            "op_args_json": op_args_json,
            "tacon_version": tacon_version,
            "repo_id": repo_id,
            "student_id": student_id.lower(),
            "status": status,
            "commit_sha": commit_sha,
            "applied_blob_sha": applied_blob_sha,
            "error_class": error_class,
            "error_message": error_message,
            "created_at": now_iso(),
            "applied_at": applied_at,
            "rolled_back_at": None,
            "pr_number": pr_number,
            "pr_branch": pr_branch,
        }
    )
    return eid


def update_event_status(
    db: Database,
    event_id: str,
    *,
    status: str,
    commit_sha: str | None = None,
    applied_blob_sha: str | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
    applied_at: str | None = None,
    rolled_back_at: str | None = None,
) -> None:
    updates: dict[str, Any] = {"status": status}
    if commit_sha is not None:
        updates["commit_sha"] = commit_sha
    if applied_blob_sha is not None:
        updates["applied_blob_sha"] = applied_blob_sha
    if error_class is not None:
        updates["error_class"] = error_class
    if error_message is not None:
        updates["error_message"] = error_message
    if applied_at is not None:
        updates["applied_at"] = applied_at
    if rolled_back_at is not None:
        updates["rolled_back_at"] = rolled_back_at
    _t(db, "events").update(event_id, updates)


def get_events_by_op(
    db: Database, op_id: str, status: str | Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Return all events for an op_id, optionally filtered by status."""
    if status is None:
        return list(_t(db, "events").rows_where("op_id = ?", (op_id,), order_by="created_at"))
    if isinstance(status, str):
        return list(
            _t(db, "events").rows_where(
                "op_id = ? AND status = ?", (op_id, status), order_by="created_at"
            )
        )
    statuses = list(status)
    placeholders = ",".join("?" * len(statuses))
    return list(
        _t(db, "events").rows_where(
            f"op_id = ? AND status IN ({placeholders})",
            (op_id, *statuses),
            order_by="created_at",
        )
    )


def get_op_class_for_op_id(db: Database, op_id: str) -> str | None:
    """Look up the op_class for a given op_id (used by rollback dispatch)."""
    row = next(
        _t(db, "events").rows_where("op_id = ?", (op_id,), select="op_class", limit=1),
        None,
    )
    return row["op_class"] if row else None


def get_schema_version(db: Database) -> int:
    row = next(_t(db, "meta").rows_where("key = ?", ("schema_version",), limit=1), None)
    return int(row["value"]) if row else 0
