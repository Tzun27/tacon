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
