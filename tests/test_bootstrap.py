from pathlib import Path

import pytest

from app.adapters.clock.clocks import OffsetClock, SystemClock
from app.adapters.config.yaml_loader import load_client_config
from app.adapters.llm.anthropic_sdk import AnthropicLLM
from app.adapters.llm.fast_path import FastPathExtractor
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.llm.openrouter import OpenRouterLLM
from app.adapters.llm.resilient import Pair, ResilientLLM
from app.bootstrap import Container, build, with_public_links
from app.settings import Settings


def settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'b.db'}", **overrides)  # type: ignore[arg-type]


def primary(container: Container) -> Pair:
    llm = container.service.ports.responder
    assert isinstance(llm, ResilientLLM)
    extractor = container.service.ports.extractor
    assert isinstance(extractor, FastPathExtractor)  # closed answers skip the LLM
    assert extractor._llm is llm
    assert isinstance(llm.primary, Pair)
    return llm.primary


def test_fake_provider_runs_offline(tmp_path: Path) -> None:
    container = build(settings(tmp_path))
    assert isinstance(primary(container).extractor, KeywordExtractor)
    assert container.service.ports.responder.fallback is None  # type: ignore[attr-defined]
    assert isinstance(container.clock, OffsetClock)
    container.close()


def test_openrouter_provider(tmp_path: Path) -> None:
    container = build(
        settings(tmp_path, llm_provider="openrouter", openrouter_api_key="k", openrouter_model="m")
    )
    assert isinstance(primary(container).extractor, OpenRouterLLM)
    assert primary(container).extractor.reasoning_effort == "low"  # the default
    container.close()


@pytest.mark.parametrize(("effort", "sent"), [("minimal", "minimal"), ("", None)])
def test_openrouter_reasoning_effort(tmp_path: Path, effort: str, sent: str | None) -> None:
    container = build(
        settings(
            tmp_path,
            llm_provider="openrouter",
            openrouter_api_key="k",
            openrouter_model="m",
            openrouter_reasoning_effort=effort,
        )
    )
    assert primary(container).extractor.reasoning_effort == sent
    container.close()


def test_fallback_provider_and_model(tmp_path: Path) -> None:
    container = build(
        settings(
            tmp_path,
            llm_provider="anthropic",
            anthropic_api_key="k",
            llm_fallback_provider="openrouter",
            llm_fallback_model="other/model",
            openrouter_api_key="k",
        )
    )
    llm = container.service.ports.responder
    assert isinstance(llm, ResilientLLM)
    assert isinstance(primary(container).extractor, AnthropicLLM)
    assert isinstance(llm.fallback, Pair)
    assert isinstance(llm.fallback.extractor, OpenRouterLLM)
    assert llm.fallback.extractor._model == "other/model"
    container.close()


def test_openrouter_needs_a_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build(settings(tmp_path, llm_provider="openrouter"))


def test_anthropic_provider_and_real_clock(tmp_path: Path) -> None:
    container = build(
        settings(tmp_path, llm_provider="anthropic", anthropic_api_key="k", demo_mode=False)
    )
    assert isinstance(primary(container).responder, AnthropicLLM)
    assert isinstance(container.clock, SystemClock)
    container.close()


def test_offset_clock_only_moves_forward() -> None:
    clock = OffsetClock()
    before = clock.now()
    clock.advance(hours=2)
    assert (clock.now() - before).total_seconds() >= 7200
    with pytest.raises(ValueError, match="forward"):
        clock.advance(hours=-1)


def test_geocoder_is_chosen_by_settings() -> None:
    from app.adapters.geo.fake import FakeGeocoder
    from app.adapters.geo.nominatim import NominatimGeocoder
    from app.bootstrap import make_geocoder

    assert make_geocoder(Settings(_env_file=None, geocoder="")) is None
    assert isinstance(make_geocoder(Settings(_env_file=None, geocoder="fake")), FakeGeocoder)
    nominatim = Settings(_env_file=None, geocoder="nominatim", nominatim_user_agent="x (a@b.c)")
    assert isinstance(make_geocoder(nominatim), NominatimGeocoder)


def test_public_base_url_makes_the_privacy_link_absolute() -> None:
    config = load_client_config("grupo_sazon")
    settings = Settings(_env_file=None, public_base_url="https://jobs.example.com/")
    assert with_public_links(config, settings).privacy_url == "https://jobs.example.com/privacy"
    assert with_public_links(config, Settings(_env_file=None)).privacy_url == "/privacy"
