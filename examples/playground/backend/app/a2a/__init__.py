"""A2A over HTTP for this console: the transport Psych refuses to own.

`psych_runtime.a2a` is the protocol and `psych_runtime.tools.a2a` is the outbound client.
Neither can serve a request, because DESIGN.md §1 refuses "an HTTP server, a
route table, or any transport". This package is the missing half, and it is
deliberately small: five files, none of which decides anything about the
protocol.

- `service.py` -- the eleven operations, in terms of Runs.
- `router.py` -- both HTTP bindings over that service, plus authentication,
  version negotiation and extension handling.
- `cards.py` -- Agent Cards for this deployment, and the key that signs them.
- `configs.py` -- durable push-notification configurations.
- `push.py` -- the webhook sender, through the egress seam.

**gRPC is not served here.** §5.2 requires an agent to declare what it
supports, and the cards this backend serves declare JSON-RPC and HTTP+JSON
only. The reason is in `psych_runtime/a2a/README.md`: gRPC needs a gRPC server and
generated stubs, and the two HTTP bindings are functionally equivalent per
§5.1, so nothing is unreachable.

## Wiring it up

`app.main` builds an `A2ADeps` in its lifespan and includes
`build_router(...)`, which is three lines there and everything else here.
"""

from __future__ import annotations

from app.a2a.cards import CardFactory, load_signing_key
from app.a2a.configs import PushConfigStore
from app.a2a.push import PushSender
from app.a2a.router import A2ADeps, build_router
from app.a2a.service import A2AService

__all__ = [
    "A2ADeps",
    "A2AService",
    "CardFactory",
    "PushConfigStore",
    "PushSender",
    "build_router",
    "load_signing_key",
]
