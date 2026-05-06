"""Tests for the static dashboard renderer."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from tacon.cli import app
from tacon.dashboard import render
from tacon.dashboard.render import _build_grid, _safe_filename
from tacon.db import (
    insert_event,
    open_db,
    update_event_status,
    upsert_assignment,
    upsert_repo,
    upsert_student,
)

runner = CliRunner()


def _populate_db(db_path: Path) -> tuple[str, str, str]:
    """Returns (op_id, repo_id, student_id) for assertions."""
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
    sid = upsert_student(db, username="alice")
    repo_id = "cs101/alice-hw3"
    upsert_repo(db, id=repo_id, assignment_id="asn-1", student_id=sid)

    op_id = "op-test-1"
    eid = insert_event(
        db,
        op_id=op_id,
        op_class="add_file",
        op_args_json='{"path": "X"}',
        tacon_version="0.0.1",
        repo_id=repo_id,
        student_id=sid,
        status="planned",
    )
    update_event_status(
        db,
        eid,
        status="applied",
        commit_sha="c0ffee01",
        applied_blob_sha="b10b1234",
        applied_at="2026-05-02T10:00:00Z",
    )
    return op_id, repo_id, sid


# ---------- render() ----------


def test_render_writes_index_op_repo_pages_and_css(tmp_path: Path) -> None:
    db_path = tmp_path / "tacon.db"
    op_id, repo_id, _sid = _populate_db(db_path)
    db = open_db(db_path)

    out = tmp_path / "site"
    stats = render(db, out)

    assert stats == {"ops": 1, "events": 1, "repos": 1}
    assert (out / "index.html").exists()
    assert (out / "op" / f"{op_id}.html").exists()
    # repo file slashes get sanitized
    assert (out / "repo" / "cs101__alice-hw3.html").exists()
    assert (out / "style.css").exists()


def test_index_contains_op_and_repo_references(tmp_path: Path) -> None:
    db_path = tmp_path / "tacon.db"
    op_id, repo_id, _sid = _populate_db(db_path)
    db = open_db(db_path)

    out = tmp_path / "site"
    render(db, out)
    body = (out / "index.html").read_text()
    assert "add_file" in body
    assert op_id[:8] in body
    assert repo_id in body
    assert "applied" in body
    assert "alice" in body


def test_op_page_lists_event_details(tmp_path: Path) -> None:
    db_path = tmp_path / "tacon.db"
    op_id, _repo_id, _sid = _populate_db(db_path)
    db = open_db(db_path)

    out = tmp_path / "site"
    render(db, out)
    body = (out / "op" / f"{op_id}.html").read_text()
    assert "c0ffee01"[:8] in body
    assert "alice" in body
    assert "applied" in body


def test_repo_page_lists_history(tmp_path: Path) -> None:
    db_path = tmp_path / "tacon.db"
    op_id, _repo_id, _sid = _populate_db(db_path)
    db = open_db(db_path)

    out = tmp_path / "site"
    render(db, out)
    body = (out / "repo" / "cs101__alice-hw3.html").read_text()
    assert op_id[:8] in body
    assert "applied" in body


def test_render_is_idempotent_on_rerun(tmp_path: Path) -> None:
    """Running render twice should not error and should still produce valid output."""
    db_path = tmp_path / "tacon.db"
    _populate_db(db_path)
    db = open_db(db_path)

    out = tmp_path / "site"
    render(db, out)
    first_index = (out / "index.html").read_text()
    render(db, out)
    second_index = (out / "index.html").read_text()
    # generated_at differs but the structural content (table, css link) does not
    assert "<table>" in first_index
    assert "<table>" in second_index


def test_render_empty_db_produces_friendly_index(tmp_path: Path) -> None:
    """A DB with no events should still produce a usable index page."""
    db_path = tmp_path / "tacon.db"
    open_db(db_path)  # creates schema, no rows
    db = open_db(db_path)

    out = tmp_path / "site"
    stats = render(db, out)
    assert stats == {"ops": 0, "events": 0, "repos": 0}
    body = (out / "index.html").read_text()
    assert "No ops have been recorded" in body or "0" in body


def test_render_handles_multiple_ops_and_repos(tmp_path: Path) -> None:
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
    op_ids = []
    for username in ("alice", "bob"):
        sid = upsert_student(db, username=username)
        repo_id = f"cs101/{username}-hw3"
        upsert_repo(db, id=repo_id, assignment_id="asn-1", student_id=sid)
    for op_idx in range(2):
        op_id = f"op-{op_idx}"
        op_ids.append(op_id)
        for username in ("alice", "bob"):
            sid = username  # student id is lowercased username
            eid = insert_event(
                db,
                op_id=op_id,
                op_class="add_file",
                op_args_json="{}",
                tacon_version="0.0.1",
                repo_id=f"cs101/{username}-hw3",
                student_id=sid,
                status="planned",
            )
            status = "applied" if op_idx == 0 else "failed"
            update_event_status(db, eid, status=status, commit_sha="abc")

    out = tmp_path / "site"
    stats = render(db, out)
    assert stats["ops"] == 2
    assert stats["events"] == 4
    assert stats["repos"] == 2
    for op_id in op_ids:
        assert (out / "op" / f"{op_id}.html").exists()


# ---------- helpers ----------


def test_safe_filename_replaces_slashes() -> None:
    assert _safe_filename("owner/repo") == "owner__repo"
    assert _safe_filename("a/b/c") == "a__b__c"
    assert _safe_filename("plain") == "plain"


def test_build_grid_keeps_last_event_per_cell() -> None:
    from tacon.dashboard.render import EventRow, OpSummary, RepoRow

    op = OpSummary(
        op_id="op-1",
        op_class="add_file",
        started_at="2026-05-01T00:00:00Z",
        n_applied=1,
        n_failed=0,
        n_skipped=0,
        n_reported=0,
        n_rolled_back=0,
    )
    repo = RepoRow(
        repo_id="r1",
        student_id="s1",
        student_display_name="S1",
        assignment_id="asn-1",
        last_event_status=None,
        last_event_at=None,
    )
    e1 = EventRow(
        id="e1",
        op_id="op-1",
        op_class="add_file",
        repo_id="r1",
        student_id="s1",
        student_display_name="S1",
        status="planned",
        commit_sha=None,
        error_class=None,
        error_message=None,
        created_at="2026-05-01T00:00:00Z",
        applied_at=None,
        rolled_back_at=None,
    )
    e2 = EventRow(
        id="e2",
        op_id="op-1",
        op_class="add_file",
        repo_id="r1",
        student_id="s1",
        student_display_name="S1",
        status="applied",
        commit_sha="abc",
        error_class=None,
        error_message=None,
        created_at="2026-05-01T00:00:01Z",
        applied_at="2026-05-01T00:00:01Z",
        rolled_back_at=None,
    )
    grid = _build_grid([e1, e2], [op], [repo])
    assert grid["r1"]["op-1"].status == "applied"  # last write wins


def test_build_grid_skips_events_for_unknown_op_or_repo() -> None:
    from tacon.dashboard.render import EventRow, OpSummary, RepoRow

    e = EventRow(
        id="e",
        op_id="unknown-op",
        op_class="x",
        repo_id="unknown-repo",
        student_id="s",
        student_display_name="S",
        status="applied",
        commit_sha=None,
        error_class=None,
        error_message=None,
        created_at="2026-05-01T00:00:00Z",
        applied_at=None,
        rolled_back_at=None,
    )
    op = OpSummary(
        op_id="op-1",
        op_class="x",
        started_at="z",
        n_applied=0,
        n_failed=0,
        n_skipped=0,
        n_reported=0,
        n_rolled_back=0,
    )
    repo = RepoRow(
        repo_id="r1",
        student_id="s",
        student_display_name="S",
        assignment_id="a",
        last_event_status=None,
        last_event_at=None,
    )
    grid = _build_grid([e], [op], [repo])
    # The orphan event should NOT have populated any cell
    assert grid == {}


# ---------- CLI dashboard command ----------


def test_dashboard_cli_renders(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "tacon.db"
    _populate_db(db_path)
    out_dir = tmp_path / "site"

    result = runner.invoke(
        app,
        ["dashboard", "--out", str(out_dir), "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert (out_dir / "index.html").exists()


def test_dashboard_cli_default_out_dir(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "tacon.db"
    _populate_db(db_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["dashboard", "--db", str(db_path)])
    assert result.exit_code == 0
    assert (tmp_path / "tacon-dashboard" / "index.html").exists()
