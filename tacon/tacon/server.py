"""FastAPI app for ``tacon serve`` — the v0.3 local web GUI.

Step 1 shipped the skeleton (app factory, host-header allowlist,
free-port picker, /healthz). Step 2 adds the API surface that the SPA
talks to:

  GET  /api/ops                       — list registered ops + JSON Schema
  POST /api/ops/{name}/plan           — validate args, run plan(), return Diff
  POST /api/ops/{name}/apply          — single-flight apply, returns op_id
  POST /api/ops/{op_id}/rollback      — single-flight rollback
  GET  /api/events?op_id=X&...        — SSE feed of per-repo progress

The SPA + static-file mount lands in Step 3. The settings page lands in
Step 4. Step 6 (AddFile spine) is what consumes these endpoints
end-to-end with diff grid + live feed UX.

Module-level state:
    * The host-header allowlist + port-picker are unchanged from Step 1.
    * Per-app state (DB path, GH client factory, single-flight lock,
      in-flight op tracker) lives on ``app.state`` so tests can spin up
      fresh apps without process-global side effects.

Concurrency model (single-flight):
    apply + rollback both mutate per-repo state. v0.3 enforces one
    long-running op per server process. The lock is an asyncio.Lock()
    held for the duration of the background task. Endpoints check
    ``lock.locked()`` before scheduling; concurrent attempts return 409
    with the in-flight op_id in the body so the GUI can show a banner
    that links to the live feed.

Synchronous PyGithub calls run in a thread executor via
``loop.run_in_executor(None, ...)`` so the event loop stays responsive
for the SSE feed.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
import webbrowser
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tacon.ops._apply_runner import new_op_id

if TYPE_CHECKING:
    from fastapi import FastAPI

    from tacon.github_client import RateLimitedClient


# DNS-rebinding defense. A page on the public internet can't open a
# real http://localhost:<port>/ connection from a victim's browser
# because of CORS / private-network access checks — but it CAN make a
# request whose Host header is its own domain pointing at 127.0.0.1
# via DNS rebinding. We require the Host header to be in this set.
_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "[::1]",
    }
)

# Port range to try when --port is omitted. Picked to avoid the common
# Vite (5173) / FastAPI tutorial (8000) / Jupyter (8888) ranges and to
# stay above the ephemeral-port window on most systems.
DEFAULT_PORT_RANGE: tuple[int, int] = (5734, 5740)


# SSE tuning. 100ms tick keeps p99 latency well inside the design doc's
# 500ms budget (halved for headroom). 5s idle keep-alive is enough to
# defeat the typical 60s reverse-proxy idle timeout without spamming.
_SSE_POLL_INTERVAL_SEC: float = 0.1
_SSE_KEEPALIVE_INTERVAL_SEC: float = 5.0
# Orphan sweep: a row stuck in 'in-progress' (introduced by the GUI
# background-task layer; not used by the CLI) for more than 60s implies
# the prior server process died mid-apply. We rewrite to 'failed'.
_ORPHAN_AGE_SECONDS: int = 60


class PortInUseError(RuntimeError):
    """The requested or default port range had no free port to bind."""


def pick_port(
    *,
    explicit: int | None = None,
    host: str = "127.0.0.1",
    port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
) -> int:
    """Return a port to bind, honoring ``--port`` if set.

    ``explicit`` (the ``--port`` flag) wins and raises
    :class:`PortInUseError` with a clear message when the chosen port
    is already in use. Without ``--port``, walks ``port_range`` and
    returns the first free port; raises :class:`PortInUseError` when
    every port in the range is taken.
    """
    if explicit is not None:
        if _port_free(host, explicit):
            return explicit
        raise PortInUseError(
            f"port {explicit} on {host} is already in use; "
            f"pick a different one with --port or let tacon auto-select"
        )
    start, end = port_range
    for port in range(start, end + 1):
        if _port_free(host, port):
            return port
    raise PortInUseError(
        f"no free port in range {start}-{end} on {host}; "
        f"pass --port to pick one explicitly"
    )


def _port_free(host: str, port: int) -> bool:
    """True if (host, port) is bindable right now.

    Uses a probe socket with SO_REUSEADDR off so we don't get a false
    positive for ports in TIME_WAIT.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ---------- app config + state ----------


