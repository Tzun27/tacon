"""FastAPI app for ``tacon serve`` — the v0.3 local web GUI.

This module is the v0.3 entry point's skeleton: a FastAPI app + uvicorn
launcher + a host-header allowlist (DNS-rebinding defense) + a free-port
picker. The SPA, op endpoints, and SSE feed land in subsequent commits.

The module imports cleanly without the [gui] extra installed — the
heavy deps (fastapi, uvicorn, sse-starlette, keyring) live inside
functions / class bodies that only run during ``tacon serve``. This
keeps `import tacon.server` cheap for users who only run the CLI.
"""

from __future__ import annotations

import socket
import webbrowser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


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


def create_app() -> FastAPI:
    """Build the FastAPI app. Imported lazily so users without the
    [gui] extra don't pay the import cost just to run the CLI."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from tacon import __version__

    app = FastAPI(
        title="tacon",
        version=__version__,
        # Disable the auto-mounted docs UIs until we have real routes
        # worth documenting. Re-enable in a later step.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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

    return app


def serve(
    *,
    port: int | None = None,
    host: str = "127.0.0.1",
    open_browser: bool = True,
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
    uvicorn.run(create_app(), host=host, port=chosen_port, log_level="info")
