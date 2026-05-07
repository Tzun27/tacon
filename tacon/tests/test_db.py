"""Tests for tacon.db schema + accessors."""

from __future__ import annotations

from pathlib import Path

from sqlite_utils import Database

from tacon.db import (
    SCHEMA_VERSION,
    archive_repo,
    get_events_by_op,
    get_op_class_for_op_id,
    get_schema_version,
    insert_event,
    list_active_repos,
    open_db,
    update_event_status,
    upsert_assignment,
    upsert_repo,
    upsert_student,
)


def test_init_db_creates_all_tables(tmp_path: Path) -> None:
    db = open_db(tmp_path / "fresh.db")
    expected = {"meta", "assignments", "students", "repos", "events", "interactions"}
    assert expected.issubset(set(db.table_names()))


def test_init_db_writes_schema_version(tmp_path: Path) -> None:
    db = open_db(tmp_path / "fresh.db")
    assert get_schema_version(db) == SCHEMA_VERSION


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "fresh.db"
    open_db(p)
    db = open_db(p)  # second open shouldn't crash or duplicate rows
    assert get_schema_version(db) == SCHEMA_VERSION
    assert db["meta"].count == 1


def test_upsert_student_lowercases_id(tmp_db: Database) -> None:
    sid = upsert_student(tmp_db, username="Alice")
    assert sid == "alice"
    row = tmp_db["students"].get("alice")
    assert row["display_name"] == "Alice"


def test_upsert_student_mixed_case_collapses_to_one_row(tmp_db: Database) -> None:
    upsert_student(tmp_db, username="Alice")
    upsert_student(tmp_db, username="alice")
    upsert_student(tmp_db, username="ALICE")
    assert tmp_db["students"].count == 1


def test_upsert_assignment_then_repo(tmp_db: Database) -> None:
    upsert_assignment(
        tmp_db,
        id="asn-1",
        classroom_id="cls-1",
        title="HW1",
        slug="hw1",
        starter_repo=None,
        created_at="2026-05-01T00:00:00Z",
    )
    sid = upsert_student(tmp_db, username="bob")
    upsert_repo(tmp_db, id="cs101/hw1-bob", assignment_id="asn-1", student_id=sid)
    row = tmp_db["repos"].get("cs101/hw1-bob")
    assert row["student_id"] == "bob"
    assert row["default_branch"] == "main"
    assert row["archived_at"] is None


def test_archive_repo_sets_archived_at(tmp_db: Database, seed_repos) -> None:
    archive_repo(tmp_db, "cs101/alice-hw3")
    row = tmp_db["repos"].get("cs101/alice-hw3")
    assert row["archived_at"] is not None


def test_list_active_repos_excludes_archived(tmp_db: Database, seed_repos) -> None:
    archive_repo(tmp_db, "cs101/alice-hw3")
    active = list_active_repos(tmp_db, assignment_id="asn-1")
    ids = {r["id"] for r in active}
    assert "cs101/alice-hw3" not in ids
    assert ids == {"cs101/bob-hw3", "cs101/carol-hw3"}


def test_list_active_repos_filters_by_assignment(tmp_db: Database, seed_repos) -> None:
    upsert_assignment(
        tmp_db,
        id="asn-2",
        classroom_id="cls-1",
        title="HW4",
        slug="hw4",
        starter_repo=None,
        created_at="2026-05-02T00:00:00Z",
    )
    sid = upsert_student(tmp_db, username="dave")
    upsert_repo(tmp_db, id="cs101/dave-hw4", assignment_id="asn-2", student_id=sid)

    asn1 = list_active_repos(tmp_db, assignment_id="asn-1")
    asn2 = list_active_repos(tmp_db, assignment_id="asn-2")
    assert len(asn1) == 3
    assert len(asn2) == 1


