"""Wire adapters to ports from settings. The only place that knows which adapters exist."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.channels.whatsapp import WhatsAppClient
from app.adapters.clock.clocks import OffsetClock, SystemClock
from app.adapters.config.yaml_loader import load_client_config
from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.llm.usage import UsageMeter
from app.adapters.storage.dashboard import DashboardQueries
from app.adapters.storage.rate_limit import SqlRateLimiter
from app.adapters.storage.retention import SqlRetention
from app.adapters.storage.sql import SqlRepository, make_engine
from app.adapters.voice.elevenlabs import ElevenLabsTTS
from app.domain.config import ClientConfig
from app.domain.ports import Clock, Extractor, Ports, Responder
from app.settings import Settings
from app.worker.runner import Runner
from app.worker.service import ScreeningService
from app.worker.whatsapp import WhatsAppChannel


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
    tts: ElevenLabsTTS | None = None
    whatsapp: WhatsAppChannel | None = None

    def close(self) -> None:
        self.db.dispose()


def make_llm(settings: Settings, meter: UsageMeter | None = None) -> tuple[Extractor, Responder]:
    match settings.llm_provider:
        case "fake":
            return KeywordExtractor(), TemplateResponder()
        case "openrouter":
            from app.adapters.llm.openrouter import OpenRouterLLM

            if settings.openrouter_api_key is None:
                raise ValueError("OPENROUTER_API_KEY is not set")
            llm = OpenRouterLLM(
                settings.openrouter_api_key.get_secret_value(),
                settings.openrouter_model,
                timeout=settings.llm_timeout_seconds,
                meter=meter,
            )
            return llm, llm
        case "anthropic":
            import anthropic

            from app.adapters.llm.anthropic_sdk import AnthropicLLM

            key = settings.anthropic_api_key
            client = anthropic.Anthropic(
                api_key=key.get_secret_value() if key else None,
                timeout=settings.llm_timeout_seconds,
            )
            usd_in = settings.anthropic_input_usd_per_mtok
            usd_out = settings.anthropic_output_usd_per_mtok
            prices = (usd_in, usd_out) if usd_in is not None and usd_out is not None else None
            claude = AnthropicLLM(
                client, settings.anthropic_model, meter=meter, usd_per_mtok=prices
            )
            return claude, claude


def build(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    clock: Clock = OffsetClock() if settings.demo_mode else SystemClock()
    config = load_client_config(settings.client_id)
    extractor, responder = make_llm(settings)
    db = make_engine(settings.database_url)
    calendar = SqlCalendar(db, config, clock)
    ports = Ports(extractor=extractor, responder=responder, clock=clock, calendar=calendar)
    service = ScreeningService(SqlRepository(db, clock, config.client_id), config, ports)
    runner = Runner(service, settings.debounce_seconds, settings.reminder_interval_seconds)
    queries = DashboardQueries(db, config.client_id)
    tts = None
    if settings.elevenlabs_api_key is not None:
        tts = ElevenLabsTTS(
            settings.elevenlabs_api_key.get_secret_value(),
            settings.elevenlabs_voice_id,
            settings.elevenlabs_model,
        )
    runner.add_periodic("release_orphan_bookings", calendar.release_orphans, 300)
    runner.add_periodic("retention", SqlRetention(db, config, clock).erase_expired, 3600)
    whatsapp = make_whatsapp(settings, service)
    if whatsapp is not None:
        runner.add_periodic(
            "whatsapp_outbox", whatsapp.dispatch_once, settings.whatsapp_dispatch_seconds
        )
    limiter = SqlRateLimiter(db, clock, config.client_id)
    return Container(settings, config, clock, service, runner, queries, db, limiter, tts, whatsapp)


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
    return WhatsAppChannel(service, client, settings.whatsapp_reminder_template)
