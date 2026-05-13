"""Tests for ``tacon.server`` — the v0.3 GUI skeleton.

Covers the host-header allowlist, the free-port picker, and the
healthz endpoint. The SPA, op routes, and SSE feed land in subsequent
commits and add their own test modules.
"""

from __future__ import annotations

import socket
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from tacon.server import (
    DEFAULT_PORT_RANGE,
    PortInUseError,
    create_app,
    pick_port,
)

# ---------- /healthz ----------


def test_healthz_returns_200_with_version() -> None:
    """A basic liveness probe. The CLI's startup wait could poll this."""
    from tacon import __version__

    client = TestClient(create_app())
    response = client.get("/healthz", headers={"host": "localhost"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


# ---------- host-header allowlist (DNS-rebinding defense) ----------


def test_host_allowlist_accepts_localhost() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz", headers={"host": "localhost"})
    assert response.status_code == 200


def test_host_allowlist_accepts_loopback_ipv4() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz", headers={"host": "127.0.0.1"})
    assert response.status_code == 200


def test_host_allowlist_accepts_localhost_with_port_suffix() -> None:
    """Browsers send Host: localhost:5734 — the suffix gets stripped."""
    client = TestClient(create_app())
    response = client.get("/healthz", headers={"host": "localhost:5734"})
    assert response.status_code == 200


def test_host_allowlist_rejects_evil_host() -> None:
    """A page on the public internet via DNS-rebinding to 127.0.0.1
    would send its own Host header; allowlist blocks it."""
    client = TestClient(create_app())
    response = client.get("/healthz", headers={"host": "evil.example.com"})
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "forbidden_host"
    assert "evil.example.com" in body["detail"]


def test_host_allowlist_rejects_ip_address_other_than_loopback() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz", headers={"host": "192.168.1.10"})
    assert response.status_code == 403


# ---------- port picker ----------


def test_pick_port_returns_first_free_in_default_range() -> None:
    """The first free port in 5734-5740 wins when no --port given."""
    port = pick_port()
    start, end = DEFAULT_PORT_RANGE
    assert start <= port <= end


def test_pick_port_explicit_returns_explicit_when_free() -> None:
    """An explicit --port that's available is returned unchanged."""
    free = _grab_free_port()
    assert pick_port(explicit=free) == free


def test_pick_port_explicit_raises_when_taken() -> None:
    """An explicit --port that's already bound raises with a clear msg."""
    with _BindPort() as taken_port, pytest.raises(PortInUseError, match=f"port {taken_port}"):
        pick_port(explicit=taken_port)


def test_pick_port_walks_range_when_first_is_taken() -> None:
    """If 5734 is taken, pick_port falls through to 5735."""
    with _BindPort(DEFAULT_PORT_RANGE[0]):
        port = pick_port(port_range=DEFAULT_PORT_RANGE)
        assert port == DEFAULT_PORT_RANGE[0] + 1


def test_pick_port_raises_when_entire_range_taken() -> None:
    """All-busy range: a clear error message instead of silent failure."""
    # A range of size 1 that we then occupy is the cleanest way to
    # guarantee "every port in range is taken" without flakiness from
    # other processes grabbing intermediate ports.
    with _BindPort() as taken, pytest.raises(PortInUseError, match="no free port in range"):
        pick_port(port_range=(taken, taken))


# ---------- helpers ----------


def _grab_free_port() -> int:
    """Ask the OS for a port that's free right now. Same trick stdlib
    uses in test_socket. Closes the probe socket before returning."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _BindPort:
    """Context manager that binds + listens on a port so it appears
    taken. Yields the chosen port number."""

    def __init__(self, port: int | None = None) -> None:
        self._port = port
        self._sock: socket.socket | None = None

    def __enter__(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target_port = self._port if self._port is not None else 0
        self._sock.bind(("127.0.0.1", target_port))
        self._sock.listen(1)
        # No accept loop needed; bind alone makes the port appear taken
        # to our probe.
        return int(self._sock.getsockname()[1])

    def __exit__(self, *args: object) -> None:
        if self._sock is not None:
            self._sock.close()
