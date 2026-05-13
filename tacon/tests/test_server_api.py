"""Tests for the v0.3 GUI Step 2 API surface (``tacon.server`` endpoints).

Covered:
  - GET  /api/ops                       (shape + per-op flags)
  - POST /api/ops/{name}/plan           (happy path + 404 + 422 + 503)
  - POST /api/ops/{name}/apply          (op_id roundtrip + 409 single-flight)
  - POST /api/ops/{op_id}/rollback      (dispatch by op_id + 404)
  - GET  /api/events?op_id=...&...      (SSE stream + cursor + keep-alive)

The /healthz + host-allowlist + port-picker tests stay in test_server.py;
the API endpoints get their own module so the file doesn't grow past
500 lines.

Strategy: spin up FastAPI's TestClient against a `create_app(...)` with
a tmp DB and a mock-`gh_factory`. PyGithub is never touched. The mock
client's `get_repo()` returns a `fake_repo` whose `create_file` /
`get_contents` / etc. return the values each test scenario requires —
the same pattern used by `tests/ops/test_add_file.py`.

SSE tests spawn uvicorn in a daemon thread on a free 127.0.0.1 port
and hit it with a normal httpx.Client. httpx.ASGITransport buffers the
entire response before yielding the first chunk (defeating the point
of SSE), and TestClient.stream() blocks indefinitely on an unbounded
stream — uvicorn-in-a-thread is the only test harness that actually
exercises the wire protocol.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from github import UnknownObjectException
from sqlite_utils import Database

from tacon.db import open_db, upsert_assignment, upsert_repo, upsert_student
from tacon.server import create_app

LOCALHOST_HEADERS = {"host": "localhost"}


# ---------- shared fixtures ----------


@pytest.fixture
def api_db_path(tmp_path: Path) -> Path:
    """Seeded tacon DB with 3 repos. Used by every test that drives plan/apply."""
    db_path = tmp_path / "tacon-server.db"
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
    for user in ("Alice", "bob", "carol"):
        sid = upsert_student(db, username=user)
        upsert_repo(
            db,
            id=f"cs101/{user.lower()}-hw3",
            assignment_id="asn-1",
            student_id=sid,
        )
    return db_path


def _content_file(sha: str) -> MagicMock:
    cf = MagicMock(name="ContentFile")
    cf.sha = sha
    return cf


def _commit(sha: str) -> MagicMock:
    c = MagicMock(name="Commit")
    c.sha = sha
    return c


def _missing_file_exc() -> UnknownObjectException:
    return UnknownObjectException(404, {"message": "Not Found"}, {})


@pytest.fixture
def mock_gh_factory() -> tuple[MagicMock, MagicMock, Any]:
    """A gh_factory that hands out one mock client with a configurable repo.

    Returns the tuple ``(gh_client, fake_repo, factory)`` so tests can
    poke at the mock between requests (e.g. flip ``get_contents`` from
    returning a content-file to raising 404).
    """
    fake_repo = MagicMock(name="Repository")
    fake_repo.default_branch = "main"
    gh = MagicMock(name="RateLimitedClient")
    gh.get_repo.return_value = fake_repo
    gh.call.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    return gh, fake_repo, lambda: gh


@pytest.fixture
def api_client(
    api_db_path: Path, mock_gh_factory: tuple[MagicMock, MagicMock, Any]
) -> Iterator[tuple[TestClient, MagicMock, MagicMock]]:
    """A TestClient on an app wired to the seeded DB + the mock GH client."""
    _gh, _repo, factory = mock_gh_factory
    app = create_app(db_path=api_db_path, gh_factory=factory)
    # TestClient must be used as a context manager so the lifespan
    # startup hook (orphan-sweep) actually runs — pytest tests that drop
    # straight into requests against `TestClient(app)` without a `with`
    # block silently skip startup. We yield the mocks so tests can
    # configure them per-scenario.
    with TestClient(app) as client:
        yield client, _gh, _repo


# ---------- GET /api/ops ----------


def test_get_api_ops_lists_all_registered_ops(api_client) -> None:
    client, _gh, _repo = api_client
    response = client.get("/api/ops", headers=LOCALHOST_HEADERS)
    assert response.status_code == 200
    body = response.json()
    names = {op["name"] for op in body["ops"]}
    # The full v0.2 op set should be present (5 ops, see design doc).
    assert names == {
        "add-file",
        "delete-file",
        "add-ci-workflow",
        "fix-ci-workflow",
        "add-branch-protection",
    }


def test_get_api_ops_includes_arg_schema_per_op(api_client) -> None:
    """The form generator on the SPA side consumes ``arg_schema`` directly,
    so the shape matters: must be a valid JSON Schema with `properties`."""
    client, _gh, _repo = api_client
    body = client.get("/api/ops", headers=LOCALHOST_HEADERS).json()
    by_name = {op["name"]: op for op in body["ops"]}

    add_file = by_name["add-file"]
    assert add_file["op_class"] == "AddFile"
    assert add_file["supports_via_pr"] is True
    assert add_file["supports_rollback"] is True
    schema = add_file["arg_schema"]
    assert "properties" in schema
    assert set(schema["properties"]).issuperset({"path", "content", "via_pr"})


def test_get_api_ops_does_not_require_db_or_gh(tmp_path: Path) -> None:
    """A bare create_app() with no DB and no GH factory still serves /api/ops.

    Useful for the dev story where the user is still on the settings page
    (Step 4) configuring auth — the home screen's card list shouldn't
    fail to render."""
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/ops", headers=LOCALHOST_HEADERS)
        assert response.status_code == 200
        assert len(response.json()["ops"]) >= 1


# ---------- POST /api/ops/{name}/plan ----------


def test_post_plan_runs_op_plan_and_returns_diff(api_client) -> None:
    """Happy path: the file doesn't exist anywhere → 3 unblocked diffs."""
    client, _gh, fake_repo = api_client
    fake_repo.get_contents.side_effect = _missing_file_exc()

    response = client.post(
        "/api/ops/add-file/plan",
        headers=LOCALHOST_HEADERS,
        json={"path": "STARTER.md", "content": "hello\n"},
    )
    assert response.status_code == 200
    diff = response.json()
    assert diff["op_class"] == "add_file"
    assert len(diff["per_repo"]) == 3
    assert all(not r["blocked"] for r in diff["per_repo"])
    repo_ids = {r["repo_id"] for r in diff["per_repo"]}
    assert repo_ids == {
        "cs101/alice-hw3",
        "cs101/bob-hw3",
        "cs101/carol-hw3",
    }