@dataclass
class _InFlight:
    """One in-flight (or just-completed) op tracked on app.state.

    ``task`` is None when the slot is still being set up (between op_id
    generation and ``asyncio.create_task``) so the apply endpoint can
    publish the op_id atomically before the executor work begins.
    """

    op_id: str
    op_name: str
    phase: str  # 'apply' | 'rollback'
    started_at: float
    task: asyncio.Task[None] | None = None


@dataclass
class _AppState:
    """Per-app runtime state. Lives on ``app.state.tacon``.

    Stored as a single dataclass (rather than a flurry of attrs on
    ``app.state``) so the field set is discoverable and tests can
    construct/inspect it directly.

    Concurrency: ``in_flight is not None`` is the single-flight lock
    signal. The check-and-set in :func:`_try_claim_lock` runs inside one
    synchronous code path (no ``await``), so under a single-process
    uvicorn event loop it's atomic. Running multiple workers would
    require a DB-level lock instead.
    """

    db_path: Path | None
    gh_factory: Callable[[], RateLimitedClient] | None
    in_flight: _InFlight | None = None


def _default_gh_factory(
    token: str | None, rate_per_sec: float
) -> Callable[[], RateLimitedClient]:
    """Build a factory that lazily constructs the RateLimitedClient.

    Lazy so importing the module doesn't try to read a token from env /
    gh CLI / keyring. The factory is invoked per-request inside the
    handler that needs GitHub access, not per-app.
    """

    def factory() -> RateLimitedClient:
        from tacon.github_client import RateLimitedClient as _Client

        return _Client(token=token, rate_per_sec=rate_per_sec)

    return factory


# ---------- app factory ----------


