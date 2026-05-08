"""tacon dashboard: static HTML rendering of the events table.

Read-only. Pure rendering: takes a tacon DB and writes HTML to an output
directory. Designed to be hosted on GitHub Pages, S3, or any static
host; no JavaScript or backend required.
"""

from __future__ import annotations

from tacon.dashboard.publish import (
    PublishError,
    PublishResult,
    publish_to_gh_pages,
)
from tacon.dashboard.render import render

__all__ = [
    "PublishError",
    "PublishResult",
    "publish_to_gh_pages",
    "render",
]
