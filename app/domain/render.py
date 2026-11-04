"""Rendering: the engine's actions become the candidate-facing messages.

Fixed texts come from the client's templates (per language and channel); questions and answers
come from the responder port, checked by `check_reply` and replaced by a template if they fail.
`template_problems` checks a config against everything this module can render, so a missing
template or placeholder fails at startup instead of mid-conversation.
"""

from __future__ import annotations

import re
import string
from datetime import date, datetime
from typing import get_args
from zoneinfo import ZoneInfo

from app.domain.config import ClientConfig
from app.domain.models import (
    KNOCKOUT_REASONS,
    Action,
    AnswerQuestion,
    AskField,
    Availability,
    Candidate,
    Channel,
    ConfirmField,
    ConversationState,
    Disqualify,
    DomainEvent,
    EventType,
    FieldName,
    HandOff,
    InterviewBooked,
    Language,
    OfferSlots,
    Qualified,
    ReAsk,
    Recap,
    Remind,
    Say,
    SayTemplate,
    Schedule,
    ScheduleReminder,
    Vehicle,
)
from app.domain.policy import missing_count
from app.domain.ports import Ports

_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")


def check_reply(text: str, config: ClientConfig, channel: Channel = "chat") -> bool:
    """Output guardrail: one short message with at most one question.

    Voice replies are read aloud, so they also can't contain emoji or "·" option lists.
    """
    stripped = text.strip()
    ok = 0 < len(stripped) <= config.max_reply_chars and stripped.count("?") <= 1
    return ok and (channel != "voice" or speakable(stripped))


def speakable(text: str) -> bool:
    """Can be read aloud as is: no emoji and no "·" option lists."""
    return not _EMOJI.search(text) and "·" not in text


def render(
    actions: list[Action], state: ConversationState, config: ClientConfig, ports: Ports
) -> tuple[list[str], list[DomainEvent]]:
    replies: list[str] = []
    events: list[DomainEvent] = []
    lang = state.language
    channel = state.channel

    def fill(key: str, **extra: str) -> str:
        return _fill(config.template(lang, key, channel), state, config, **extra)

    for action in actions:
        match action:
            case Say(template=key):
                replies.append(fill(key))
            case Remind(number=number):
                replies.append(fill(_reminder_key(number, state)))
            case AskField(offer_options=False) if channel in config.template_questions_on:
                replies.append(fill(_fallback_key(action)))
            case AnswerQuestion(faq_id=faq_id):
                # Approved text only: the LLM picks the entry but can't write the answer, and an
                # id the config doesn't know gets the "a recruiter will confirm" template.
                replies.append(config.faq_answer(faq_id, lang) or fill("answer_unknown"))
            case AskField() | ReAsk():
                text: str | None
                try:
                    text = ports.responder.compose(action, state, config)
                except Exception as exc:  # graceful failure: fall back to a template
                    events.append(
                        DomainEvent(
                            type=EventType.RESPONDER_FAILED, data={"error": type(exc).__name__}
                        )
                    )
                    text = None
                if text is None or not check_reply(text, config, channel):
                    if text is not None:
                        events.append(
                            DomainEvent(type=EventType.REPLY_REJECTED, data={"kind": action.kind})
                        )
                    text = fill(_fallback_key(action))
                replies.append(text)
            case Disqualify(reason=reason):
                replies.append(fill(f"disqualified_{reason.value}"))
            case HandOff(reason=reason):
                replies.append(
                    fill("handoff_human" if reason == "human_requested" else "needs_review")
                )
            case Recap():
                summary = (
                    recap_spoken(state.candidate, config, lang)
                    if channel == "voice"
                    else recap_summary(state.candidate, config, lang)
                )
                replies.append(fill("recap", summary=summary))
            case Qualified():
                replies.append(fill("qualified"))
            case ConfirmField(field=name):
                replies.append(fill("confirm_value", value=_pending_text(name, state, config)))
            case OfferSlots(slots=slots, location_id=location_id):
                replies.append(
                    fill("offer_slots", slots=_slot_list(slots, location_id, state, config))
                )
            case InterviewBooked(at=at, location_id=location_id):
                replies.append(
                    fill(
                        "interview_booked", when=_slot_text(at, location_id, config, lang, channel)
                    )
                )
            case ScheduleReminder():
                pass
    return replies, events


def _reminder_key(number: int, state: ConversationState) -> str:
    """The reminder that reads right: at the recap no question is left, only the confirmation."""
    missing = missing_count(state.candidate)
    suffix = {0: "_recap", 1: "_one"}.get(missing, "")
    return f"reminder_{number}{suffix}"


