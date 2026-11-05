from pathlib import Path

import pytest

from app.adapters.clock.clocks import OffsetClock, SystemClock
from app.adapters.config.yaml_loader import load_client_config
from app.adapters.llm.anthropic_sdk import AnthropicLLM
from app.adapters.llm.fast_path import FastPathExtractor
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.llm.openai_sdk import OpenAILLM
from app.adapters.llm.openrouter import OpenRouterLLM
from app.adapters.llm.resilient import Pair, ResilientLLM
from app.bootstrap import Container, build, make_llm, with_public_links
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


def test_anthropic_needs_a_key_as_the_fallback_too(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build(
            settings(
                tmp_path,
                llm_provider="openai",
                openai_api_key="k",
                llm_fallback_provider="anthropic",
            )
        )


def test_demo_defaults_to_terra_through_openrouter(tmp_path: Path) -> None:
    container = build(settings(tmp_path, llm_provider="openrouter", openrouter_api_key="k"))
    extractor = primary(container).extractor
    assert isinstance(extractor, OpenRouterLLM)
    assert extractor._model == "openai/gpt-5.6-terra"
    container.close()


def test_production_is_terra_with_haiku_behind_it(tmp_path: Path) -> None:
    container = build(
        settings(
            tmp_path,
            demo_mode=False,
            llm_provider="openai",
            openai_api_key="k",
            llm_fallback_provider="anthropic",
            llm_fallback_model="claude-haiku-5-5",
            anthropic_api_key="k",
        )
    )
    llm = container.service.ports.responder
    assert isinstance(llm, ResilientLLM)
    extractor = primary(container).extractor
    assert isinstance(extractor, OpenAILLM)
    assert extractor._model == "gpt-5.6-terra"
    assert isinstance(llm.fallback, Pair)
    assert isinstance(llm.fallback.extractor, AnthropicLLM)
    assert llm.fallback.extractor._model == "claude-haiku-5-5"
    container.close()


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


def test_whatsapp_invites_need_a_secret_to_sign_codes(tmp_path: Path) -> None:
    channel = {
        "whatsapp_access_token": "t",
        "whatsapp_phone_number_id": "1",
        "whatsapp_verify_token": "v",
        "whatsapp_app_secret": "s",
        "whatsapp_invite_number": "15551868329",
    }
    database = f"sqlite:///{tmp_path / 'i.db'}"
    with pytest.raises(ValueError, match="WHATSAPP_INVITE_NUMBER needs SESSION_SECRET"):
        build(Settings(_env_file=None, database_url=database, **channel))


def test_openai_provider_needs_a_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        make_llm(Settings(_env_file=None, llm_provider="openai"))


def test_openai_is_wired_with_its_settings() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key="k",
        openai_model="gpt-x",
        openai_input_usd_per_mtok=1.0,
        openai_output_usd_per_mtok=2.0,
    )
    extractor, responder = make_llm(settings)
    assert isinstance(extractor, OpenAILLM)
    assert extractor is responder
    assert extractor._model == "gpt-x"
    assert extractor._prices == (1.0, 2.0)


def test_the_example_env_file_loads_as_the_readme_says(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cp .env.example .env` is the quick start: every blank value in it must mean "not set"."""
    for name in (
        "OPENAI_INPUT_USD_PER_MTOK",
        "OPENAI_OUTPUT_USD_PER_MTOK",
        "ANTHROPIC_INPUT_USD_PER_MTOK",
        "ANTHROPIC_OUTPUT_USD_PER_MTOK",
    ):
        monkeypatch.delenv(name, raising=False)
    example = Path(__file__).parent.parent / ".env.example"
    loaded = Settings(_env_file=example)  # type: ignore[call-arg]
    assert loaded.openai_input_usd_per_mtok is None
    assert loaded.openai_output_usd_per_mtok is None
    assert loaded.anthropic_input_usd_per_mtok is None
    assert loaded.anthropic_output_usd_per_mtok is None
