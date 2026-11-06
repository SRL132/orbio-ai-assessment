"""Resilience around the LLM, behind the same Extractor/Responder interface the engine uses.

- **Concurrency cap.** At most `max_concurrency` calls in flight per process, so a peak (or a
  retry storm) queues here instead of piling onto the provider's rate limit. Across the fleet
  the cap is replicas x this number: size it to the provider's limits (docs/architecture.md).
  A call that can't get a slot in `acquire_timeout` raises `LLMUnavailable`, and the engine
  holds the candidate's message for a later retry.
- **Fallback provider.** When the primary fails (after its own retries), the same call goes to
  the fallback. The engine never knows which one answered.
- **Circuit breaker.** After a primary failure, calls go straight to the fallback for
  `cooldown_seconds`, then the primary is tried again. No point paying a timeout per turn
  while a provider is down.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app import metrics
from app.adapters.llm.anthropic_sdk import RefusalError
from app.domain.config import ClientConfig
from app.domain.models import AskField, ConversationState, ExtractionResult, ReAsk
from app.domain.ports import Extractor, LLMUnavailable, Responder

logger = logging.getLogger("app.llm")


class LLM(Protocol):
    """One provider: both an Extractor and a Responder (as every real adapter is)."""

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult: ...

    def compose(
        self,
        action: AskField | ReAsk,
        state: ConversationState,
        config: ClientConfig,
    ) -> str: ...


@dataclass(frozen=True)
class Pair:
    """An extractor and a responder from separate objects (the offline fakes) as one LLM."""

    extractor: Extractor
    responder: Responder

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        return self.extractor.extract(state, text, config, today)

    def compose(
        self,
        action: AskField | ReAsk,
        state: ConversationState,
        config: ClientConfig,
    ) -> str:
        return self.responder.compose(action, state, config)


class LLMBusy(LLMUnavailable):
    """No free LLM slot in time: the process is at its concurrency cap."""


class ResilientLLM:
    def __init__(
        self,
        primary: LLM,
        fallback: LLM | None = None,
        max_concurrency: int = 16,
        acquire_timeout: float = 30.0,
        cooldown_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._acquire_timeout = acquire_timeout
        self._cooldown = cooldown_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._primary_down_until = 0.0

    @property
    def primary(self) -> LLM:
        return self._primary

    @property
    def fallback(self) -> LLM | None:
        return self._fallback

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        def read(llm: LLM) -> ExtractionResult:
            result = llm.extract(state, text, config, today)
            return (
                result.model_copy(update={"on_fallback": True}) if llm is self._fallback else result
            )

        return self._call("extract", read)

    def compose(
        self,
        action: AskField | ReAsk,
        state: ConversationState,
        config: ClientConfig,
    ) -> str:
        return self._call("compose", lambda llm: llm.compose(action, state, config))

    def _call[T](self, kind: str, call: Callable[[LLM], T]) -> T:
        """A bad answer (unparseable, refused) is raised as it is. Anything else means no
        provider answered: raised as `LLMUnavailable`, so the engine retries the turn later."""
        try:
            return self._call_any(kind, call)
        except (LLMUnavailable, ValueError, RefusalError):
            raise
        except Exception as exc:
            raise LLMUnavailable(f"{kind}: {type(exc).__name__}") from exc

    def _call_any[T](self, kind: str, call: Callable[[LLM], T]) -> T:
        if not self._slots.acquire(timeout=self._acquire_timeout):
            metrics.LLM_FAILURES.labels("busy", kind).inc()
            raise LLMBusy(kind)
        try:
            fallback = self._fallback
            if fallback is None:
                return self._attempt("primary", kind, call, self._primary)
            if not self._primary_is_down():
                try:
                    return self._attempt("primary", kind, call, self._primary)
                except Exception as exc:
                    self._mark_primary_down()
                    logger.warning("llm_primary_failed kind=%s error=%s", kind, type(exc).__name__)
            metrics.LLM_FALLBACKS.labels(kind).inc()
            return self._attempt("fallback", kind, call, fallback)
        finally:
            self._slots.release()

    @staticmethod
    def _attempt[T](which: str, kind: str, call: Callable[[LLM], T], llm: LLM) -> T:
        try:
            return call(llm)
        except Exception:
            metrics.LLM_FAILURES.labels(which, kind).inc()
            raise

    def _primary_is_down(self) -> bool:
        with self._lock:
            return self._monotonic() < self._primary_down_until

    def _mark_primary_down(self) -> None:
        with self._lock:
            self._primary_down_until = self._monotonic() + self._cooldown
