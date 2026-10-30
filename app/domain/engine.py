"""The conversation engine: `turn(state, message, config, ports) -> TurnResult`.

The engine owns the flow from `docs/process-design.md`. It performs no I/O of its own: the LLM
and the clock are reached through ports, and persistence happens outside, from the returned
state and events.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.config import ClientConfig
from app.domain.models import (
    TERMINAL_STATUSES,
    Action,
    AnswerQuestion,
    AskField,
    Candidate,
    ConversationState,
    Disqualify,
    DomainEvent,
    FieldName,
    FieldStatus,
    HandOff,
    InboundMessage,
    InterviewBooked,
    Language,
    OfferSlots,
    Qualified,
    ReAsk,
    Recap,
    Say,
    ScheduleReminder,
    Stage,
    Status,
    TurnResult,
)
from app.domain.policy import (
    apply_extraction,
    missing_count,
    next_missing_field,
    register_failed_attempt,
)
from app.domain.ports import Ports
from app.domain.summary import build_summary
from app.domain.validators import final_review_reasons

# Intents that count as a (possibly failed) attempt at answering the awaited question.
_ANSWER_INTENTS = frozenset({"answer", "other"})


def start(state: ConversationState, config: ClientConfig, ports: Ports) -> TurnResult:
    """Open the conversation: greeting, automated-assistant disclosure and privacy notice."""
    now = ports.clock.now()
    state = state.model_copy(deep=True)
    state.started_at = now
    state.stage = Stage.CONSENT
    actions: list[Action] = [Say(template="greeting_consent")]
    events = [DomainEvent(type="conversation_started")]
    _schedule_reminder(state, config, actions)
    return _finish(state, actions, events, config, ports)


def turn(
    state: ConversationState, message: InboundMessage, config: ClientConfig, ports: Ports
) -> TurnResult:
    now = ports.clock.now()
    state = state.model_copy(deep=True)
    events = [DomainEvent(type="message_received", data={"stage": state.stage.value})]
    actions: list[Action] = []

    if state.stage == Stage.CLOSED:
        return _finish(state, [Say(template="closed")], events, config, ports)

    state.last_inbound_at = now
    state.reminders_sent = 0
    try:
        extraction = ports.extractor.extract(state, message.text, config, now.date())
    except Exception as exc:  # graceful failure: ask again, never count it against the candidate
        events.append(DomainEvent(type="extractor_failed", data={"error": type(exc).__name__}))
        actions.append(Say(template="technical_retry"))
        _schedule_reminder(state, config, actions)
        return _finish(state, actions, events, config, ports)
    state.language = extraction.language
    redact = extraction.sensitive_data
    if redact:
        actions.append(Say(template="sensitive_data"))
        events.append(DomainEvent(type="sensitive_data_redacted"))

    if extraction.intent == "abuse":
        state.abuse_strikes += 1
        events.append(DomainEvent(type="abuse_flagged", data={"strikes": state.abuse_strikes}))
        if state.abuse_strikes >= config.max_abuse_strikes:
            _add_review_reason(state, "flagged_behaviour")
            _close(state, Status.NEEDS_REVIEW, events, ports)
            actions.append(Say(template="abuse_end"))
            return _finish(state, actions, events, config, ports, redact)
        actions.append(Say(template="abuse_warning"))
        actions.extend(_ask_current(state, config))
        _schedule_reminder(state, config, actions)
        return _finish(state, actions, events, config, ports, redact)

    if extraction.intent == "human_request":
        _add_review_reason(state, "human_requested")
        _close(state, Status.NEEDS_REVIEW, events, ports)
        actions.append(HandOff(reason="human_requested"))
        return _finish(state, actions, events, config, ports, redact)

    if state.stage == Stage.SCHEDULING:
        _schedule_interview(
            state, extraction.slot_choice, extraction.yes_no, events, actions, config, ports
        )
        return _finish(state, actions, events, config, ports, redact)

    if state.stage == Stage.KEEP_ON_FILE:
        if extraction.yes_no is None:
            actions.append(Say(template="keep_on_file_reask"))
            return _finish(state, actions, events, config, ports, redact)
        state.keep_on_file = extraction.yes_no
        state.stage = Stage.CLOSED
        events.append(DomainEvent(type="keep_on_file", data={"value": extraction.yes_no}))
        template = "keep_on_file_yes" if extraction.yes_no else "keep_on_file_no"
        actions.append(Say(template=template))
        return _finish(state, actions, events, config, ports, redact)

    if state.stage == Stage.CONSENT:
        if extraction.yes_no is True:
            state.stage = Stage.COLLECTING
            events.append(DomainEvent(type="consent_given"))
        elif extraction.yes_no is False:
            events.append(DomainEvent(type="consent_declined"))
            state.dropped_at = "consent"
            _close(state, Status.ABANDONED, events, ports)
            actions.append(Say(template="consent_declined"))
            return _finish(state, actions, events, config, ports, redact)
        else:
            if extraction.question:
                actions.append(AnswerQuestion(question=extraction.question))
            actions.append(Say(template="consent_reask"))
            _schedule_reminder(state, config, actions)
            return _finish(state, actions, events, config, ports, redact)

    if extraction.intent == "question" and extraction.question:
        actions.append(AnswerQuestion(question=extraction.question))

    applied = apply_extraction(state, extraction.fields, config, now.date())
    events.extend(applied.events)
    if applied.knockout:
        _disqualify(state, applied.knockout, events, actions, ports)
        return _finish(state, actions, events, config, ports, redact)

    if state.stage == Stage.RECAP:
        if applied.progressed or applied.changed:
            actions.append(Recap())
        elif extraction.yes_no is True:
            _finalize(state, config, events, actions, ports)
            return _finish(state, actions, events, config, ports, redact)
        elif extraction.yes_no is False:
            actions.append(Say(template="ask_change"))
        else:
            actions.append(Recap())
        _schedule_reminder(state, config, actions)
        return _finish(state, actions, events, config, ports, redact)

    awaited = state.awaiting
    reask_reason: str | None = None
    if (
        awaited is not None
        and state.candidate.meta[awaited].status == FieldStatus.MISSING
        and extraction.intent in _ANSWER_INTENTS
        and not applied.progressed
    ):
        gave_up = register_failed_attempt(state, awaited, config)
        if gave_up:
            events.append(DomainEvent(type="field_gave_up", data={"field": awaited.value}))
        else:
            reask_reason = applied.rejected.get(awaited, "unclear")

    if extraction.sentiment == "frustrated" and not state.offered_human:
        state.offered_human = True
        actions.append(Say(template="offer_human"))

    nxt = next_missing_field(state.candidate, config)
    if nxt is None:
        state.stage = Stage.RECAP
        state.awaiting = None
        actions.append(Recap())
    else:
        state.awaiting = nxt
        if nxt == awaited and (reask_reason or nxt in applied.rejected):
            reason = reask_reason or applied.rejected[nxt]
            actions.append(ReAsk(field=nxt, reason=reason, offer_options=extraction.confused))
        else:
            actions.append(AskField(field=nxt, offer_options=extraction.confused))
    _schedule_reminder(state, config, actions)
    return _finish(state, actions, events, config, ports, redact)


def on_silence(state: ConversationState, config: ClientConfig, ports: Ports) -> TurnResult | None:
    """Called by the scheduler. Sends the next reminder, or marks the candidate abandoned."""
    now = ports.clock.now()
    if (
        state.status != Status.IN_PROGRESS
        or state.next_reminder_at is None
        or now < state.next_reminder_at
    ):
        return None
    state = state.model_copy(deep=True)
    events: list[DomainEvent] = []
    actions: list[Action] = []
    base = state.last_inbound_at or state.started_at or now
    sent = state.reminders_sent
    hours = config.reminder_hours
    if sent < len(hours):
        state.reminders_sent = sent + 1
        actions.append(Say(template=f"reminder_{sent + 1}"))
        events.append(DomainEvent(type="reminder_sent", data={"number": sent + 1}))
        if sent + 1 < len(hours):
            state.next_reminder_at = base + timedelta(hours=hours[sent + 1])
        else:
            state.next_reminder_at = base + timedelta(hours=hours[-1] + config.abandon_after_hours)
    else:
        drop = state.awaiting.value if state.awaiting else state.stage.value
        state.dropped_at = drop
        events.append(DomainEvent(type="abandoned", data={"drop_off": drop}))
        _close(state, Status.ABANDONED, events, ports)
    return _finish(state, actions, events, config, ports)


# --- State transitions ----------------------------------------------------------------------------


def _add_review_reason(state: ConversationState, reason: str) -> None:
    if reason not in state.review_reasons:
        state.review_reasons.append(reason)


def _close(
    state: ConversationState, status: Status, events: list[DomainEvent], ports: Ports
) -> None:
    previous = state.status
    state.status = status
    state.stage = Stage.CLOSED
    state.awaiting = None
    state.next_reminder_at = None
    state.ended_at = ports.clock.now()
    events.append(
        DomainEvent(
            type="status_changed",
            data={
                "from": previous.value,
                "to": status.value,
                "reason": ",".join(state.review_reasons) or None,
            },
        )
    )


def _disqualify(
    state: ConversationState,
    reason: str,
    events: list[DomainEvent],
    actions: list[Action],
    ports: Ports,
) -> None:
    state.disqualify_reason = reason
    _close(state, Status.DISQUALIFIED, events, ports)
    state.stage = Stage.KEEP_ON_FILE  # one last question: keep their details for future roles
    actions.append(Disqualify(reason=reason))


def _finalize(
    state: ConversationState,
    config: ClientConfig,
    events: list[DomainEvent],
    actions: list[Action],
    ports: Ports,
) -> None:
    for reason in final_review_reasons(state.candidate, config):
        _add_review_reason(state, reason)
    if state.review_reasons:
        _close(state, Status.NEEDS_REVIEW, events, ports)
        actions.append(HandOff(reason="review"))
    else:
        _close(state, Status.QUALIFIED, events, ports)
        actions.append(Qualified())
        _offer_slots(state, events, actions, config, ports)


def _offer_slots(
    state: ConversationState,
    events: list[DomainEvent],
    actions: list[Action],
    config: ClientConfig,
    ports: Ports,
) -> None:
    """Qualified: book the interview right here in the chat (no callback, process design)."""
    location_id = state.candidate.location_id
    slots: list[datetime] = []
    if ports.calendar is not None and location_id:
        after = ports.clock.now() + timedelta(days=config.interview.min_days_ahead)
        slots = ports.calendar.available_slots(location_id, after, config.interview.offer_count)
    if not slots or location_id is None:
        state.stage = Stage.CLOSED
        actions.append(Say(template="qualified_no_slots"))
        return
    state.stage = Stage.SCHEDULING
    state.offered_slots = slots
    events.append(DomainEvent(type="slots_offered", data={"count": len(slots)}))
    actions.append(OfferSlots(slots=slots, location_id=location_id))


def _schedule_interview(
    state: ConversationState,
    choice: int | None,
    yes_no: bool | None,
    events: list[DomainEvent],
    actions: list[Action],
    config: ClientConfig,
    ports: Ports,
) -> None:
    location_id = state.candidate.location_id or ""
    if choice is not None and 1 <= choice <= len(state.offered_slots) and ports.calendar:
        slot = state.offered_slots[choice - 1]
        if ports.calendar.book(location_id, slot, state.conversation_id):
            state.interview_at = slot
            state.offered_slots = []
            state.stage = Stage.CLOSED
            events.append(DomainEvent(type="interview_booked", data={"at": slot.isoformat()}))
            actions.append(InterviewBooked(at=slot, location_id=location_id))
            return
        events.append(DomainEvent(type="slot_taken", data={"at": slot.isoformat()}))
        actions.append(Say(template="slot_taken"))
        _offer_slots(state, events, actions, config, ports)
        return
    if yes_no is False:
        state.no_slot_fits = True
        state.offered_slots = []
        state.stage = Stage.CLOSED
        events.append(DomainEvent(type="no_slot_fits"))
        actions.append(Say(template="no_slot_fits"))
        return
    actions.append(OfferSlots(slots=state.offered_slots, location_id=location_id))


def _ask_current(state: ConversationState, config: ClientConfig) -> list[Action]:
    if state.stage == Stage.CONSENT:
        return [Say(template="consent_reask")]
    if state.stage == Stage.RECAP:
        return [Recap()]
    nxt = state.awaiting or next_missing_field(state.candidate, config)
    return [AskField(field=nxt)] if nxt else []


def _schedule_reminder(
    state: ConversationState, config: ClientConfig, actions: list[Action]
) -> None:
    if state.status != Status.IN_PROGRESS or not config.reminder_hours:
        return
    base = state.last_inbound_at or state.started_at
    if base is None:
        return
    state.next_reminder_at = base + timedelta(hours=config.reminder_hours[0])
    actions.append(ScheduleReminder(at=state.next_reminder_at))


# --- Rendering ------------------------------------------------------------------------------------


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def check_reply(text: str, config: ClientConfig) -> bool:
    """Output guardrail: one short message with at most one question."""
    stripped = text.strip()
    return 0 < len(stripped) <= config.max_reply_chars and stripped.count("?") <= 1


def _fill(template: str, state: ConversationState, config: ClientConfig, **extra: str) -> str:
    first_name = (state.candidate.full_name or "").split(" ")[0]
    context = _SafeDict(
        name=first_name,
        persona=config.persona.name,
        company=config.persona.company,
        missing_count=str(missing_count(state.candidate)),
        **extra,
    )
    return template.format_map(context)


def _label(config: ClientConfig, language: Language, key: str) -> str:
    return config.templates[language].get(key, key)


def recap_summary(candidate: Candidate, config: ClientConfig, language: Language) -> str:
    def values(prefix: str, items: list[str]) -> str:
        return ", ".join(_label(config, language, f"{prefix}{item}") for item in items)

    lines: list[tuple[FieldName, str]] = [
        (FieldName.FULL_NAME, candidate.full_name or "-"),
        (
            FieldName.LICENSE,
            _label(config, language, f"value_{candidate.vehicle.value}")
            if candidate.vehicle
            else "-",
        ),
        (
            FieldName.LOCATION,
            ", ".join(p for p in (candidate.zone, candidate.city, candidate.postal_code) if p)
            or "-",
        ),
        (FieldName.AVAILABILITY, values("value_", [a.value for a in candidate.availability])),
        (FieldName.SCHEDULE, values("value_", [s.value for s in candidate.schedule])),
        (
            FieldName.EXPERIENCE,
            _label(config, language, "experience_value").format(
                years=f"{candidate.experience_years:g}"
                if candidate.experience_years is not None
                else "-",
                platforms=", ".join(candidate.platforms) or "-",
            ),
        ),
        (
            FieldName.START_DATE,
            candidate.start_date.strftime("%d/%m/%Y") if candidate.start_date else "-",
        ),
    ]
    return "\n".join(
        f"• {_label(config, language, f'label_{name.value}')}: {value}" for name, value in lines
    )


def render(
    actions: list[Action], state: ConversationState, config: ClientConfig, ports: Ports
) -> tuple[list[str], list[DomainEvent]]:
    replies: list[str] = []
    events: list[DomainEvent] = []
    lang = state.language
    for action in actions:
        match action:
            case Say(template=key):
                replies.append(_fill(config.template(lang, key), state, config))
            case AskField() | ReAsk() | AnswerQuestion():
                text: str | None
                try:
                    text = ports.responder.compose(action, state, config)
                except Exception as exc:  # graceful failure: fall back to a template
                    events.append(
                        DomainEvent(type="responder_failed", data={"error": type(exc).__name__})
                    )
                    text = None
                if text is None or not check_reply(text, config):
                    if text is not None:
                        events.append(
                            DomainEvent(type="reply_rejected", data={"kind": action.kind})
                        )
                    text = _fill(config.template(lang, _fallback_key(action)), state, config)
                replies.append(text)
            case Disqualify(reason=reason):
                replies.append(
                    _fill(config.template(lang, f"disqualified_{reason}"), state, config)
                )
            case HandOff(reason=reason):
                key = "handoff_human" if reason == "human_requested" else "needs_review"
                replies.append(_fill(config.template(lang, key), state, config))
            case Recap():
                summary = recap_summary(state.candidate, config, lang)
                replies.append(
                    _fill(config.template(lang, "recap"), state, config, summary=summary)
                )
            case Qualified():
                replies.append(_fill(config.template(lang, "qualified"), state, config))
            case OfferSlots(slots=slots, location_id=location_id):
                listed = "\n".join(
                    f"{i}) {_slot_text(slot, location_id, config, lang)}"
                    for i, slot in enumerate(slots, start=1)
                )
                replies.append(
                    _fill(config.template(lang, "offer_slots"), state, config, slots=listed)
                )
            case InterviewBooked(at=at, location_id=location_id):
                when = _slot_text(at, location_id, config, lang)
                replies.append(
                    _fill(config.template(lang, "interview_booked"), state, config, when=when)
                )
            case ScheduleReminder():
                pass
    return replies, events


def _slot_text(slot: datetime, location_id: str, config: ClientConfig, language: Language) -> str:
    """'jue 5/11 10:00' in the location's own timezone."""
    location = config.location(location_id)
    local = slot.astimezone(ZoneInfo(location.timezone)) if location else slot
    weekday = config.template(language, "weekdays").split(",")[local.weekday()]
    return f"{weekday} {local.day}/{local.month} {local:%H:%M}"


