"""Token and cost accounting across LLM calls (evals, budgets, cost per conversation)."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class UsageMeter:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0  # OpenRouter reports cost in credits, bought 1:1 with USD

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def record(self, prompt_tokens: int, completion_tokens: int, cost: float) -> None:
        with self._lock:
            self.calls += 1
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            self.cost_usd += cost
