"""The playground backend: a FastAPI app a person runs locally to exercise
Psych by hand.

This package is the "platform" DESIGN.md §1 says Psych deliberately does not
ship: an HTTP server, a couple of in-memory indexes over what was published
and dispatched, and a home for the handful of decisions (which model backend,
which credentials, how approvals are gated) every real consumer has to make
for themselves. None of it belongs in ``psych`` -- that is the entire point
of building it here instead.
"""

from __future__ import annotations
