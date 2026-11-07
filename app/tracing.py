"""Tracing (OpenTelemetry) and request ids in logs.

Spans: FastAPI makes the request's own (tagged with `request.id` by our middleware); we add one
per candidate turn (`turn`) and one per LLM call (`llm.<kind>`) nested inside it. Attributes are
ids, enums, counts and timings, never message text or names. Export with OTLP when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set (any collector: Jaeger, Tempo, Honeycomb…); otherwise the
API's no-op tracer makes all of this free.

Every log line carries the request id (`req=`), and responses return it as `X-Request-ID`, so
a candidate's or recruiter's report can be matched to logs and traces.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

tracer = trace.get_tracer("sazon")
request_id: ContextVar[str] = ContextVar("request_id", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s req=%(request_id)s %(message)s"


class RequestIdFilter(logging.Filter):
    """Adds the current request id to every log record (`-` outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    if not any(isinstance(f, RequestIdFilter) for h in root.handlers for f in h.filters):
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    # At INFO httpx logs every request's full URL, and a geocoder URL holds what a candidate wrote.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configure_tracing(service_name: str) -> bool:
    """Export spans over OTLP if an endpoint is configured. Returns whether it did."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return True