def _fallback_key(action: AskField | ReAsk | AnswerQuestion) -> str:
    match action:
        case AskField(field=name):
            return f"ask_{name.value}"
        case ReAsk(field=name):
            return f"reask_{name.value}"
        case AnswerQuestion():
            return "answer_unknown"


def _finish(
    state: ConversationState,
    actions: list[Action],
    events: list[DomainEvent],
    config: ClientConfig,
    ports: Ports,
    redact: bool = False,
) -> TurnResult:
    if state.status in TERMINAL_STATUSES:
        state.summary = build_summary(state, config)
    replies, render_events = render(actions, state, config, ports)
    return TurnResult(
        state=state,
        actions=actions,
        replies=replies,
        events=[*events, *render_events],
        redact_inbound=redact,
    )


# --- Recruiter actions ----------------------------------------------------------------------------

OVERRIDABLE = frozenset({Status.QUALIFIED, Status.DISQUALIFIED, Status.NEEDS_REVIEW})


def override_status(
    state: ConversationState,
    status: Status,
    reason: str,
    actor: str,
    config: ClientConfig,
    ports: Ports,
) -> tuple[ConversationState, list[DomainEvent]]:
    """A recruiter changes the outcome. Needs a reason; the original decision stays in the log."""
    if status not in OVERRIDABLE:
        raise ValueError(f"cannot override to {status.value}")
    if not reason.strip():
        raise ValueError("an override needs a reason")
    state = state.model_copy(deep=True)
    previous = state.status
    state.status = status
    state.stage = Stage.CLOSED
    state.awaiting = None
    state.next_reminder_at = None
    state.ended_at = state.ended_at or ports.clock.now()
    state.summary = build_summary(state, config)
    event = DomainEvent(
        type="override",
        data={"from": previous.value, "to": status.value, "reason": reason.strip(), "actor": actor},
    )
    return state, [event]
