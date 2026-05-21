"""Tests for the v0.3 GUI SPA static-file mount in ``tacon.server``.

Step 3 wires ``GET /`` to serve the built Vite SPA from
``tacon/web/dist/``. These cover both states: dist present (serve
index.html) and dist absent (a friendly build-hint page rather than a
bare 404). The dist-resolution path is monkeypatched so the tests don't
depend on whether ``pnpm build`` has actually run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tacon.server as server


def test_root_serves_index_html_when_dist_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A built dist/ → GET / returns its index.html as text/html."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>tacon</title><div id=root></div>"
    )
    monkeypatch.setattr(server, "_spa_dist_dir", lambda: dist)

    client = TestClient(server.create_app())
    response = client.get("/", headers={"host": "localhost"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "id=root" in response.text


def test_root_shows_build_hint_when_dist_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dist/ → GET / returns the friendly one-line build hint."""
    monkeypatch.setattr(server, "_spa_dist_dir", lambda: tmp_path / "never-built")

    client = TestClient(server.create_app())
    response = client.get("/", headers={"host": "localhost"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "cd tacon/web && pnpm install && pnpm build" in response.text


def test_healthz_still_served_when_spa_is_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catch-all SPA mount at / must not shadow /healthz or /api/*."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    monkeypatch.setattr(server, "_spa_dist_dir", lambda: dist)

    client = TestClient(server.create_app())
    healthz = client.get("/healthz", headers={"host": "localhost"})
    ops = client.get("/api/ops", headers={"host": "localhost"})

    assert healthz.status_code == 200
    assert healthz.json()["status"] == "ok"
    assert ops.status_code == 200


def test_spa_dist_dir_points_into_the_package() -> None:
    """The real resolver targets tacon/web/dist inside the package."""
    dist = server._spa_dist_dir()
    assert dist.name == "dist"
    assert dist.parent.name == "web"
    assert dist.parent.parent.name == "tacon"
