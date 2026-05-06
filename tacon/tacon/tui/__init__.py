"""tacon TUI: a Textual app for inspecting events + ops.

Offline read-only — wraps the same DB the CLI writes to. Designed for
"I just ran a batch op, what happened?" — plus drill-down per repo.
"""

from __future__ import annotations

from tacon.tui.app import TaconApp

__all__ = ["TaconApp"]
