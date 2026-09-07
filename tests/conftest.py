"""Shared fixtures, and the guard that makes one of the rules enforceable.

DESIGN.md §22: **no automated test may make a real network call.** Every external
call goes through an injectable seam (``ModelClient``, ``fetch_impl``,
``Sandbox``), and a test that needs a model uses the scriptable fake.

That rule is easy to state and easy to break by accident: one adapter that
constructs its own client, one test that points at a real endpoint "just to
check", and the suite quietly depends on someone else's uptime and on a
credential nobody meant to ship. So it is enforced rather than remembered.

The store tests are the deliberate exception, and they are not really one: they
talk to a real database on loopback, started by ``scripts/dev-services.sh``. The
guard below allows loopback and blocks everything else, which is exactly the line
DESIGN.md draws. A local stub server a test started itself is on the correct side
of it; ``api.openai.com`` is not.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterator
from typing import Any

import pytest

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_GETADDRINFO = socket.getaddrinfo


class NetworkCallInTest(RuntimeError):
    """A test tried to reach something that is not on this machine.

    DESIGN.md §22 forbids it. If the call is legitimate, it belongs behind an
    injectable seam and the test should supply a fake or a local stub, not reach
    out and hope.
    """


def _is_loopback(host: object) -> bool:
    """Whether an address is on this machine.

    Hostnames that are not literal addresses are treated as remote. Resolving
    them to find out would itself be a network call, and a test that needs a name
    resolved is a test reaching for something it should have injected.
    """
    if not isinstance(host, str):
        return False
    if host in {"localhost", "localhost.localdomain", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if isinstance(address, tuple) and address and _is_loopback(address[0]):
        return _REAL_CONNECT(self, address)
    if not isinstance(address, tuple):
        # A unix domain socket. Local by construction, and how the MySQL client
        # and a sandbox's host-binding channel talk.
        return _REAL_CONNECT(self, address)
    raise NetworkCallInTest(
        f"a test tried to connect to {address!r}. DESIGN.md §22 forbids a test "
        "making a real network call: put the call behind an injectable seam and "
        "give the test a fake or a local stub server."
    )


def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
    if isinstance(address, tuple) and address and not _is_loopback(address[0]):
        raise NetworkCallInTest(f"a test tried to connect to {address!r}. See DESIGN.md §22.")
    return _REAL_CONNECT_EX(self, address)


def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    if not _is_loopback(host):
        raise NetworkCallInTest(
            f"a test tried to resolve {host!r}. DESIGN.md §22 forbids a test "
            "making a real network call, and a DNS lookup is one."
        )
    return _REAL_GETADDRINFO(host, port, *args, **kwargs)


@pytest.fixture(autouse=True, scope="session")
def _no_network() -> Iterator[None]:
    """Block every outbound connection that is not loopback, for the whole suite.

    Autouse and session-scoped so it cannot be forgotten and cannot be opted out
    of by a test that does not know about it. Set ``PSYCH_ALLOW_NETWORK=1`` to
    disable it, which exists only for someone deliberately debugging against a
    real provider and is never set in CI.
    """
    if os.environ.get("PSYCH_ALLOW_NETWORK") == "1":
        yield
        return

    # Patching the socket module is the point, and mypy is right that these
    # assignments do not match the declared signatures: the guards take Any
    # because they inspect an address whose shape depends on the family.
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = _guarded_getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = _REAL_CONNECT  # type: ignore[method-assign]
        socket.socket.connect_ex = _REAL_CONNECT_EX  # type: ignore[method-assign]
        socket.getaddrinfo = _REAL_GETADDRINFO


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """DSN for a real PostgreSQL. Skips when none is configured."""
    dsn = os.environ.get("PSYCH_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("PSYCH_TEST_POSTGRES_DSN is unset; run scripts/dev-services.sh start")
    return dsn


@pytest.fixture(scope="session")
def mysql_dsn() -> str:
    """DSN for a real MySQL or MariaDB. Skips when none is configured."""
    dsn = os.environ.get("PSYCH_TEST_MYSQL_DSN")
    if not dsn:
        pytest.skip("PSYCH_TEST_MYSQL_DSN is unset; run scripts/dev-services.sh start")
    return dsn


@pytest.fixture(scope="session")
def dynamodb_endpoint() -> str:
    """Endpoint for DynamoDB Local. Skips when none is configured."""
    endpoint = os.environ.get("PSYCH_TEST_DYNAMODB_ENDPOINT")
    if not endpoint:
        pytest.skip("PSYCH_TEST_DYNAMODB_ENDPOINT is unset; run scripts/dev-services.sh start")
    return endpoint