def test_post_plan_404_for_unknown_op(api_client) -> None:
    client, _gh, _repo = api_client
    response = client.post(
        "/api/ops/not-an-op/plan",
        headers=LOCALHOST_HEADERS,
        json={},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_op"


def test_post_plan_422_when_required_arg_missing(api_client) -> None:
    """Pydantic ValidationError surfaces as a 422 with issue details so
    the GUI can highlight bad form fields."""
    client, _gh, _repo = api_client
    response = client.post(
        "/api/ops/add-file/plan",
        headers=LOCALHOST_HEADERS,
        json={"path": "X"},  # missing 'content'
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert any(issue["loc"][-1] == "content" for issue in detail["issues"])


def test_post_plan_422_when_op_specific_validation_fails(api_client) -> None:
    """AddCIWorkflow validates YAML at construction time. Bad YAML must
    surface as 422 (form-fixable), not 500."""
    client, _gh, _repo = api_client
    response = client.post(
        "/api/ops/add-ci-workflow/plan",
        headers=LOCALHOST_HEADERS,
        json={"name": "ci", "content": "this is not valid: yaml :: :::"},
    )
    assert response.status_code == 422


def test_post_plan_503_when_db_unconfigured(tmp_path: Path) -> None:
    """A server with no DB configured (settings page hasn't run yet) must
    return 503, not crash."""
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/ops/add-file/plan",
            headers=LOCALHOST_HEADERS,
            json={"path": "x", "content": "y"},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "db_unconfigured"


# ---------- POST /api/ops/{name}/apply ----------


def _await_op_completion(db: Database, op_id: str, *, timeout: float = 5.0) -> None:
    """Poll the events table until every row for op_id is in a terminal status."""
    terminal = {"applied", "failed", "skipped", "rolled_back"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = list(db["events"].rows_where("op_id = ?", (op_id,)))
        if rows and all(r["status"] in terminal for r in rows):
            return
        time.sleep(0.05)
    raise AssertionError(f"op {op_id} did not complete within {timeout}s")


def _await_op_status(
    db: Database, op_id: str, expected_status: str, *, timeout: float = 5.0
) -> None:
    """Poll until every event for op_id matches ``expected_status``.

    Used after rollback where the same op_id's events transition from
    ``applied`` → ``rolled_back`` and the generic _await_op_completion
    would short-circuit on the prior applied state.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = list(db["events"].rows_where("op_id = ?", (op_id,)))
        if rows and all(r["status"] == expected_status for r in rows):
            return
        time.sleep(0.05)
    raise AssertionError(
        f"op {op_id} never reached {expected_status!r} within {timeout}s"
    )


def test_post_apply_returns_op_id_synchronously_then_writes_events(
    api_client, api_db_path: Path
) -> None:
    """The apply endpoint pre-generates op_id and returns it immediately;
    the background task writes per-repo events."""
    client, _gh, fake_repo = api_client
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }

    response = client.post(
        "/api/ops/add-file/apply",
        headers=LOCALHOST_HEADERS,
        json={"path": "X.md", "content": "x"},
    )
    assert response.status_code == 200
    op_id = response.json()["op_id"]
    assert op_id  # non-empty
    assert response.json()["phase"] == "apply"

    db = open_db(api_db_path)
    _await_op_completion(db, op_id)
    rows = list(db["events"].rows_where("op_id = ?", (op_id,)))
    assert len(rows) == 3
    assert all(r["status"] == "applied" for r in rows)
    assert all(r["commit_sha"] == "c1" for r in rows)


def test_post_apply_returns_op_id_within_50ms(api_client) -> None:
    """Design-doc acceptance: 'POST apply returns op_id within 50ms.'

    The work happens in a background task; the response should be fast.
    A blocking apply that takes ~50ms-per-repo×3-repos would blow this.
    """
    client, _gh, fake_repo = api_client
    # Make get_contents slow so blocking apply would clearly exceed 50ms.
    # The endpoint must NOT wait on it — the background task does.
    def _slow_get_contents(*args, **kwargs):
        time.sleep(0.2)
        raise _missing_file_exc()
    fake_repo.get_contents.side_effect = _slow_get_contents
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }

    start = time.monotonic()
    response = client.post(
        "/api/ops/add-file/apply",
        headers=LOCALHOST_HEADERS,
        json={"path": "Slow.md", "content": "x"},
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    assert response.status_code == 200
    # 50ms is the design budget; we give a generous 500ms ceiling so the
    # test isn't flaky on a busy CI runner but still catches "endpoint
    # waits for the full apply to finish" regressions.
    assert elapsed_ms < 500, f"apply endpoint blocked for {elapsed_ms:.0f}ms"


def test_post_apply_returns_409_when_another_op_in_flight(
    api_client, api_db_path: Path
) -> None:
    """Single-flight: concurrent apply attempts get 409 with the in-flight op_id."""
    client, _gh, fake_repo = api_client

    def _slow_get_contents(*args, **kwargs):
        time.sleep(0.3)
        raise _missing_file_exc()
    fake_repo.get_contents.side_effect = _slow_get_contents
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }

    first = client.post(
        "/api/ops/add-file/apply",
        headers=LOCALHOST_HEADERS,
        json={"path": "A.md", "content": "x"},
    )
    assert first.status_code == 200
    first_op_id = first.json()["op_id"]

    # Second apply hits while the first is still running.
    second = client.post(
        "/api/ops/add-file/apply",
        headers=LOCALHOST_HEADERS,
        json={"path": "B.md", "content": "y"},
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "op_in_progress"
    assert detail["op_id"] == first_op_id
    assert detail["phase"] == "apply"

    db = open_db(api_db_path)
    _await_op_completion(db, first_op_id)


def test_post_apply_after_completion_releases_the_lock(
    api_client, api_db_path: Path
) -> None:
    """Once the background task wraps up, the next apply must succeed."""
    client, _gh, fake_repo = api_client
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }

    first = client.post(
        "/api/ops/add-file/apply",
        headers=LOCALHOST_HEADERS,
        json={"path": "First.md", "content": "x"},
    )
    assert first.status_code == 200
    db = open_db(api_db_path)
    _await_op_completion(db, first.json()["op_id"])

    second = client.post(
        "/api/ops/add-file/apply",
        headers=LOCALHOST_HEADERS,
        json={"path": "Second.md", "content": "y"},
    )
    assert second.status_code == 200, second.text


# ---------- POST /api/ops/{op_id}/rollback ----------


def test_post_rollback_404_for_unknown_op_id(api_client) -> None:
    client, _gh, _repo = api_client
    response = client.post(
        "/api/ops/00000000-0000-0000-0000-000000000000/rollback",
        headers=LOCALHOST_HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_op_id"


def test_post_rollback_dispatches_by_op_class_in_events_table(
    api_client, api_db_path: Path
) -> None:
    """After an apply, hit /rollback/{op_id} and verify it runs to completion."""
    client, _gh, fake_repo = api_client

    # First: apply.
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    apply_resp = client.post(
        "/api/ops/add-file/apply",
        headers=LOCALHOST_HEADERS,
        json={"path": "Roll.md", "content": "x"},
    )
    op_id = apply_resp.json()["op_id"]
    db = open_db(api_db_path)
    _await_op_completion(db, op_id)

    # Reset side_effect so rollback sees the file present.
    fake_repo.get_contents.side_effect = None
    fake_repo.get_contents.return_value = _content_file("blob-1")
    fake_repo.delete_file.return_value = {"commit": _commit("revert-1")}

    rollback_resp = client.post(
        f"/api/ops/{op_id}/rollback",
        headers=LOCALHOST_HEADERS,
    )
    assert rollback_resp.status_code == 200
    assert rollback_resp.json()["op_id"] == op_id
    assert rollback_resp.json()["phase"] == "rollback"

    _await_op_status(db, op_id, "rolled_back")
    rows = list(db["events"].rows_where("op_id = ?", (op_id,)))
    statuses = {r["status"] for r in rows}
    assert statuses == {"rolled_back"}


# ---------- GET /api/events (SSE) ----------


def _parse_sse_messages(text: str) -> list[dict[str, Any]]:
    """Crude SSE parser — returns one dict per event with `event`, `id`, `data`."""
    messages = []
    block: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if block:
                messages.append(block)
            block = {}
            continue
        if line.startswith(":"):
            block.setdefault("comment", line[1:].strip())
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            block[key.strip()] = value.lstrip()
    if block:
        messages.append(block)
    return messages


def _pick_test_port() -> int:
    """Ask the kernel for a free port. Same trick as stdlib's test_socket."""
    import socket as _sock

    with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _UvicornInThread:
    """Run a uvicorn server in a daemon thread for SSE tests.

    httpx.ASGITransport doesn't deliver SSE chunks as they arrive (it
    buffers the entire response), and the sync TestClient.stream() hangs
    on unbounded streams. So we spin up a real uvicorn on a free
    127.0.0.1 port and hit it with a normal httpx.Client. Slower than
    in-process but it's the only way to exercise the real wire protocol.
    """

    def __init__(self, app: FastAPI) -> None:
        import asyncio as _async
        import threading

        import uvicorn

        self.port = _pick_test_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        def _run() -> None:
            loop = _async.new_event_loop()
            _async.set_event_loop(loop)
            try:
                loop.run_until_complete(self._server.serve())
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)

    def __enter__(self) -> _UvicornInThread:
        self._thread.start()
        # Wait for the listener to come up. uvicorn sets `started=True`
        # once the socket is open.
        deadline = time.monotonic() + 5.0
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self._server.started:
            raise RuntimeError("uvicorn failed to start within 5s")
        return self

    def __exit__(self, *args: Any) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _read_sse_events(
    base_url: str,
    path_and_query: str,
    *,
    target_event_count: int,
    timeout: float = 6.0,
) -> list[dict[str, Any]]:
    """Pull from the SSE endpoint until ``target_event_count`` events arrive.

    httpx's read-timeout on each chunk bounds total wall time when the
    feed goes idle (keepalives are 5s apart). We close the response
    early to signal disconnect; the server's generator picks that up
    via its receive channel and shuts down cleanly.
    """
    buf: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout)) as client, client.stream(
        "GET",
        base_url + path_and_query,
        headers={"host": "localhost"},
    ) as resp:
        assert resp.status_code == 200, resp.text
        try:
            for line in resp.iter_lines():
                buf.append(line)
                messages = _parse_sse_messages("\n".join(buf) + "\n\n")
                # Trailing "\n\n" forces a partial block to be appended.
                # Filter out events without a `data` field — those
                # are mid-stream partials, not complete deliveries.
                event_messages = [
                    m
                    for m in messages
                    if m.get("event") == "event" and "data" in m
                ]
                if len(event_messages) >= target_event_count:
                    return [json.loads(m["data"]) for m in event_messages]
        except httpx.ReadTimeout:
            messages = _parse_sse_messages("\n".join(buf) + "\n\n")
            event_messages = [m for m in messages if m.get("event") == "event"]
            raise AssertionError(
                f"SSE feed produced {len(event_messages)} events "
                f"(wanted {target_event_count}). Lines so far: {buf[:30]}"
            ) from None
    raise AssertionError("SSE stream ended unexpectedly")