def create_app(
    *,
    db_path: Path | None = None,
    token: str | None = None,
    rate_per_sec: float = 3.0,
    gh_factory: Callable[[], RateLimitedClient] | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    All deps are lazy-imported so importing ``tacon.server`` itself stays
    cheap for users who only run the CLI (and don't have the [gui] extra
    installed).

    ``db_path`` and ``gh_factory`` are optional so unit tests can spin up
    an app for /healthz / GET /api/ops without seeding either. Endpoints
    that need them return 503 with a clear message.

    ``gh_factory`` (test seam) overrides ``token`` + ``rate_per_sec`` when
    provided. Production wiring goes through ``token`` + ``rate_per_sec``;
    tests inject a mock factory to avoid touching real GitHub.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, HTTPException, Request, status
    from fastapi.responses import JSONResponse

    from tacon import __version__

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Startup/shutdown hooks via FastAPI's lifespan context manager.

        Startup: orphan-sweep stale 'in-progress' rows on the DB so a
        prior crashed server doesn't leave events permanently in flight.
        Shutdown: cancel any still-running background task so uvicorn
        can exit cleanly on Ctrl-C without hanging on the executor.
        """
        _sweep_orphan_events(app.state.tacon)
        yield
        in_flight = app.state.tacon.in_flight
        if in_flight is not None and in_flight.task is not None:
            in_flight.task.cancel()

    app = FastAPI(
        title="tacon",
        version=__version__,
        # Disable the auto-mounted docs UIs until we have real routes
        # worth documenting. Re-enable in a later step.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    app.state.tacon = _AppState(
        db_path=db_path,
        gh_factory=gh_factory or _default_gh_factory(token, rate_per_sec),
    )

    @app.middleware("http")
    async def host_header_allowlist(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Block requests whose Host header isn't on the allowlist.

        Strips an optional ``:<port>`` suffix before comparing so that
        ``Host: localhost:5734`` still matches ``localhost``.
        Defense-in-depth against DNS rebinding (the user's browser is
        the trust boundary, not the network).
        """
        raw_host = request.headers.get("host", "")
        host_only = raw_host.split(":", 1)[0] if ":" in raw_host else raw_host
        # IPv6 hosts come through with brackets; preserve them.
        if raw_host.startswith("["):
            host_only = raw_host.split("]", 1)[0] + "]"
        if host_only not in _ALLOWED_HOSTS:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "forbidden_host",
                    "detail": (
                        f"Host header {raw_host!r} not on the allowlist. "
                        "tacon serve only accepts requests addressed to "
                        "localhost / 127.0.0.1 / [::1]."
                    ),
                },
            )
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe. Used by tests + the CLI's startup wait."""
        return {"status": "ok", "version": __version__}

    # ---------- GET /api/ops ----------

    @app.get("/api/ops")
    async def api_list_ops() -> dict[str, list[dict[str, Any]]]:
        """Enumerate registered ops + their JSON Schema for form rendering.

        Read-only; needs neither DB nor a GH token, so it works on a
        bare ``create_app()`` (useful for tests + the dev story where
        the user is on the settings page before configuring auth).
        """
        from tacon.ops import get_op_class, list_ops

        ops_payload: list[dict[str, Any]] = []
        for name in list_ops():
            cls = get_op_class(name)
            ops_payload.append(
                {
                    "name": name,
                    "op_class": cls.__name__,
                    "arg_schema": cls.arg_schema().model_json_schema(),
                    "supports_via_pr": cls.supports_via_pr,
                    "supports_rollback": cls.supports_rollback,
                }
            )
        return {"ops": ops_payload}

    # ---------- POST /api/ops/{name}/plan ----------

    @app.post("/api/ops/{name}/plan")
    async def api_plan(name: str, request: Request) -> dict[str, Any]:
        """Validate body against the op's arg_schema, run plan(), return Diff."""
        state: _AppState = app.state.tacon
        db = _open_db_or_503(state)
        gh = _build_gh_or_503(state)
        op = _validate_and_build_op(name, await request.json())

        loop = asyncio.get_running_loop()
        diff = await loop.run_in_executor(None, op.plan, db, gh)
        return _serialize_diff(diff)

    # ---------- POST /api/ops/{name}/apply ----------

    @app.post("/api/ops/{name}/apply")
    async def api_apply(name: str, request: Request) -> dict[str, Any]:
        """Kick off a background apply. Returns op_id; client subscribes to SSE."""
        state: _AppState = app.state.tacon
        db_path = _db_path_or_503(state)
        # Construct op now so validation errors surface synchronously,
        # not as a silent background-task crash.
        op = _validate_and_build_op(name, await request.json())
        gh = _build_gh_or_503(state)

        existing = _try_claim_lock(state, op_name=name, phase="apply")
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "op_in_progress",
                    "op_id": existing.op_id,
                    "phase": existing.phase,
                    "op_name": existing.op_name,
                },
            )

        op_id = new_op_id()
        assert state.in_flight is not None  # _try_claim_lock set this
        state.in_flight.op_id = op_id

        # Background task: open a fresh DB connection inside the executor
        # so we don't share a sqlite3 Connection across threads.
        async def _run_apply() -> None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _apply_in_executor, db_path, op, op_id, gh
                )
            finally:
                _release_lock(state)

        state.in_flight.task = asyncio.create_task(_run_apply())
        return {"op_id": op_id, "phase": "apply", "op_name": name}

    # ---------- POST /api/ops/{op_id}/rollback ----------

    @app.post("/api/ops/{op_id}/rollback")
    async def api_rollback(op_id: str) -> dict[str, Any]:
        """Reverse a prior apply for the given op_id. Streams via SSE."""
        state: _AppState = app.state.tacon
        db_path = _db_path_or_503(state)
        gh = _build_gh_or_503(state)

        # Resolve op_class so we can dispatch rollback. 404 if the op_id
        # is unknown — caller probably typo'd a uuid.
        op_cls = _resolve_op_class_for_rollback(db_path, op_id)
        op_name = _kebab_for(op_cls.__name__)

        existing = _try_claim_lock(state, op_name=op_name, phase="rollback")
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "op_in_progress",
                    "op_id": existing.op_id,
                    "phase": existing.phase,
                    "op_name": existing.op_name,
                },
            )

        assert state.in_flight is not None
        state.in_flight.op_id = op_id

        async def _run_rollback() -> None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _rollback_in_executor, db_path, op_cls, op_id, gh
                )
            finally:
                _release_lock(state)

        state.in_flight.task = asyncio.create_task(_run_rollback())
        return {"op_id": op_id, "phase": "rollback", "op_name": op_name}

    # ---------- GET /api/events (SSE) ----------

    @app.get("/api/events")
    async def api_events(
        op_id: str, last_event_id: int = 0, phase: str | None = None
    ) -> Any:
        """SSE feed: per-repo events for ``op_id`` since ``last_event_id``.

        ``last_event_id`` is the SQLite ``rowid`` of the last event the
        client received. Client reconnects with the same op_id +
        cursor; server resumes from rowid > last_event_id with no
        duplicate deliveries. Polls every 100ms; emits a ``: keep-alive``
        comment every 5 seconds of idle so reverse proxies don't drop
        the connection.

        Optional ``phase=apply|rollback`` filter — when set, only events
        whose phase matches stream. The phase column doesn't exist in
        the DB; it's derived from the event's status (rolled_back /
        failed-during-rollback => 'rollback', everything else =>
        'apply').
        """
        from sse_starlette.sse import EventSourceResponse

        state: _AppState = app.state.tacon
        db_path = _db_path_or_503(state)

        async def stream() -> AsyncIterator[dict[str, Any]]:
            cursor = last_event_id
            last_emit_at = time.monotonic()
            while True:
                # Read in a thread; sqlite3 connections are per-thread.
                rows = await asyncio.get_running_loop().run_in_executor(
                    None, _read_events_since, db_path, op_id, cursor
                )
                for row in rows:
                    row_phase = _derive_phase(row)
                    if phase is not None and row_phase != phase:
                        cursor = max(cursor, int(row["rowid"]))
                        continue
                    payload = _serialize_event(row, row_phase)
                    cursor = max(cursor, int(row["rowid"]))
                    last_emit_at = time.monotonic()
                    yield {
                        "id": str(cursor),
                        "event": "event",
                        "data": json.dumps(payload),
                    }
                if time.monotonic() - last_emit_at >= _SSE_KEEPALIVE_INTERVAL_SEC:
                    last_emit_at = time.monotonic()
                    yield {
                        "event": "keep-alive",
                        "data": json.dumps({"cursor": cursor}),
                    }
                await asyncio.sleep(_SSE_POLL_INTERVAL_SEC)

        return EventSourceResponse(stream())

    return app