def _pending_text(name: FieldName, state: ConversationState, config: ClientConfig) -> str:
    """The value waiting for confirmation, as the recap would show it (or the geocoded place)."""
    pending = state.confirming
    if pending is None or pending.field != name:
        return ""
    if pending.place:
        return pending.place
    draft = Candidate.model_validate({**state.candidate.model_dump(), **pending.values})
    return field_values(draft, config, state.language, state.channel)[name] or ""


# --- Candidate record as text ---------------------------------------------------------------------


def field_values(
    candidate: Candidate, config: ClientConfig, language: Language, channel: Channel = "chat"
) -> dict[FieldName, str | None]:
    """Each screening field as the candidate (recap) or recruiter (dashboard) reads it; None if
    missing."""

    def label(key: str) -> str:
        return config.templates[language].get(key, key)

    def listed(values: list[Availability] | list[Schedule]) -> str | None:
        return ", ".join(label(f"value_{v.value}") for v in values) or None

    years = candidate.experience_years
    experience = None
    if years == 0 and not candidate.platforms:
        experience = label("experience_none")
    elif years is not None and not candidate.platforms:
        experience = label("experience_years_one" if years == 1 else "experience_years").format(
            years=f"{years:g}"
        )
    elif years is not None:
        experience = label("experience_value_one" if years == 1 else "experience_value").format(
            years=f"{years:g}", platforms=", ".join(candidate.platforms)
        )
    license_text = None
    if candidate.has_license is False:
        license_text = label("value_no_license")
    elif candidate.vehicle:
        license_text = label(f"value_{candidate.vehicle.value}")
        held = candidate.license_years
        if candidate.license_type and held is not None:
            key = "license_with_years_one" if held == 1 else "license_with_years"
            license_text = label(key).format(
                vehicle=license_text, license_type=candidate.license_type, years=f"{held:g}"
            )
        elif candidate.license_type:
            license_text = label("license_with_type").format(
                vehicle=license_text, license_type=candidate.license_type
            )
    location = ", ".join(p for p in (candidate.zone, candidate.city, candidate.postal_code) if p)
    return {
        FieldName.FULL_NAME: candidate.full_name,
        FieldName.LICENSE: license_text,
        FieldName.LOCATION: location or None,
        FieldName.AVAILABILITY: listed(candidate.availability),
        FieldName.SCHEDULE: listed(candidate.schedule),
        FieldName.EXPERIENCE: experience,
        FieldName.START_DATE: (
            _date_text(candidate.start_date, config, language, channel)
            if candidate.start_date
            else None
        ),
    }


def recap_summary(
    candidate: Candidate, config: ClientConfig, language: Language, channel: Channel = "chat"
) -> str:
    labels = config.templates[language]
    return "\n".join(
        f"• {labels.get(f'label_{name.value}', name.value)}: {value or '-'}"
        for name, value in field_values(candidate, config, language, channel).items()
    )


def recap_spoken(candidate: Candidate, config: ClientConfig, language: Language) -> str:
    """The recap as one sentence for text-to-speech: no bullets, no symbols."""
    lines = recap_summary(candidate, config, language, "voice").splitlines()
    return "; ".join(line.removeprefix("• ").replace(" · ", ", ") for line in lines)


# --- Helpers --------------------------------------------------------------------------------------


# ", {name}" / " {name}" before punctuation: dropped when the name isn't known yet.
_NAME_SLOT = re.compile(r",?\s*\{name\}(?=\s*[.!?,;:])")


def _fill(template: str, state: ConversationState, config: ClientConfig, **extra: str) -> str:
    first_name = (state.candidate.full_name or "").split(" ")[0]
    if not first_name:  # "Gracias, {name}." without a name would read "Gracias, ."
        template = _NAME_SLOT.sub("", template)
    return template.format(
        name=first_name,
        persona=config.persona.name,
        company=config.persona.company,
        missing_count=str(missing_count(state.candidate)),
        privacy_url=config.privacy_url,
        **extra,
    )


def _date_text(day: date, config: ClientConfig, language: Language, channel: Channel) -> str:
    """'04/11/2026' in chat; '4 de noviembre' / 'November 4' when it will be read aloud."""
    if channel == "voice":
        months = config.template(language, "months", channel).split(",")
        return config.template(language, "date_format", channel).format(
            day=day.day, month=months[day.month - 1], year=day.year
        )
    return day.strftime("%d/%m/%Y")


def _slot_list(
    slots: list[datetime], location_id: str, state: ConversationState, config: ClientConfig
) -> str:
    lang, channel = state.language, state.channel
    if channel == "voice":
        ordinals = config.template(lang, "slot_ordinals", channel).split(",")
        return "; ".join(
            f"{ordinals[i]}, {_slot_text(slot, location_id, config, lang, channel)}"
            for i, slot in enumerate(slots)
        )
    return "\n".join(
        f"{i}) {_slot_text(slot, location_id, config, lang)}" for i, slot in enumerate(slots, 1)
    )