def test_get_events_streams_per_repo_events_for_an_op(
    api_db_path: Path, mock_gh_factory
) -> None:
    """Apply 3 repos, then read the SSE feed and verify the 3 final-state
    events arrive (one row per repo; the runner UPDATEs in place so the
    feed sees terminal status rather than the planned→applied transition).
    """
    _gh, fake_repo, factory = mock_gh_factory
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    app = create_app(db_path=api_db_path, gh_factory=factory)
    with _UvicornInThread(app) as server:
        with httpx.Client(timeout=3.0) as client:
            r = client.post(
                f"{server.url}/api/ops/add-file/apply",
                headers=LOCALHOST_HEADERS,
                json={"path": "SSE.md", "content": "x"},
            )
            op_id = r.json()["op_id"]
            db = open_db(api_db_path)
            _await_op_completion(db, op_id)

        payloads = _read_sse_events(
            server.url,
            f"/api/events?op_id={op_id}&last_event_id=0",
            target_event_count=3,
        )

    seen_repos = {p["repo_id"] for p in payloads}
    assert seen_repos == {
        "cs101/alice-hw3",
        "cs101/bob-hw3",
        "cs101/carol-hw3",
    }
    assert all(p["op_id"] == op_id for p in payloads)
    sample = payloads[0]
    for required in ("event_id", "cursor", "op_id", "phase", "repo_id", "status"):
        assert required in sample
    assert all(p["phase"] == "apply" for p in payloads)


