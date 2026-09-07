"""Traces and log lines: turning Psych's ports on, which is the consumer's job.

Psych opens spans on every Run, Attempt, Turn, model call, tool call and
compaction, and by default throws them away: ``Runtime.telemetry`` defaults to
``NOOP_TELEMETRY``, and the root ``psych`` logger carries a ``NullHandler`` and
nothing else. That is deliberate on the library's side. A library that
configured a log destination or chose an exporter would be making a decision
that belongs to whoever runs the process (DESIGN.md §1, §13).

This backend is that whoever. Until it existed the playground passed no
``Telemetry`` at all, so the console emitted no spans and the OTel adapter got
no exercise outside its own conformance tests -- which meant the demo was
quietly claiming Psych could not produce a trace, and a consumer wiring pattern
nobody had run was a pattern with a bug in it.

## Off unless asked

No endpoint configured means the no-op, and that is what keeps
``docker run -p 3000:3000`` a single command: standing up a collector to look
at a chat window is a worse first experience than having no traces. With
``PSYCH_PLAYGROUND_OTLP_ENDPOINT`` set, spans go there over OTLP/HTTP.
``docker compose up`` sets it and brings up Jaeger, so there is somewhere to
open and actually see a Run.

## Why the exporter lives here and not in `psych`

``psych``'s own ``otel`` extra takes the API and the SDK and stops there. Which
exporter, which protocol, batched or not, and what the sampling is are all a
deployment's decisions, and a library that picked them would be wrong in
somebody's cluster. This module picks them, because this is a deployment.
"""

from __future__ import annotations

import logging
import logging.config
from typing import Final

from app.config import Settings
from psych_runtime.telemetry.port import NOOP_TELEMETRY, Telemetry

_LOG: Final = logging.getLogger("psych.playground.observability")


class Observability:
    """What the app holds on to: the telemetry to hand `Runtime`, and a way to
    flush it on the way out.

    ``shutdown`` matters more than it looks. Spans are batched before export,
    so a process that exits without flushing loses the last few seconds of
    them -- which in a demo is exactly the Run somebody just watched and then
    went to look for.
    """

    def __init__(self, telemetry: Telemetry, shutdown: object | None = None) -> None:
        self.telemetry = telemetry
        self._provider = shutdown

    @property
    def enabled(self) -> bool:
        """Whether anything is actually collecting. Reported by
        ``GET /api/config`` so the console can say "traces are going here"
        rather than showing a screen that is empty for an unexplained reason."""
        return self._provider is not None

    def shutdown(self) -> None:
        provider = self._provider
        if provider is None:
            return
        # Duck-typed rather than imported: the OTel SDK is only present when
        # tracing was configured, and importing it to type this attribute
        # would make an optional dependency mandatory.
        flush = getattr(provider, "shutdown", None)
        if callable(flush):
            flush()


def configure_logging(settings: Settings) -> None:
    """Decide where Psych's log lines go, since Psych deliberately does not.

    Uvicorn configures its own loggers and nothing else, so before this the
    playground showed request lines and none of Psych's: a lease lost, a
    supervisor pass raising, a notification that could not be delivered. Those
    are the lines an operator running Workers needs, and there are few enough
    of them that ``INFO`` is a reasonable place to leave the dial.

    ``disable_existing_loggers`` is off, deliberately: turning it on would
    silence uvicorn's own access log, which is not this function's to take
    away.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "playground": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    "datefmt": "%H:%M:%S",
                }
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "playground",
                    "stream": "ext://sys.stderr",
                }
            },
            "loggers": {
                # The whole `psych.*` tree, including this backend's own
                # `psych_runtime.playground` loggers, which sit under it on purpose so
                # one setting covers both.
                "psych": {
                    "handlers": ["stderr"],
                    "level": settings.log_level,
                    "propagate": False,
                }
            },
        }
    )


def build_telemetry(settings: Settings) -> Observability:
    """A `Telemetry` for `Runtime`, exporting only when asked to.

    Every failure here is caught and degraded to the no-op rather than raised.
    A collector that is missing, misconfigured or briefly down is not a reason
    to refuse to serve a chat: the Run still happens, the Records are still
    written, and the report is still correct. Losing the trace is the smaller
    loss, and it is logged rather than swallowed so nobody is left wondering
    why the traces stopped.
    """
    if settings.otlp_endpoint is None:
        return Observability(NOOP_TELEMETRY)

    # Imported here rather than at module scope, and the lint waived on
    # purpose: the SDK and the exporter are optional, this module is imported
    # on every boot, and a missing optional dependency must degrade to no
    # traces rather than stop the backend starting.
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        from psych_runtime.telemetry.otel import OtelTelemetry  # noqa: PLC0415
    except ImportError:
        _LOG.warning(
            "PSYCH_PLAYGROUND_OTLP_ENDPOINT is set but the OpenTelemetry SDK and "
            "OTLP exporter are not installed; running without traces. Install the "
            "playground extra."
        )
        return Observability(NOOP_TELEMETRY)

    endpoint = settings.otlp_endpoint.rstrip("/")
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otlp_service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    # A provider of this backend's own rather than the global one. Setting the
    # global would decide tracing for anything else in this process, which is
    # the kind of thing a library must not do and an application should still
    # not do casually. `OtelTelemetry` takes a tracer, so it does not need it.
    telemetry = OtelTelemetry(provider.get_tracer("psych"))
    _LOG.info("exporting traces to %s as %r", endpoint, settings.otlp_service_name)
    return Observability(telemetry, provider)
