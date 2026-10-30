from pathlib import Path

import pytest

from app.adapters.clock.clocks import OffsetClock, SystemClock
from app.adapters.llm.anthropic_sdk import AnthropicLLM
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.llm.openrouter import OpenRouterLLM
from app.bootstrap import build
from app.settings import Settings


def settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'b.db'}", **overrides)  # type: ignore[arg-type]


def test_fake_provider_runs_offline(tmp_path: Path) -> None:
    container = build(settings(tmp_path))
    assert isinstance(container.service.ports.extractor, KeywordExtractor)
    assert isinstance(container.clock, OffsetClock)
    container.close()


def test_openrouter_provider(tmp_path: Path) -> None:
    container = build(
        settings(tmp_path, llm_provider="openrouter", openrouter_api_key="k", openrouter_model="m")
    )
    assert isinstance(container.service.ports.extractor, OpenRouterLLM)
    container.close()


def test_openrouter_needs_a_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build(settings(tmp_path, llm_provider="openrouter"))


def test_anthropic_provider_and_real_clock(tmp_path: Path) -> None:
    container = build(
        settings(tmp_path, llm_provider="anthropic", anthropic_api_key="k", demo_mode=False)
    )
    assert isinstance(container.service.ports.responder, AnthropicLLM)
    assert isinstance(container.clock, SystemClock)
    container.close()


def test_offset_clock_only_moves_forward() -> None:
    clock = OffsetClock()
    before = clock.now()
    clock.advance(hours=2)
    assert (clock.now() - before).total_seconds() >= 7200
    with pytest.raises(ValueError, match="forward"):
        clock.advance(hours=-1)