# ---------- support: validation + serialization ----------


def _validate_and_build_op(name: str, raw_body: Any) -> Any:
    """Validate the request body against the op's arg_schema, then build the op.

    Raises HTTPException(404) for unknown op names, HTTPException(422) for
    schema-shape failures (wrong type, missing required) and for the
    per-op semantic checks in :func:`tacon.server_ops.build_op`.
    """
    from fastapi import HTTPException, status
    from pydantic import ValidationError

    from tacon.ops import get_op_class
    from tacon.server_ops import OpBuildError, build_op

    try:
        cls = get_op_class(name)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_op", "name": name, "message": str(exc)},
        ) from exc

    schema_cls = cls.arg_schema()
    try:
        validated = schema_cls.model_validate(raw_body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_failed", "issues": exc.errors()},
        ) from exc

    try:
        return build_op(name, validated.model_dump())
    except OpBuildError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_failed", "message": str(exc)},
        ) from exc


def _serialize_diff(diff: Any) -> dict[str, Any]:
    """Diff → JSON-friendly dict. Mirrors the dataclass field shapes."""
    return {
        "op_class": diff.op_class,
        "op_args": diff.op_args,
        "per_repo": [
            {
                "repo_id": r.repo_id,
                "student_id": r.student_id,
                "summary": r.summary,
                "unified_diff": r.unified_diff,
                "blocked": r.blocked,
                "blocked_reason": r.blocked_reason,
            }
            for r in diff.per_repo
        ],
    }


def _serialize_event(row: dict[str, Any], phase: str) -> dict[str, Any]:
    """One events-table row → an SSE-payload dict.

    Schema documented in the design doc Step 2:
        {event_id, op_id, phase, repo_id, status,
         error_class?, error_message?, commit_sha?, pr_number?}
    The optional fields drop out when None to keep the wire small.
    """
    payload: dict[str, Any] = {
        "event_id": row["id"],
        "cursor": row["rowid"],
        "op_id": row["op_id"],
        "op_class": row["op_class"],
        "phase": phase,
        "repo_id": row["repo_id"],
        "student_id": row["student_id"],
        "status": row["status"],
    }
    for opt in ("error_class", "error_message", "commit_sha", "pr_number"):
        if row.get(opt) is not None:
            payload[opt] = row[opt]
    return payload


