"""Wire adapters to ports from settings. The only place that knows which adapters exist."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Engine

from app import tracing
from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.channels.whatsapp import WhatsAppClient
from app.adapters.clock.clocks import OffsetClock, SystemClock
from app.adapters.config.yaml_loader import load_client_config
from app.adapters.geo.fake import FakeGeocoder
from app.adapters.geo.nominatim import NominatimGeocoder
from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.fast_path import FastPathExtractor
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.llm.resilient import Pair, ResilientLLM
from app.adapters.llm.usage import UsageMeter
from app.adapters.storage.dashboard import DashboardQueries
from app.adapters.storage.rate_limit import SqlRateLimiter
from app.adapters.storage.retention import SqlRetention
from app.adapters.storage.sql import SqlRepository, make_engine
from app.adapters.voice.elevenlabs import ElevenLabsTTS
from app.domain.config import ClientConfig
from app.domain.ports import Clock, Extractor, Geocoder, Ports, Responder
from app.settings import Settings
from app.worker.pool import TurnPool
from app.worker.runner import Runner
from app.worker.service import ScreeningService
from app.worker.whatsapp import WhatsAppChannel

logger = logging.getLogger("app.bootstrap")


@dataclass
class Container:
    settings: Settings
    config: ClientConfig
    clock: Clock
    service: ScreeningService
    runner: Runner
    queries: DashboardQueries
    db: Engine
    limiter: SqlRateLimiter
    pool: TurnPool
    tts: ElevenLabsTTS | None = None
    whatsapp: WhatsAppChannel | None = None

    def close(self) -> None:
        self.pool.shutdown()  # running turns hold leases: let them finish first
        self.db.dispose()


def make_geocoder(settings: Settings) -> Geocoder | None:
    match settings.geocoder:
        case "nominatim":
            return NominatimGeocoder(settings.nominatim_user_agent, base_url=settings.nominatim_url)
        case "fake":
            return FakeGeocoder()
        case _:
            return None


def make_llm(
    settings: Settings,
    meter: UsageMeter | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[Extractor, Responder]:
    """One provider's adapters: `settings.llm_provider` unless `provider` is given (the
    fallback), with `model` overriding that provider's model setting."""
    match provider or settings.llm_provider:
        case "fake":
            return KeywordExtractor(), TemplateResponder()
        case "openrouter":
            from app.adapters.llm.openrouter import OpenRouterLLM

            if settings.openrouter_api_key is None:
                raise ValueError("OPENROUTER_API_KEY is not set")
            llm = OpenRouterLLM(
                settings.openrouter_api_key.get_secret_value(),
                model or settings.openrouter_model,
                timeout=settings.llm_timeout_seconds,
                meter=meter,
                reasoning_effort=settings.openrouter_reasoning_effort.strip() or None,
                max_retries=settings.llm_max_retries,
            )
            return llm, llm
        case "anthropic":
            import anthropic

            from app.adapters.llm.anthropic_sdk import AnthropicLLM

            key = settings.anthropic_api_key
            client = anthropic.Anthropic(
                api_key=key.get_secret_value() if key else None,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,  # jittered, honours Retry-After
            )
            usd_in = settings.anthropic_input_usd_per_mtok
            usd_out = settings.anthropic_output_usd_per_mtok
            prices = (usd_in, usd_out) if usd_in is not None and usd_out is not None else None
            claude = AnthropicLLM(
                client, model or settings.anthropic_model, meter=meter, usd_per_mtok=prices
            )
            return claude, claude
        case other:
            raise ValueError(f"unknown LLM provider: {other!r}")


def make_resilient_llm(settings: Settings) -> ResilientLLM:
    """The primary provider behind a concurrency cap, with the fallback (if any) behind the
    same interface. The engine sees one Extractor and one Responder either way."""
    fallback = None
    if settings.llm_fallback_provider:
        fallback = Pair(
            *make_llm(
                settings,
                provider=settings.llm_fallback_provider,
                model=settings.llm_fallback_model or None,
            )
        )
    return ResilientLLM(
        Pair(*make_llm(settings)),
        fallback,
        max_concurrency=settings.llm_max_concurrency,
        acquire_timeout=settings.llm_timeout_seconds,
    )


def with_public_links(config: ClientConfig, settings: Settings) -> ClientConfig:
    """A relative privacy link works in the web chat but not on WhatsApp: make it absolute."""
    base = settings.public_base_url.rstrip("/")
    if base and config.privacy_url.startswith("/"):
        return config.model_copy(update={"privacy_url": base + config.privacy_url})
    if settings.whatsapp_phone_number_id and config.privacy_url.startswith("/"):
        logger.warning("privacy link is relative: set PUBLIC_BASE_URL so it works on WhatsApp")
    return config


def build(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    tracing.configure_logging()
    tracing.configure_tracing(f"sazon-{settings.role}")
    clock: Clock = OffsetClock() if settings.demo_mode else SystemClock()
    config = with_public_links(load_client_config(settings.client_id), settings)
    llm = make_resilient_llm(settings)
    db = make_engine(settings.database_url)
    calendar = SqlCalendar(db, config, clock)
    ports = Ports(
        extractor=FastPathExtractor(llm),
        responder=llm,
        clock=clock,
        calendar=calendar,
        geocoder=make_geocoder(settings),
    )
    # Leases run on the system clock: fast-forwarding the demo clock mustn't expire them.
    repo = SqlRepository(db, clock, config.client_id, lease_clock=SystemClock())
    service = ScreeningService(repo, config, ports)
    runner = Runner(service, settings.debounce_seconds, settings.reminder_interval_seconds)
    queries = DashboardQueries(db, config.client_id)
    tts = None
    if settings.elevenlabs_api_key is not None:
        tts = ElevenLabsTTS(
            settings.elevenlabs_api_key.get_secret_value(),
            settings.elevenlabs_voice_id,
            settings.elevenlabs_model,
        )
    debounce = timedelta(seconds=settings.debounce_seconds)
    pool = TurnPool(service, settings.worker_concurrency, debounce)
    runner.add_periodic("process_waiting", pool.fill, settings.worker_poll_seconds)
    runner.add_periodic("release_orphan_bookings", calendar.release_orphans, 300)
    runner.add_periodic("retention", SqlRetention(db, config, clock).erase_expired, 3600)
    whatsapp = make_whatsapp(settings, service)
    if whatsapp is not None:
        runner.add_periodic(
            "whatsapp_outbox", whatsapp.dispatch_once, settings.whatsapp_dispatch_seconds
        )
    limiter = SqlRateLimiter(db, clock, config.client_id)
    return Container(
        settings, config, clock, service, runner, queries, db, limiter, pool, tts, whatsapp
    )


def make_whatsapp(settings: Settings, service: ScreeningService) -> WhatsAppChannel | None:
    if settings.whatsapp_access_token is None:
        return None
    if settings.whatsapp_verify_token is None or settings.whatsapp_app_secret is None:
        raise ValueError("WhatsApp needs WHATSAPP_VERIFY_TOKEN and WHATSAPP_APP_SECRET too")
    client = WhatsAppClient(
        settings.whatsapp_access_token.get_secret_value(),
        settings.whatsapp_phone_number_id,
        settings.whatsapp_api_version,
    )
    return WhatsAppChannel(
        service,
        client,
        settings.whatsapp_reminder_template,
        concurrency=settings.whatsapp_dispatch_concurrency,
    )
