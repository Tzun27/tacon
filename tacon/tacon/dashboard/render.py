"""Render the tacon dashboard to a directory of static HTML files.

Layout written by render():
  out/index.html            — class overview: ops + per-repo grid + timeline
  out/op/<op_id>.html       — per-op detail: every event in that op
  out/repo/<repo_id>.html   — per-repo timeline (one page per repo)
  out/style.css             — shared stylesheet (no JS)

The renderer reads the DB but does not write to it. Idempotent: running
twice produces byte-identical output (modulo the generated_at timestamp,
which is rendered into a single section near the top).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader, select_autoescape

from tacon import __version__
from tacon.db import now_iso

if TYPE_CHECKING:
    from sqlite_utils import Database


# ---------- view-model dataclasses (decouple templates from raw rows) ----------


@dataclass
class OpSummary:
    op_id: str
    op_class: str
    started_at: str
    n_applied: int
    n_failed: int
    n_skipped: int
    n_reported: int
    n_rolled_back: int


@dataclass
class EventRow:
    id: str
    op_id: str
    op_class: str
    repo_id: str
    student_id: str
    student_display_name: str
    status: str
    commit_sha: str | None
    error_class: str | None
    error_message: str | None
    created_at: str
    applied_at: str | None
    rolled_back_at: str | None


@dataclass
class RepoRow:
    repo_id: str
    student_id: str
    student_display_name: str
    assignment_id: str
    last_event_status: str | None
    last_event_at: str | None


@dataclass
class CellState:
    """Per-(repo, op) cell for the grid view."""

    status: str  # 'applied' | 'failed' | 'skipped' | ...
    error_class: str | None
    commit_sha: str | None


# ---------- public entrypoint ----------


def render(db: Database, out_dir: Path) -> dict[str, int]:
    """Render the dashboard. Returns a small stats dict for the CLI to print."""
    out_dir.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=PackageLoader("tacon.dashboard", "templates"),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    op_summaries = _collect_op_summaries(db)
    events = _collect_events(db)
    repos = _collect_repos(db)
    grid = _build_grid(events, op_summaries, repos)

    context: dict[str, Any] = {
        "tacon_version": __version__,
        "generated_at": now_iso(),
        "ops": op_summaries,
        "events": events,
        "repos": repos,
        "grid_columns": op_summaries,  # column order = ops oldest first
        "grid": grid,
    }

    # index
    (out_dir / "index.html").write_text(
        env.get_template("index.html").render(**context), encoding="utf-8"
    )

    # The per-op and per-repo templates take their own (filtered) `events`
    # list, so we strip the global one out of the shared context before merging.
    sub_context = {k: v for k, v in context.items() if k != "events"}

    # per-op pages
    op_dir = out_dir / "op"
    op_dir.mkdir(exist_ok=True)
    for op in op_summaries:
        op_events = [e for e in events if e.op_id == op.op_id]
        (op_dir / f"{op.op_id}.html").write_text(
            env.get_template("op.html").render(op=op, events=op_events, **sub_context),
            encoding="utf-8",
        )

    # per-repo pages
    repo_dir = out_dir / "repo"
    repo_dir.mkdir(exist_ok=True)
    for repo in repos:
        repo_events = [e for e in events if e.repo_id == repo.repo_id]
        (repo_dir / f"{_safe_filename(repo.repo_id)}.html").write_text(
            env.get_template("repo.html").render(
                repo=repo, events=repo_events, **sub_context
            ),
            encoding="utf-8",
        )

    # stylesheet
    (out_dir / "style.css").write_text(_STYLESHEET, encoding="utf-8")

    return {
        "ops": len(op_summaries),
        "events": len(events),
        "repos": len(repos),
    }


# ---------- DB readers ----------


def _collect_op_summaries(db: Database) -> list[OpSummary]:
    rows = list(
        db.query(
            """
            SELECT op_id, op_class, MIN(created_at) AS started_at,
                   SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END) AS n_applied,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS n_failed,
                   SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS n_skipped,
                   SUM(CASE WHEN status = 'reported' THEN 1 ELSE 0 END) AS n_reported,
                   SUM(CASE WHEN status = 'rolled_back' THEN 1 ELSE 0 END) AS n_rolled_back
            FROM events
            GROUP BY op_id, op_class
            ORDER BY started_at ASC
            """
        )
    )
    return [
        OpSummary(
            op_id=r["op_id"],
            op_class=r["op_class"],
            started_at=r["started_at"],
            n_applied=r["n_applied"] or 0,
            n_failed=r["n_failed"] or 0,
            n_skipped=r["n_skipped"] or 0,
            n_reported=r["n_reported"] or 0,
            n_rolled_back=r["n_rolled_back"] or 0,
        )
        for r in rows
    ]


def _collect_events(db: Database) -> list[EventRow]:
    rows = list(
        db.query(
            """
            SELECT e.id, e.op_id, e.op_class, e.repo_id, e.student_id,
                   COALESCE(s.display_name, e.student_id) AS student_display_name,
                   e.status, e.commit_sha, e.error_class, e.error_message,
                   e.created_at, e.applied_at, e.rolled_back_at
            FROM events e
            LEFT JOIN students s ON s.id = e.student_id
            ORDER BY e.created_at ASC, e.id ASC
            """
        )
    )
    return [
        EventRow(
            id=r["id"],
            op_id=r["op_id"],
            op_class=r["op_class"],
            repo_id=r["repo_id"],
            student_id=r["student_id"],
            student_display_name=r["student_display_name"],
            status=r["status"],
            commit_sha=r["commit_sha"],
            error_class=r["error_class"],
            error_message=r["error_message"],
            created_at=r["created_at"],
            applied_at=r["applied_at"],
            rolled_back_at=r["rolled_back_at"],
        )
        for r in rows
    ]


def _collect_repos(db: Database) -> list[RepoRow]:
    rows = list(
        db.query(
            """
            SELECT r.id AS repo_id, r.student_id, r.assignment_id,
                   COALESCE(s.display_name, r.student_id) AS student_display_name,
                   (SELECT status FROM events
                      WHERE repo_id = r.id ORDER BY created_at DESC LIMIT 1) AS last_event_status,
                   (SELECT created_at FROM events
                      WHERE repo_id = r.id ORDER BY created_at DESC LIMIT 1) AS last_event_at
            FROM repos r
            LEFT JOIN students s ON s.id = r.student_id
            WHERE r.archived_at IS NULL
            ORDER BY r.id ASC
            """
        )
    )
    return [
        RepoRow(
            repo_id=r["repo_id"],
            student_id=r["student_id"],
            student_display_name=r["student_display_name"],
            assignment_id=r["assignment_id"],
            last_event_status=r["last_event_status"],
            last_event_at=r["last_event_at"],
        )
        for r in rows
    ]


def _build_grid(
    events: Iterable[EventRow],
    ops: Iterable[OpSummary],
    repos: Iterable[RepoRow],
) -> dict[str, dict[str, CellState]]:
    """Returns {repo_id: {op_id: CellState}} — sparse where no event exists."""
    cells: dict[str, dict[str, CellState]] = defaultdict(dict)
    op_ids = {o.op_id for o in ops}
    repo_ids = {r.repo_id for r in repos}
    for e in events:
        if e.op_id not in op_ids or e.repo_id not in repo_ids:
            continue
        # Last write wins — events are ordered chronologically so we end with
        # the most recent state for each (repo, op) cell.
        cells[e.repo_id][e.op_id] = CellState(
            status=e.status,
            error_class=e.error_class,
            commit_sha=e.commit_sha,
        )
    return cells


# ---------- helpers ----------


def _safe_filename(s: str) -> str:
    """Make a string safe to use as a filename. Replaces '/' with '__'."""
    return s.replace("/", "__")


_STYLESHEET = """\
:root {
  --bg: #0d1117;
  --panel: #161b22;
  --text: #c9d1d9;
  --muted: #8b949e;
  --accent: #58a6ff;
  --good: #3fb950;
  --warn: #d29922;
  --bad: #f85149;
  --neutral: #8b949e;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header {
  background: var(--panel);
  padding: 16px 24px;
  border-bottom: 1px solid #30363d;
  display: flex;
  align-items: baseline;
  gap: 16px;
}
header h1 { margin: 0; font-size: 20px; }
header .sub { color: var(--muted); font-size: 12px; }
main { padding: 24px; max-width: 1400px; margin: 0 auto; }
section { margin-bottom: 32px; }
section h2 { font-size: 16px; border-bottom: 1px solid #30363d; padding-bottom: 4px; }
table { border-collapse: collapse; width: 100%; }
th, td {
  text-align: left;
  padding: 6px 10px;
  border-bottom: 1px solid #21262d;
  font-size: 13px;
  vertical-align: top;
}
th { color: var(--muted); font-weight: 500; }
tr:hover td { background: #1c2128; }
.status { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 11px; }
.status-applied { background: rgba(63, 185, 80, 0.15); color: var(--good); }
.status-failed { background: rgba(248, 81, 73, 0.15); color: var(--bad); }
.status-skipped { background: rgba(210, 153, 34, 0.15); color: var(--warn); }
.status-reported { background: rgba(88, 166, 255, 0.15); color: var(--accent); }
.status-rolled_back { background: rgba(139, 148, 158, 0.15); color: var(--neutral); }
.status-planned { background: rgba(139, 148, 158, 0.15); color: var(--muted); }
.grid { display: grid; gap: 2px; }
.cell {
  width: 18px; height: 18px; border-radius: 3px;
  background: #21262d;
  display: inline-block;
}
.cell.s-applied { background: var(--good); }
.cell.s-failed { background: var(--bad); }
.cell.s-skipped { background: var(--warn); }
.cell.s-reported { background: var(--accent); }
.cell.s-rolled_back { background: var(--neutral); }
.timeline-row { font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 12px; }
.muted { color: var(--muted); }
footer { padding: 24px; text-align: center; color: var(--muted); font-size: 12px; }
"""
