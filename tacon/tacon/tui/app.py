"""Textual app: per-op summary + per-repo event drill-down.

Layout:
  ┌─ Ops ──────────────────┐  ┌─ Events for selected op ────────────┐
  │ op_id  op_class  …     │  │ repo  student  status  commit  err  │
  │ ...                    │  │ ...                                  │
  └────────────────────────┘  └──────────────────────────────────────┘
  Footer: r=refresh  q=quit  enter=open repo url  /=filter

Move with arrow keys. Enter on a repo row prints its URL in the status
bar. The app talks to the DB only on mount + manual refresh — no
background polling.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from tacon import __version__
from tacon.db import open_db

if TYPE_CHECKING:
    from sqlite_utils import Database


class TaconApp(App[int]):
    """Read-only browser over the tacon events table."""

    CSS = """
    Screen {
        layout: vertical;
    }
    Horizontal {
        height: 1fr;
    }
    #ops {
        width: 50%;
        border: round $primary;
        margin: 0 1 0 0;
    }
    #events {
        width: 50%;
        border: round $primary;
    }
    #status {
        height: 1;
        background: $primary-darken-1;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "quit", "Quit", show=False),
    ]

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.db_path = db_path
        self._db: Database | None = None
        # Tests can read this without depending on Static internals across
        # Textual versions.
        self.last_status: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield DataTable(id="ops", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="events", cursor_type="row", zebra_stripes=True)
        yield Static(f"tacon v{__version__}  ·  db: {self.db_path}", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "tacon"
        self.sub_title = "events browser"
        self._db = open_db(self.db_path)

        ops_table = self.query_one("#ops", DataTable)
        ops_table.add_columns("op_id", "op_class", "started", "applied", "failed", "skipped")

        events_table = self.query_one("#events", DataTable)
        events_table.add_columns("repo", "student", "status", "commit", "error")

        self._refresh_ops()
        self.last_status = f"tacon v{__version__}  ·  db: {self.db_path}"

    def action_refresh(self) -> None:
        self._refresh_ops()
        self._set_status("refreshed")

    def action_quit(self) -> None:  # type: ignore[override]
        self.exit(0)

    # ---------- DB helpers ----------

    def _refresh_ops(self) -> None:
        ops_table = self.query_one("#ops", DataTable)
        ops_table.clear()
        rows = self._fetch_op_summaries()
        for row in rows:
            ops_table.add_row(
                row["op_id"][:8],
                row["op_class"],
                row["started_at"] or "",
                str(row["n_applied"] or 0),
                str(row["n_failed"] or 0),
                str(row["n_skipped"] or 0),
                key=row["op_id"],
            )
        if rows:
            ops_table.move_cursor(row=0)
            self._refresh_events_for(rows[0]["op_id"])
        else:
            self.query_one("#events", DataTable).clear()

    def _refresh_events_for(self, op_id: str) -> None:
        events_table = self.query_one("#events", DataTable)
        events_table.clear()
        for ev in self._fetch_events_for_op(op_id):
            events_table.add_row(
                ev["repo_id"],
                ev["student_id"],
                ev["status"],
                (ev["commit_sha"] or "")[:8],
                ev.get("error_class") or "",
            )

    def _fetch_op_summaries(self) -> list[dict[str, Any]]:
        assert self._db is not None
        return list(
            self._db.query(
                """
                SELECT op_id, op_class, MIN(created_at) AS started_at,
                       SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END) AS n_applied,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS n_failed,
                       SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS n_skipped
                FROM events
                GROUP BY op_id, op_class
                ORDER BY started_at DESC
                """
            )
        )

    def _fetch_events_for_op(self, op_id: str) -> Iterable[dict[str, Any]]:
        assert self._db is not None
        return self._db.query(
            """
            SELECT repo_id, student_id, status, commit_sha, error_class, error_message,
                   created_at
            FROM events
            WHERE op_id = ?
            ORDER BY created_at ASC
            """,
            [op_id],
        )

    # ---------- event handlers ----------

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        # When the user moves the cursor in the ops table, refresh the events
        # pane to show that op's events.
        if event.data_table.id != "ops":
            return
        if event.row_key is None or event.row_key.value is None:
            return
        self._refresh_events_for(event.row_key.value)

    # ---------- helpers ----------

    def _set_status(self, message: str) -> None:
        rendered = f"tacon v{__version__}  ·  db: {self.db_path}  ·  {message}"
        self.last_status = rendered
        status = self.query_one("#status", Static)
        status.update(rendered)
