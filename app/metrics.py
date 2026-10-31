"""Prometheus metrics: the signals in docs/architecture.md, "Monitor".

Counters and histograms are recorded where things happen (service, LLM adapters, channels);
backlog gauges are read from the database at scrape time (`BacklogCollector`). Labels are
enums and provider names only: never ids, names or message text. Each process exposes its own
`/metrics`; Prometheus scrapes every instance and sums.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime

from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app import tracing
from app.domain.models import DomainEvent, EventType

TURN_SECONDS = Histogram(
    "sazon_turn_seconds",
    "Time to process one candidate turn (LLM calls included).",
    ["channel"],
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32),
)
EVENTS = Counter(
    "sazon_events_total",
    "Domain events. Failure and fallback types (extractor_failed, responder_failed, "
    "reply_rejected) are the LLM error and template-fallback rates.",
    ["type"],
)
OUTCOMES = Counter("sazon_outcomes_total", "Conversations reaching a status.", ["status"])
LLM_CALLS = Counter("sazon_llm_calls_total", "LLM calls that returned.", ["provider", "kind"])
LLM_SECONDS = Histogram(
    "sazon_llm_call_seconds",
    "LLM call latency.",
    ["provider", "kind"],
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16),
)
LLM_TOKENS = Counter("sazon_llm_tokens_total", "LLM tokens.", ["provider", "direction"])
LLM_COST = Counter(
    "sazon_llm_cost_usd_total", "LLM spend (where the price is known).", ["provider"]
)
RATE_LIMITED = Counter("sazon_rate_limited_total", "Requests refused with 429.", ["endpoint"])
WHATSAPP_SENT = Counter("sazon_whatsapp_messages_total", "WhatsApp outbox results.", ["result"])
WHATSAPP_DUPLICATES = Counter(
    "sazon_whatsapp_duplicate_deliveries_total", "WhatsApp webhook retries dropped."
)


def record_events(events: list[DomainEvent]) -> None:
    for event in events:
        EVENTS.labels(event.type.value).inc()
        if event.type == EventType.STATUS_CHANGED and isinstance(event.data.get("to"), str):
            OUTCOMES.labels(event.data["to"]).inc()


def record_llm_call(
    provider: str,
    kind: str,
    seconds: float,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
) -> None:
    """Metrics for one LLM call, and its span (inside the turn's span, if any)."""
    now = time.time_ns()
    span = tracing.tracer.start_span(
        f"llm.{kind}",
        start_time=now - int(seconds * 1e9),
        attributes={
            "llm.provider": provider,
            "llm.prompt_tokens": prompt_tokens,
            "llm.completion_tokens": completion_tokens,
            "llm.cost_usd": cost_usd,
        },
    )
    span.end(end_time=now)
    LLM_CALLS.labels(provider, kind).inc()
    LLM_SECONDS.labels(provider, kind).observe(seconds)
    LLM_TOKENS.labels(provider, "prompt").inc(prompt_tokens)
    LLM_TOKENS.labels(provider, "completion").inc(completion_tokens)
    if cost_usd:
        LLM_COST.labels(provider).inc(cost_usd)


# (count, oldest timestamp or None) per backlog, from the read model.
Backlog = dict[str, tuple[int, datetime | None]]

_BACKLOG_HELP = {
    "pending_messages": "Candidate messages waiting for a turn (queue depth and age).",
    "review_queue": "Candidates waiting for a recruiter's review.",
    "whatsapp_outbox": "Agent messages not yet delivered on WhatsApp.",
}


class BacklogCollector(Collector):
    """Gauges read from the database when Prometheus scrapes, so they are always current."""

    def __init__(self, backlog: Callable[[], Backlog], now: Callable[[], datetime]) -> None:
        self._backlog = backlog
        self._now = now

    def collect(self) -> Iterator[GaugeMetricFamily]:
        backlog, now = self._backlog(), self._now()
        for name, (count, oldest) in backlog.items():
            yield GaugeMetricFamily(f"sazon_{name}", _BACKLOG_HELP[name], value=count)
            age = (now - oldest).total_seconds() if oldest else 0.0
            yield GaugeMetricFamily(
                f"sazon_{name}_oldest_seconds",
                f"Age of the oldest item. {_BACKLOG_HELP[name]}",
                value=age,
            )