def _slot_text(
    slot: datetime,
    location_id: str,
    config: ClientConfig,
    language: Language,
    channel: Channel = "chat",
) -> str:
    """'jue 5/11 10:00' (chat) or 'jueves 5 a las 10:00' (voice), in the location's timezone."""
    location = config.location(location_id)
    local = slot.astimezone(ZoneInfo(location.timezone)) if location else slot
    weekday = config.template(language, "weekdays", channel).split(",")[local.weekday()]
    return config.template(language, "slot_format", channel).format(
        weekday=weekday, day=local.day, month=local.month, time=f"{local:%H:%M}"
    )


def _fallback_key(action: AskField | ReAsk) -> str:
    match action:
        case AskField(field=name):
            return f"ask_{name.value}"
        case ReAsk(field=name):
            return f"reask_{name.value}"


# --- Config check ---------------------------------------------------------------------------------

_COMMON = frozenset({"name", "persona", "company", "missing_count", "privacy_url"})
_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "recap": _COMMON | {"summary"},
    "confirm_value": _COMMON | {"value"},
    "offer_slots": _COMMON | {"slots"},
    "interview_booked": _COMMON | {"when"},
    "slot_format": frozenset({"weekday", "day", "month", "time"}),
    "date_format": frozenset({"day", "month", "year"}),
    "experience_value": frozenset({"years", "platforms"}),
    "experience_value_one": frozenset({"years", "platforms"}),
    "experience_years": frozenset({"years"}),
    "experience_years_one": frozenset({"years"}),
    "license_with_type": frozenset({"vehicle", "license_type"}),
    "license_with_years": frozenset({"vehicle", "license_type", "years"}),
    "license_with_years_one": frozenset({"vehicle", "license_type", "years"}),
}
# Comma-separated lists and how many items each needs.
_LISTS = {"weekdays": 7, "months": 12, "slot_ordinals": 0}
_VOICE_ONLY = frozenset({"months", "date_format", "slot_ordinals"})


def required_templates(config: ClientConfig) -> set[str]:
    """Every chat template key the domain can render (voice falls back to chat)."""
    reminders = max(
        [len(config.reminder_hours), *(len(h) for h in config.channel_reminder_hours.values())]
    )
    values = [*Vehicle, *Availability, *Schedule]
    return {
        *get_args(SayTemplate),
        *(f"{prefix}_{name.value}" for name in FieldName for prefix in ("ask", "reask", "label")),
        *(f"value_{value.value}" for value in values),
        *(f"disqualified_{reason.value}" for reason in KNOCKOUT_REASONS),
        *(
            f"reminder_{n}{suffix}"
            for n in range(1, reminders + 1)
            for suffix in ("", "_one", "_recap")
        ),
        "answer_unknown",
        "handoff_human",
        "needs_review",
        "recap",
        "confirm_value",
        "qualified",
        "offer_slots",
        "interview_booked",
        "slot_format",
        "weekdays",
        "experience_value",
        "experience_value_one",
        "experience_years",
        "experience_years_one",
        "experience_none",
        "value_no_license",
        "license_with_type",
        "license_with_years",
        "license_with_years_one",
        "text_only",
    }


def template_problems(config: ClientConfig) -> list[str]:
    """What's wrong with the config's templates: missing keys, unknown placeholders, short lists."""
    problems: list[str] = []
    required = required_templates(config)
    for language, chat in config.templates.items():
        voice = config.voice_templates.get(language, {})
        problems += [f"{language}/chat: missing {key}" for key in sorted(required - chat.keys())]
        problems += [
            f"{language}/voice: missing {key}" for key in sorted(_VOICE_ONLY - voice.keys())
        ]
        for channel, templates in (("chat", chat), ("voice", voice)):
            for key, text in templates.items():
                where = f"{language}/{channel}: {key}"
                if key in _LISTS:
                    needed = _LISTS[key] or config.interview.offer_count
                    if len(text.split(",")) < needed:
                        problems.append(f"{where} needs {needed} comma-separated items")
                    continue
                try:
                    fields = {f for _, f, _, _ in string.Formatter().parse(text) if f is not None}
                except ValueError as exc:
                    problems.append(f"{where}: {exc}")
                    continue
                unknown = fields - _PLACEHOLDERS.get(key, _COMMON)
                if unknown:
                    problems.append(f"{where}: unknown placeholder {', '.join(sorted(unknown))}")
    return problems