def test_insert_and_update_event(tmp_db: Database, seed_repos) -> None:
    eid = insert_event(
        tmp_db,
        op_id="op-x",
        op_class="add_file",
        op_args_json='{"path":"X"}',
        tacon_version="0.0.1",
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="planned",
    )
    update_event_status(
        tmp_db, eid, status="applied", commit_sha="abc1234", applied_blob_sha="blobsha1"
    )
    events = get_events_by_op(tmp_db, "op-x")
    assert len(events) == 1
    assert events[0]["status"] == "applied"
    assert events[0]["commit_sha"] == "abc1234"
    assert events[0]["applied_blob_sha"] == "blobsha1"


def test_get_events_by_op_filters_by_status(tmp_db: Database, seed_repos) -> None:
    insert_event(
        tmp_db,
        op_id="op-y",
        op_class="add_file",
        op_args_json="{}",
        tacon_version="0.0.1",
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="applied",
    )
    insert_event(
        tmp_db,
        op_id="op-y",
        op_class="add_file",
        op_args_json="{}",
        tacon_version="0.0.1",
        repo_id="cs101/bob-hw3",
        student_id="bob",
        status="failed",
    )
    applied = get_events_by_op(tmp_db, "op-y", status="applied")
    failed = get_events_by_op(tmp_db, "op-y", status="failed")
    assert len(applied) == 1
    assert len(failed) == 1
    assert applied[0]["repo_id"] == "cs101/alice-hw3"


def test_get_events_by_op_filters_by_status_iterable(tmp_db: Database, seed_repos) -> None:
    insert_event(
        tmp_db,
        op_id="op-z",
        op_class="add_file",
        op_args_json="{}",
        tacon_version="0.0.1",
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="applied",
    )
    insert_event(
        tmp_db,
        op_id="op-z",
        op_class="add_file",
        op_args_json="{}",
        tacon_version="0.0.1",
        repo_id="cs101/bob-hw3",
        student_id="bob",
        status="skipped",
    )
    matched = get_events_by_op(tmp_db, "op-z", status=["applied", "skipped"])
    assert len(matched) == 2


def test_get_op_class_for_op_id(tmp_db: Database, seed_repos) -> None:
    insert_event(
        tmp_db,
        op_id="op-q",
        op_class="add_file",
        op_args_json="{}",
        tacon_version="0.0.1",
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="planned",
    )
    assert get_op_class_for_op_id(tmp_db, "op-q") == "add_file"
    assert get_op_class_for_op_id(tmp_db, "op-missing") is None


# ---------- schema v2 (events.pr_number / events.pr_branch) ----------


def test_schema_v2_columns_present_on_fresh_db(tmp_path: Path) -> None:
    """A new DB is built straight at v2: events table includes pr_number + pr_branch."""
    db = open_db(tmp_path / "fresh.db")
    cols = {c.name for c in db["events"].columns}
    assert "pr_number" in cols
    assert "pr_branch" in cols


def test_insert_event_persists_pr_fields(tmp_db: Database, seed_repos) -> None:
    """Direct-write events store NULL; via-pr events store the pr metadata."""
    eid_direct = insert_event(
        tmp_db,
        op_id="op-direct",
        op_class="add_file",
        op_args_json='{"path":"X"}',
        tacon_version="0.2.0",
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="applied",
    )
    eid_pr = insert_event(
        tmp_db,
        op_id="op-pr",
        op_class="add_file",
        op_args_json='{"path":"Y","via_pr":true}',
        tacon_version="0.2.0",
        repo_id="cs101/bob-hw3",
        student_id="bob",
        status="applied",
        pr_number=42,
        pr_branch="tacon/add-file-deadbeef",
    )
    direct = tmp_db["events"].get(eid_direct)
    via_pr = tmp_db["events"].get(eid_pr)
    assert direct["pr_number"] is None
    assert direct["pr_branch"] is None
    assert via_pr["pr_number"] == 42
    assert via_pr["pr_branch"] == "tacon/add-file-deadbeef"


