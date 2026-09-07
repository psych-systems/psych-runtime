"""The seam's own bounds: timeouts, redirects, and what the policy is asked.

``tests/unit/test_egress_seam.py`` proves every outbound call goes *through*
``HttpTransport`` by reading the source. This proves the transport itself is
worth going through: that it bounds a request in time, that it refuses a client
configured to leave its control, and that the policy is asked about the URL
that will actually be requested rather than a prefix of it.
"""

from __future__ import annotations

import httpx
import pytest

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import HttpTransport

pytestmark = pytest.mark.functional

SCOPE = Scope(tenant="acme")


class TestTheSeamIsBounded:
    async def test_a_transport_built_by_default_has_real_timeouts(self) -> None:
        """``httpx.AsyncClient(timeout=None)`` disables every timeout, which is
        what this transport used to be constructed with. A tenant-controlled
        server that accepted a connection and then said nothing held a Worker's
        turn open until the Run's own deadline, with the lease heartbeat
        keeping the Run pinned to that Worker the whole time."""
        transport = HttpTransport()
        try:
            timeout = transport._client.timeout
            assert timeout.connect is not None
            assert timeout.read is not None
            assert timeout.write is not None
            assert timeout.pool is not None
        finally:
            await transport.aclose()

    def test_a_client_that_follows_redirects_is_refused(self) -> None:
        """A redirect is a second request to a host the policy never evaluated,
        so a following client turns one allowed URL into an arbitrary one."""
        with pytest.raises(ValueError, match="follow_redirects"):
            HttpTransport(client=httpx.AsyncClient(follow_redirects=True))

    async def test_the_policy_is_asked_about_the_url_including_its_query(self) -> None:
        """A policy matching on a full URL saw only the path when the caller
        passed ``params`` separately, so a rule written against a query
        parameter -- the shape an SSRF guard takes -- matched nothing."""
        seen: list[str] = []

        class Recording:
            async def allow(self, scope: Scope, url: str) -> bool:
                _ = scope
                seen.append(url)
                return False

        transport = HttpTransport(policy=Recording())
        try:
            with pytest.raises(AccessDenied):
                await transport.request(
                    "GET",
                    "https://example.invalid/fetch",
                    scope=SCOPE,
                    params={"target": "http://169.254.169.254/"},
                )
        finally:
            await transport.aclose()

        assert seen == ["https://example.invalid/fetch?target=http%3A%2F%2F169.254.169.254%2F"]
