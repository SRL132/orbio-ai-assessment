"""Prometheus metrics: the signals in docs/architecture.md, "Monitor".

Counters and histograms are recorded where things happen (service, LLM adapters, channels);
backlog gauges are read from the database at scrape time (`BacklogCollector`). Labels are
enums and provider names only: never ids, names or message text. Each process exposes its own
`/metrics`; Prometheus scrapes every instance and sums.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app import tracing
from app.domain.models import TERMINAL_STATUSES, ConversationState, DomainEvent, EventType

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
# Provider outages (app/adapters/llm/resilient.py). `which` is primary, fallback or busy (no
# free concurrency slot). A rising fallback count means the primary is down.
LLM_FAILURES = Counter("sazon_llm_failures_total", "LLM calls that failed.", ["which", "kind"])
LLM_FALLBACKS = Counter(
    "sazon_llm_fallbacks_total", "LLM calls served by the fallback provider.", ["kind"]
)
TURNS_HELD = Counter(
    "sazon_turns_held_total",
    "Turns whose messages were kept for a retry because no LLM answered (outcome: held, "
    "or gave_up once the retries ran out).",
    ["outcome"],
)

# The funnel, as product signals (the analytics page has the same numbers per period). A
# product metric falling is often the first sign of a bug.
DROP_OFFS = Counter(
    "sazon_drop_offs_total", "Abandoned candidates, by where they stopped.", ["stage"]
)
CONVERSATION_COST = Histogram(
    "sazon_conversation_cost_usd",
    "LLM spend per finished conversation (where the price is known).",
    ["status"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
)
CONVERSATION_TURNS = Histogram(
    "sazon_conversation_turns",
    "Candidate turns per finished conversation.",
    ["status"],
    buckets=(2, 4, 6, 8, 12, 16, 24, 32),
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
        if event.type == EventType.ABANDONED and isinstance(event.data.get("drop_off"), str):
            DROP_OFFS.labels(event.data["drop_off"]).inc()


def record_finished(before: ConversationState, after: ConversationState) -> None:
    """Cost and turns per conversation, once, when it reaches its first outcome."""
    if before.status in TERMINAL_STATUSES or after.status not in TERMINAL_STATUSES:
        return
    CONVERSATION_COST.labels(after.status.value).observe(after.llm_cost_usd)
    CONVERSATION_TURNS.labels(after.status.value).observe(after.turns)


@dataclass
class TurnCost:
    usd: float = 0.0


_turn_cost: ContextVar[TurnCost | None] = ContextVar("turn_cost", default=None)


@contextmanager
def turn_cost() -> Iterator[TurnCost]:
    """Add up the LLM spend of the calls made inside the block (one turn, on this thread)."""
    cost = TurnCost()
    token = _turn_cost.set(cost)
    try:
        yield cost
    finally:
        _turn_cost.reset(token)


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
        if (turn := _turn_cost.get()) is not None:
            turn.usd += cost_usd


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
