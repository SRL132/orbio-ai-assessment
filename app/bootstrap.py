"""Wire adapters to ports from settings. The only place that knows which adapters exist."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine

from app.adapters.clock.clocks import OffsetClock, SystemClock
from app.adapters.config.yaml_loader import load_client_config
from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.storage.dashboard import DashboardQueries
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.ports import Clock, Extractor, Ports, Responder
from app.settings import Settings
from app.worker.runner import Runner
from app.worker.service import ScreeningService


@dataclass
class Container:
    settings: Settings
    config: ClientConfig
    clock: Clock
    service: ScreeningService
    runner: Runner
    queries: DashboardQueries
    db: Engine

    def close(self) -> None:
        self.db.dispose()


def _llm(settings: Settings) -> tuple[Extractor, Responder]:
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
            claude = AnthropicLLM(client, settings.anthropic_model)
            return claude, claude


def build(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    clock: Clock = OffsetClock() if settings.demo_mode else SystemClock()
    config = load_client_config(settings.client_id)
    extractor, responder = _llm(settings)
    ports = Ports(extractor=extractor, responder=responder, clock=clock)
    db = make_engine(settings.database_url)
    service = ScreeningService(SqlRepository(db, clock), config, ports)
    runner = Runner(service, settings.debounce_seconds, settings.reminder_interval_seconds)
    queries = DashboardQueries(db, config.client_id)
    return Container(settings, config, clock, service, runner, queries, db)
