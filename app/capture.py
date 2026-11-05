"""What the LLM really returned, for debugging extraction. Off unless LLM_CAPTURE_OUTPUTS is set.

Like the turn's cost in `metrics`: adapters report each output, and the service collects those of
one turn, on this thread. It stays out of logs, traces, the dashboard and exports, because the
text holds the candidate's personal data.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.domain.models import LlmOutput

_outputs: ContextVar[list[LlmOutput] | None] = ContextVar("llm_outputs", default=None)


@contextmanager
def collect() -> Iterator[list[LlmOutput]]:
    """Gather the outputs reported inside the block."""
    outputs: list[LlmOutput] = []
    token = _outputs.set(outputs)
    try:
        yield outputs
    finally:
        _outputs.reset(token)


def report(kind: str, provider: str, model: str, text: str) -> None:
    """Called by the LLM adapters before parsing, so a reply that fails to parse is kept too."""
    if (outputs := _outputs.get()) is not None:
        outputs.append(LlmOutput(kind=kind, provider=provider, model=model, text=text))