def test_v1_db_migrates_to_v2_in_place(tmp_path: Path) -> None:
    """Open an old v1-shaped DB, run init_db, and confirm the migration adds the
    pr columns + bumps schema_version without losing existing rows."""
    p = tmp_path / "v1.db"
    raw = Database(str(p))
    # Hand-roll the v1 schema by approximating what tacon 0.1.x wrote: events
    # table without pr_number/pr_branch + meta(schema_version=1).
    raw["meta"].create({"key": str, "value": str}, pk="key")
    raw["meta"].insert({"key": "schema_version", "value": "1"})
    raw["events"].create(
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
        },
        pk="id",
    )
    raw["events"].insert(
        {
            "id": "legacy-1",
            "op_id": "old-op",
            "op_class": "add_file",
            "op_args_json": "{}",
            "tacon_version": "0.1.0",
            "repo_id": "cs101/alice-hw3",
            "student_id": "alice",
            "status": "applied",
            "commit_sha": "deadbeef",
            "applied_blob_sha": "blob1",
            "error_class": None,
            "error_message": None,
            "created_at": "2026-05-01T00:00:00Z",
            "applied_at": "2026-05-01T00:00:01Z",
            "rolled_back_at": None,
        }
    )
    raw.conn.close()

    # Now open via tacon's open_db, which runs init_db (and the migration).
    migrated = open_db(p)
    cols = {c.name for c in migrated["events"].columns}
    assert "pr_number" in cols
    assert "pr_branch" in cols
    assert get_schema_version(migrated) == SCHEMA_VERSION
    legacy = migrated["events"].get("legacy-1")
    assert legacy["status"] == "applied"
    assert legacy["pr_number"] is None
    assert legacy["pr_branch"] is None


def test_migration_is_re_runnable(tmp_path: Path) -> None:
    """Running open_db twice on a v2 DB is a no-op (idempotency)."""
    p = tmp_path / "v2.db"
    open_db(p)
    db2 = open_db(p)  # second run
    cols = {c.name for c in db2["events"].columns}
    assert "pr_number" in cols
    assert "pr_branch" in cols
    assert get_schema_version(db2) == SCHEMA_VERSION
    # meta has exactly one schema_version row
    rows = list(db2["meta"].rows_where("key = ?", ("schema_version",)))
    assert len(rows) == 1


def test_migration_partial_state_recovers(tmp_path: Path) -> None:
    """Crash scenario: pr_number was added but the meta row was never bumped.
    The next init_db run should observe the columns exist + finish bumping.
    """
    p = tmp_path / "partial.db"
    raw = Database(str(p))
    raw["meta"].create({"key": str, "value": str}, pk="key")
    raw["meta"].insert({"key": "schema_version", "value": "1"})
    raw["events"].create(
        {"id": str, "op_id": str, "op_class": str, "op_args_json": str,
         "tacon_version": str, "repo_id": str, "student_id": str, "status": str,
         "created_at": str, "pr_number": int},
        pk="id",
    )
    raw.conn.close()

    migrated = open_db(p)
    cols = {c.name for c in migrated["events"].columns}
    assert "pr_number" in cols
    assert "pr_branch" in cols
    assert get_schema_version(migrated) == SCHEMA_VERSION


# ---------- schema v3 (events.prior_state_json) ----------


def test_schema_v3_column_present_on_fresh_db(tmp_path: Path) -> None:
    """A new DB is built straight at v3: events table includes prior_state_json."""
    db = open_db(tmp_path / "fresh.db")
    cols = {c.name for c in db["events"].columns}
    assert "prior_state_json" in cols