def _derive_phase(row: dict[str, Any]) -> str:
    """Best-effort: 'rollback' if the event has been rolled back, else 'apply'.

    Maps the existing status enum onto the design doc's `phase`
    distinguisher. A row that lands as ``rolled_back`` was a rollback
    event; everything else is part of the apply lifecycle (planned →
    applied / failed / skipped).
    """
    if row.get("rolled_back_at"):
        return "rollback"
    if row["status"] == "rolled_back":
        return "rollback"
    return "apply"


# ---------- support: lock + in-flight tracker ----------


def _try_claim_lock(
    state: _AppState, *, op_name: str, phase: str
) -> _InFlight | None:
    """Try to acquire the single-flight slot.

    Returns ``None`` on success (with ``state.in_flight`` populated to a
    placeholder), or the existing :class:`_InFlight` on contention.

    The "lock" is just ``state.in_flight is not None``. Under one
    uvicorn worker the event loop is single-threaded, so the
    check-and-set runs without an intervening ``await`` and is atomic
    against other requests. Multi-worker uvicorn would need a DB-level
    lock — out of scope for v0.3.
    """
    if state.in_flight is not None:
        return state.in_flight
    state.in_flight = _InFlight(
        op_id="",  # filled in by the caller after new_op_id()
        op_name=op_name,
        phase=phase,
        started_at=time.monotonic(),
    )
    return None


def _release_lock(state: _AppState) -> None:
    """Release the single-flight slot after a background task completes.

    Tolerant of repeated calls — the task wrapper always calls this in
    a ``finally`` so a crash doesn't strand the slot.
    """
    state.in_flight = None


# ---------- support: orphan sweep ----------


def _sweep_orphan_events(state: _AppState) -> None:
    """Rewrite stale 'in-progress' rows to 'failed' on startup.

    The CLI never produces ``status='in-progress'`` — that status is
    introduced by the GUI's background-task layer (future steps; not
    yet emitted by Step 2 since apply()'s loop is synchronous within
    its executor). The sweep exists now so a future status=in-progress
    rollout is safe from server-crash orphans.
    """
    if state.db_path is None or not state.db_path.exists():
        return
    from typing import cast

    from sqlite_utils.db import Table

    from tacon.db import open_db

    db = open_db(state.db_path)
    events_table = cast(Table, db["events"])
    rows = list(
        events_table.rows_where(
            "status = ? AND (created_at <= datetime('now', ?))",
            ("in-progress", f"-{_ORPHAN_AGE_SECONDS} seconds"),
        )
    )
    for row in rows:
        events_table.update(
            row["id"],
            {
                "status": "failed",
                "error_class": "server_restart",
                "error_message": "process exited mid-apply",
            },
        )


# ---------- support: state guards ----------


def _open_db_or_503(state: _AppState) -> Any:
    """Open the per-request DB connection or raise 503 if unconfigured."""
    from fastapi import HTTPException, status

    if state.db_path is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "db_unconfigured",
                "message": (
                    "tacon serve has no DB configured. Pass --db <path>, "
                    "set a default classroom via `tacon classroom add ... --default`, "
                    "or set TACON_HOME."
                ),
            },
        )
    from tacon.db import open_db

    return open_db(state.db_path)


def _db_path_or_503(state: _AppState) -> Path:
    """Return the configured DB path or raise 503. Used by endpoints that
    open the DB inside an executor (must pass the path, not the handle).
    """
    from fastapi import HTTPException, status

    if state.db_path is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "db_unconfigured"},
        )
    return state.db_path


def _build_gh_or_503(state: _AppState) -> RateLimitedClient:
    """Build a GH client via the configured factory, or 503 if not set up.

    Wraps token-resolution failures (missing env var / gh CLI / keyring)
    into 503 with a clear message so the settings page can surface them.
    """
    from fastapi import HTTPException, status

    factory = state.gh_factory
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "gh_unconfigured"},
        )
    try:
        return factory()
    except RuntimeError as exc:
        # get_default_token() raises RuntimeError when no token found.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "no_github_token", "message": str(exc)},
        ) from exc


# ---------- support: executors ----------


