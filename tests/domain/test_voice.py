"""Voice channel: same engine, spoken wording."""

from datetime import date

import pytest

from app.domain import engine
from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    ConversationState,
    Schedule,
    Vehicle,
)
from app.domain.ports import Ports
from app.domain.render import check_reply, recap_spoken, speakable
from app.domain.validators import validate_full_name
from tests.conftest import ex


@pytest.fixture
def voice_state(config: ClientConfig) -> ConversationState:
    return ConversationState(client_id=config.client_id, channel="voice")


def test_voice_uses_spoken_templates_with_chat_fallback(config: ClientConfig) -> None:
    assert "·" not in config.template("es", "ask_license", "voice")
    assert "·" in config.template("es", "ask_license", "chat")
    # No voice override: falls back to the chat wording.
    assert config.template("es", "ask_start_date", "voice") == config.template(
        "es", "ask_start_date"
    )


def test_spoken_greeting_mentions_audio_is_not_kept(
    voice_state: ConversationState, config: ClientConfig, ports: Ports
) -> None:
    greeting = engine.start(voice_state, config, ports).replies[0]
    assert "no guardamos el audio" in greeting
    assert speakable(greeting)


def test_check_reply_rejects_emoji_and_option_lists_on_voice(config: ClientConfig) -> None:
    assert check_reply("¡Hola! 👋 ¿Empezamos?", config, "chat")
    assert not check_reply("¡Hola! 👋 ¿Empezamos?", config, "voice")
    assert not check_reply("Moto · Coche", config, "voice")


def test_check_reply_keeps_text_rules_on_whatsapp(config: ClientConfig) -> None:
    # WhatsApp is text: emoji and option lists are fine there, only voice can't say them.
    assert check_reply("¡Hola! 👋 ¿Empezamos?", config, "whatsapp")
    assert check_reply("Responde: Moto · Coche · Ambos · No tengo", config, "whatsapp")
    assert check_reply("¿Moto, coche o los dos?", config, "voice")


def test_spoken_recap_is_one_sentence(voice_state: ConversationState, config: ClientConfig) -> None:
    c = voice_state.candidate
    c.full_name, c.vehicle, c.city, c.zone = "Ana López", Vehicle.MOTO, "Madrid", "Retiro"
    c.availability, c.schedule = [Availability.FULL_TIME], [Schedule.EVENING]
    c.experience_years, c.platforms, c.start_date = 1, ["Glovo"], date(2026, 11, 4)
    spoken = recap_spoken(c, config, "es")
    assert "\n" not in spoken
    assert "•" not in spoken
    assert "1 año, Glovo" in spoken
    assert "4 de noviembre" in spoken
    assert "4 de noviembre" not in recap_spoken(c, config, "en")
    assert "November 4" in recap_spoken(c, config, "en")


def test_voice_conversation_end_to_end(
    voice_state: ConversationState, config: ClientConfig, ports: Ports, extractor
) -> None:
    from uuid import UUID

    from app.domain.models import InboundMessage

    state = engine.start(voice_state, config, ports).state
    replies: list[str] = []
    for extraction in [
        ex(yes_no=True),
        ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO),
        ex(city="Sevilla", availability=[Availability.WEEKENDS], schedule=[Schedule.MORNING]),
        ex(experience_years=0, start_date=date(2026, 11, 5)),
    ]:
        extractor.push(extraction)
        message = InboundMessage(
            conversation_id=UUID(int=1), text="...", received_at=ports.clock.now()
        )
        result = engine.turn(state, message, config, ports)
        state = result.state
        replies.extend(result.replies)
    assert state.channel == "voice"
    assert all(speakable(r) for r in replies)
    assert replies[-1].startswith("Repasemos.")


def test_lowercase_names_are_capitalised() -> None:
    assert validate_full_name("maría de la fuente").updates == {"full_name": "María de la Fuente"}
    assert validate_full_name("Ana McDonald").updates == {"full_name": "Ana McDonald"}