def test_insert_event_persists_prior_state_json(
    tmp_db: Database, seed_repos
) -> None:
    """Survey events leave prior_state_json NULL; admin-write events store JSON."""
    eid_survey = insert_event(
        tmp_db,
        op_id="op-survey",
        op_class="add_branch_protection",
        op_args_json='{"branch":"main"}',
        tacon_version="0.2.0",
        repo_id="cs101/alice-hw3",
        student_id="alice",
        status="reported",
    )
    eid_write = insert_event(
        tmp_db,
        op_id="op-write",
        op_class="add_branch_protection",
        op_args_json='{"branch":"main","rule":{"required_approving_review_count":1}}',
        tacon_version="0.2.0",
        repo_id="cs101/bob-hw3",
        student_id="bob",
        status="applied",
        prior_state_json='{"required_approving_review_count":2,"enforce_admins":true}',
    )
    survey = tmp_db["events"].get(eid_survey)
    write = tmp_db["events"].get(eid_write)
    assert survey["prior_state_json"] is None
    assert write["prior_state_json"] == (
        '{"required_approving_review_count":2,"enforce_admins":true}'
    )


def test_v2_db_migrates_to_v3_in_place(tmp_path: Path) -> None:
    """Open a v2-shaped DB, run init_db, confirm prior_state_json is added
    and the schema version bumps without touching existing rows."""
    p = tmp_path / "v2.db"
    raw = Database(str(p))
    raw["meta"].create({"key": str, "value": str}, pk="key")
    raw["meta"].insert({"key": "schema_version", "value": "2"})
    # v2 events table — has pr_number/pr_branch but NOT prior_state_json.
    raw["events"].create(
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
    )
    raw["events"].insert(
        {
            "id": "v2-row",
            "op_id": "old-op",
            "op_class": "add_file",
            "op_args_json": "{}",
            "tacon_version": "0.2.0",
            "repo_id": "cs101/alice-hw3",
            "student_id": "alice",
            "status": "applied",
            "commit_sha": "deadbeef",
            "applied_blob_sha": "blob1",
            "error_class": None,
            "error_message": None,
            "created_at": "2026-05-06T00:00:00Z",
            "applied_at": "2026-05-06T00:00:01Z",
            "rolled_back_at": None,
            "pr_number": None,
            "pr_branch": None,
        }
    )
    raw.conn.close()

    migrated = open_db(p)
    cols = {c.name for c in migrated["events"].columns}
    assert "prior_state_json" in cols
    assert get_schema_version(migrated) == SCHEMA_VERSION
    legacy = migrated["events"].get("v2-row")
    assert legacy["status"] == "applied"
    assert legacy["pr_number"] is None
    assert legacy["prior_state_json"] is None


def test_v3_migration_is_re_runnable(tmp_path: Path) -> None:
    """Running open_db twice on a v3 DB is a no-op."""
    p = tmp_path / "v3.db"
    open_db(p)
    db2 = open_db(p)
    cols = {c.name for c in db2["events"].columns}
    assert "prior_state_json" in cols
    assert get_schema_version(db2) == SCHEMA_VERSION
    rows = list(db2["meta"].rows_where("key = ?", ("schema_version",)))
    assert len(rows) == 1
    assert rows[0]["value"] == str(SCHEMA_VERSION)


def test_v3_partial_state_recovers(tmp_path: Path) -> None:
    """prior_state_json was added but meta wasn't bumped past v2.
    Next open_db should observe the column + finish bumping."""
    p = tmp_path / "v3-partial.db"
    raw = Database(str(p))
    raw["meta"].create({"key": str, "value": str}, pk="key")
    raw["meta"].insert({"key": "schema_version", "value": "2"})
    raw["events"].create(
        {
            "id": str, "op_id": str, "op_class": str, "op_args_json": str,
            "tacon_version": str, "repo_id": str, "student_id": str,
            "status": str, "created_at": str,
            "pr_number": int, "pr_branch": str,
            "prior_state_json": str,
        },
        pk="id",
    )
    raw.conn.close()

    migrated = open_db(p)
    cols = {c.name for c in migrated["events"].columns}
    assert "prior_state_json" in cols
    assert get_schema_version(migrated) == SCHEMA_VERSION
