"""Prompt building shared by every LLM adapter.

System prompts depend only on the client config, so they are byte-stable across calls and cache
well. Everything that changes per turn (today's date, stage, awaited field, the message) goes in
the user message.
"""

from __future__ import annotations

from datetime import date
from functools import cache
from pathlib import Path

from app.domain.config import ClientConfig
from app.domain.models import (
    AskField,
    ConversationState,
    FieldStatus,
    ReAsk,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"


@cache
def _read(name: str) -> str:
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    # Drop the version comment line: it's for humans, not for the model.
    return "\n".join(line for line in text.splitlines() if not line.startswith("<!--")).strip()


def extractor_system(config: ClientConfig) -> str:
    cities = sorted(
        {loc.city for loc in config.locations}
        | {a for loc in config.locations for a in loc.aliases}
    )
    nearby = sorted({town for loc in config.locations for town in loc.nearby})
    faq = "\n".join(f"- {entry.id}: {entry.question}" for entry in config.faq)
    return _read("extractor_system.md").format(
        company=config.persona.company,
        cities=", ".join(cities),
        nearby=", ".join(nearby) or "(none)",
        platforms=", ".join(config.known_platforms),
        faq=faq or "(none)",
    )


def responder_system(config: ClientConfig) -> str:
    return _read("responder_system.md").format(
        persona=config.persona.name, company=config.persona.company
    )


def extractor_user(state: ConversationState, text: str, today: date) -> str:
    known = [
        name.value
        for name, meta in state.candidate.meta.items()
        if meta.status != FieldStatus.MISSING
    ]
    asked = state.awaiting.value if state.awaiting else state.stage.value
    if state.confirming is not None:  # a yes/no check of what we understood: read yes_no
        asked = f"confirming the {state.confirming.field.value} we understood (a yes/no question)"
    return (
        f"Today: {today.isoformat()} ({today.strftime('%A')})\n"
        f"Current language: {state.language}\n"
        f"Channel: {_extract_channel_note(state)}\n"
        f"Stage: {state.stage.value}\n"
        f"The assistant just asked about: {asked}\n"
        f"Already answered: {', '.join(known) or 'nothing yet'}\n"
        f"{_recent_block(state)}"
        "Candidate message:\n"
        f"<<<\n{text}\n>>>"
    )


def _recent_block(state: ConversationState) -> str:
    """The last messages, as context. Everything inside is data, like the candidate's message."""
    if not state.recent_messages:
        return ""
    lines = "\n".join(
        f"{'Candidate' if direction == 'in' else 'Assistant'}: {text}"
        for direction, text in state.recent_messages
    )
    return f"Recent conversation (context only):\n<<<\n{lines}\n>>>\n"


def responder_user(action: AskField | ReAsk, state: ConversationState, config: ClientConfig) -> str:
    lang = state.language
    first_name = (state.candidate.full_name or "").split(" ")[0] or "(unknown)"
    match action:
        case AskField(field=name, offer_options=options):
            task = f"Ask for: {name.value}." + (
                " The candidate seems confused: offer the options." if options else ""
            )
            reference = config.template(lang, f"ask_{name.value}", state.channel)
        case ReAsk(field=name, reason=reason, offer_options=options):
            task = (
                f"Ask again for: {name.value}. The previous answer could not be used "
                f"(reason: {reason}). Acknowledge kindly and give an example of a good answer."
                + (" Offer the options." if options else "")
            )
            reference = config.template(lang, f"reask_{name.value}", state.channel)
    return (
        f"Language: {lang}\n"
        f"Channel: {_respond_channel_note(state)}\n"
        f"Candidate first name: {first_name}\n"
        f"{_recent_block(state)}"
        f"Task: {task}\n"
        f"Reference wording (keep the meaning, you may rephrase): {reference}"
    )


def _extract_channel_note(state: ConversationState) -> str:
    if state.channel == "voice":
        return (
            "voice. The message is speech-to-text output: no punctuation, numbers may be spelled "
            "out ('veintiocho cero uno cero' = 28010, 'dos años y medio' = 2.5), and names or "
            "places may be misheard; read them generously."
        )
    return "chat"


def _respond_channel_note(state: ConversationState) -> str:
    if state.channel == "voice":
        return (
            "voice. Your message will be read aloud: no emoji, symbols or lists; say any options "
            "as a natural sentence; keep it short."
        )
    return "chat"
