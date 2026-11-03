"""Prompt building shared by every LLM adapter.

System prompts depend only on the client config, so they are byte-stable across calls and cache
well. Everything that changes per turn (today's date, stage, awaited field, the message) goes in
the user message.
"""

from __future__ import annotations

import json
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from app.domain.config import ClientConfig
from app.domain.models import (
    AskField,
    ConversationState,
    FieldStatus,
    ReAsk,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"
_DROPPED_KEYWORDS = frozenset({"title", "default", "minimum", "maximum"})


@cache
def _read(name: str) -> str:
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    # Drop the version comment line: it's for humans, not for the model.
    return "\n".join(line for line in text.splitlines() if not line.startswith("<!--")).strip()


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema, in the form every provider's strict structured output accepts.

    Every property is required (optional ones are nullable instead), no others are allowed, and
    keywords some providers reject are dropped: OpenAI requires `additionalProperties: false`.
    The Pydantic model still validates the answer, range checks included.
    """

    def tidy(node: object) -> object:
        if isinstance(node, list):
            return [tidy(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {key: tidy(value) for key, value in node.items() if key not in _DROPPED_KEYWORDS}
        if "properties" in node:
            out["properties"] = {name: tidy(prop) for name, prop in node["properties"].items()}
            out["required"] = list(node["properties"])
            out["additionalProperties"] = False
        if "$defs" in node:
            out["$defs"] = {name: tidy(item) for name, item in node["$defs"].items()}
        return out

    return cast(dict[str, Any], tidy(model.model_json_schema()))


def json_instruction(model: type[BaseModel]) -> str:
    """For Claude, the schema goes in the prompt and Pydantic checks the answer. Its constrained
    output compiles a schema into a grammar, and rejects or times out on ours, with 13 nullable
    objects (tested 2026-11-03 on Haiku 5.5 and Sonnet 5.5 through OpenRouter). Sorted, so the
    system prompt stays byte-stable for caching."""
    schema = json.dumps(strict_schema(model), ensure_ascii=False, sort_keys=True)
    return (
        "\n\nAnswer with only a JSON object that matches this JSON schema, with no code fence "
        f"and no other text:\n{schema}"
    )


def strip_code_fence(text: str) -> str:
    """Some models wrap JSON in ```json fences even when asked not to."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()


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