def test_get_events_resumes_from_last_event_id_without_duplicates(
    api_db_path: Path, mock_gh_factory
) -> None:
    """Design-doc acceptance: 'Reconnect with last_event_id=N resumes from N+1.'"""
    _gh, fake_repo, factory = mock_gh_factory
    fake_repo.get_contents.side_effect = _missing_file_exc()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    app = create_app(db_path=api_db_path, gh_factory=factory)
    with _UvicornInThread(app) as server:
        with httpx.Client(timeout=3.0) as client:
            op_id = client.post(
                f"{server.url}/api/ops/add-file/apply",
                headers=LOCALHOST_HEADERS,
                json={"path": "Cursor.md", "content": "x"},
            ).json()["op_id"]
            db = open_db(api_db_path)
            _await_op_completion(db, op_id)

        all_events = _read_sse_events(
            server.url,
            f"/api/events?op_id={op_id}&last_event_id=0",
            target_event_count=3,
        )
        midpoint_cursor = all_events[1]["cursor"]
        expected_remaining = [
            e["cursor"] for e in all_events if e["cursor"] > midpoint_cursor
        ]
        assert expected_remaining  # meaningful only if events live past midpoint

        resumed = _read_sse_events(
            server.url,
            f"/api/events?op_id={op_id}&last_event_id={midpoint_cursor}",
            target_event_count=len(expected_remaining),
        )

    resumed_cursors = [e["cursor"] for e in resumed]
    assert all(c > midpoint_cursor for c in resumed_cursors)
    assert resumed_cursors == expected_remaining