def _apply_in_executor(
    db_path: Path,
    op: Any,
    op_id: str,
    gh: RateLimitedClient,
) -> None:
    """Run plan() + apply() synchronously in a thread.

    Opens its own DB handle (sqlite3 connections aren't shareable across
    threads). All errors are swallowed and recorded on the events table
    by the op's own apply loop, so we don't need to surface anything
    further to the caller; clients consume status via the SSE feed.
    """
    from tacon.db import open_db

    db = open_db(db_path)
    diff = op.plan(db, gh)
    op.apply(db, gh, diff, confirm=lambda _r: True, op_id=op_id)


def _rollback_in_executor(
    db_path: Path,
    op_cls: type[Any],
    op_id: str,
    gh: RateLimitedClient,
) -> None:
    from tacon.db import open_db

    db = open_db(db_path)
    op_cls.rollback(db, gh, op_id)


# ---------- support: rollback dispatch ----------


def _resolve_op_class_for_rollback(db_path: Path, op_id: str) -> type[Any]:
    """Look up op_class for ``op_id`` and return its registered class.

    404 if the op_id has no events. 422 if the op's class doesn't
    support rollback (e.g. a hypothetical irreversible op).
    """
    from fastapi import HTTPException, status

    from tacon.db import get_op_class_for_op_id, open_db
    from tacon.ops import get_op_class

    db = open_db(db_path)
    op_class_db = get_op_class_for_op_id(db, op_id)
    if op_class_db is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_op_id", "op_id": op_id},
        )
    name = _kebab_for(op_class_db)
    try:
        cls = get_op_class(name)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "op_class_not_registered",
                "op_class": op_class_db,
                "message": str(exc),
            },
        ) from exc
    if not cls.supports_rollback:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "rollback_unsupported", "op_class": op_class_db},
        )
    return cls


def _kebab_for(op_class_or_name: str) -> str:
    """Map ``events.op_class`` (snake_case) → registry name (kebab-case).

    Mirrors the small map in ``tacon.cli.rollback``. Centralized here so
    a future op gets exactly one place to register the mapping.
    """
    mapping = {
        "add_file": "add-file",
        "AddFile": "add-file",
        "delete_file": "delete-file",
        "DeleteFile": "delete-file",
        "add_ci_workflow": "add-ci-workflow",
        "AddCIWorkflow": "add-ci-workflow",
        "fix_ci_workflow": "fix-ci-workflow",
        "FixCIWorkflow": "fix-ci-workflow",
        "add_branch_protection": "add-branch-protection",
        "AddBranchProtection": "add-branch-protection",
    }
    return mapping.get(op_class_or_name, op_class_or_name)


# ---------- support: events query ----------


def _read_events_since(
    db_path: Path, op_id: str, last_rowid: int
) -> list[dict[str, Any]]:
    """Read events for op_id where rowid > last_rowid, ordered by rowid.

    Opens a fresh DB connection on each call so this can run in any
    thread the executor picks. SQLite handles concurrent reads cleanly;
    with WAL mode (default for fresh DBs created via sqlite_utils), the
    write side doesn't block readers either.
    """
    from tacon.db import open_db

    db = open_db(db_path)
    return list(
        db["events"].rows_where(
            "op_id = ? AND rowid > ?",
            (op_id, last_rowid),
            order_by="rowid",
            select="rowid, *",
        )
    )


# ---------- entry point ----------


def serve(
    *,
    port: int | None = None,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    db_path: Path | None = None,
    token: str | None = None,
    rate_per_sec: float = 3.0,
) -> None:
    """Run the GUI server. Blocks until the process is interrupted.

    Picks a free port (honoring ``--port`` if set), starts uvicorn,
    and optionally opens the user's default browser at the served URL.
    The host defaults to ``127.0.0.1`` (loopback only); listening on
    ``0.0.0.0`` is technically allowed but the host-header allowlist
    will reject any request whose Host header isn't in
    ``_ALLOWED_HOSTS`` anyway.
    """
    import uvicorn

    chosen_port = pick_port(explicit=port, host=host)
    url = f"http://{host}:{chosen_port}/"
    print(f"tacon serve: listening on {url}")  # noqa: T201
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(
        create_app(db_path=db_path, token=token, rate_per_sec=rate_per_sec),
        host=host,
        port=chosen_port,
        log_level="info",
    )
