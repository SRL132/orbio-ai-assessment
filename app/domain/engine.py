"""The conversation engine: `turn(state, message, config, ports) -> TurnResult`.

The engine owns the flow from `docs/process-design.md`. It performs no I/O of its own: the LLM,
the calendar and the clock are reached through ports, and persistence happens outside, from the
returned state and events. Turning actions into text is `render.py`'s job.

Each entry point builds a `_Turn`, lets one handler per stage add actions and events to it, and
ends with `_finish`, which schedules the next reminder and renders the replies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.config import ClientConfig
from app.domain.models import (
    TERMINAL_STATUSES,
    Action,
    AnswerQuestion,
    AskField,
    ConversationState,
    Disqualify,
    DomainEvent,
    EventType,
    ExtractionResult,
    FieldStatus,
    HandOff,
    InboundMessage,
    InterviewBooked,
    InvalidReason,
    OfferSlots,
    Qualified,
    ReAsk,
    Reason,
    Recap,
    Remind,
    Say,
    SayTemplate,
    ScheduleReminder,
    Stage,
    Status,
    TurnResult,
)
from app.domain.policy import (
    ApplyResult,
    add_review_reason,
    apply_extraction,
    next_missing_field,
    progress,
    register_failed_attempt,
)
from app.domain.ports import Ports
from app.domain.render import render
from app.domain.summary import build_summary
from app.domain.validators import final_review_reasons

# Intents that count as a (possibly failed) attempt at answering the awaited question.
_ANSWER_INTENTS = frozenset({"answer", "other"})

EventValue = str | int | float | bool | None


@dataclass
class _Turn:
    """One engine step in progress: a working copy of the state and what the step decided."""

    state: ConversationState
    config: ClientConfig
    ports: Ports
    now: datetime
    actions: list[Action] = field(default_factory=list)
    events: list[DomainEvent] = field(default_factory=list)
    redact: bool = False

    @classmethod
    def begin(cls, state: ConversationState, config: ClientConfig, ports: Ports) -> _Turn:
        return cls(state.model_copy(deep=True), config, ports, ports.clock.now())

    def say(self, template: SayTemplate) -> None:
        self.actions.append(Say(template=template))

    def event(self, type: EventType, **data: EventValue) -> None:
        self.events.append(DomainEvent(type=type, data=data))


# --- Entry points ---------------------------------------------------------------------------------


def start(state: ConversationState, config: ClientConfig, ports: Ports) -> TurnResult:
    """Open the conversation: greeting, automated-assistant disclosure and privacy notice."""
    t = _Turn.begin(state, config, ports)
    t.state.started_at = t.now
    t.state.stage = Stage.CONSENT
    t.say("greeting_consent")
    t.event(EventType.CONVERSATION_STARTED)
    return _finish(t)


def turn(
    state: ConversationState, message: InboundMessage, config: ClientConfig, ports: Ports
) -> TurnResult:
    t = _Turn.begin(state, config, ports)
    t.event(EventType.MESSAGE_RECEIVED, stage=t.state.stage.value)
    if t.state.stage == Stage.CLOSED:
        t.say("closed")
        return _finish(t)

    t.state.last_inbound_at = t.now
    t.state.reminders_sent = 0
    try:
        extraction = ports.extractor.extract(t.state, message.text, config, t.now.date())
    except Exception as exc:  # graceful failure: ask again, never count it against the candidate
        t.event(EventType.EXTRACTOR_FAILED, error=type(exc).__name__)
        t.say("technical_retry")
        return _finish(t)

    t.state.language = extraction.language
    if extraction.sensitive_data:
        t.redact = True
        t.say("sensitive_data")
        t.event(EventType.SENSITIVE_DATA_REDACTED)

    if extraction.intent == "abuse":
        _on_abuse(t)
    elif extraction.intent == "human_request":
        add_review_reason(t.state, Reason.HUMAN_REQUESTED)
        _close(t, Status.NEEDS_REVIEW)
        t.actions.append(HandOff(reason="human_requested"))
    else:
        _STAGE_HANDLERS[t.state.stage](t, extraction)
    return _finish(t)


def on_silence(state: ConversationState, config: ClientConfig, ports: Ports) -> TurnResult | None:
    """Called by the scheduler. Sends the next reminder, or marks the candidate abandoned."""
    now = ports.clock.now()
    if (
        state.status != Status.IN_PROGRESS
        or state.next_reminder_at is None
        or now < state.next_reminder_at
    ):
        return None
    t = _Turn.begin(state, config, ports)
    base = t.state.last_inbound_at or t.state.started_at or now
    hours = config.reminders_for(t.state.channel)
    number = t.state.reminders_sent + 1
    if number <= len(hours):
        t.state.reminders_sent = number
        t.actions.append(Remind(number=number))
        t.event(EventType.REMINDER_SENT, number=number)
        if number < len(hours):
            t.state.next_reminder_at = base + timedelta(hours=hours[number])
        else:
            t.state.next_reminder_at = base + timedelta(
                hours=hours[-1] + config.abandon_after_hours
            )
    else:
        drop = t.state.awaiting.value if t.state.awaiting else t.state.stage.value
        t.state.dropped_at = drop
        t.event(EventType.ABANDONED, drop_off=drop)
        _close(t, Status.ABANDONED)
    return _finish(t, schedule_reminder=False)


# --- Stage handlers -------------------------------------------------------------------------------


def _on_abuse(t: _Turn) -> None:
    t.state.abuse_strikes += 1
    t.event(EventType.ABUSE_FLAGGED, strikes=t.state.abuse_strikes)
    if t.state.abuse_strikes >= t.config.max_abuse_strikes:
        add_review_reason(t.state, Reason.FLAGGED_BEHAVIOUR)
        _close(t, Status.NEEDS_REVIEW)
        t.say("abuse_end")
        return
    t.say("abuse_warning")
    _ask_current(t)


def _on_consent(t: _Turn, extraction: ExtractionResult) -> None:
    if extraction.yes_no is True:
        t.state.stage = Stage.COLLECTING
        t.event(EventType.CONSENT_GIVEN)
        _on_answers(t, extraction)
    elif extraction.yes_no is False:
        t.event(EventType.CONSENT_DECLINED)
        t.state.dropped_at = "consent"
        _close(t, Status.ABANDONED)
        t.say("consent_declined")
    else:
        if extraction.question:
            t.actions.append(AnswerQuestion(question=extraction.question))
        t.say("consent_reask")


def _on_answers(t: _Turn, extraction: ExtractionResult) -> None:
    """Collecting and recap: apply the extracted fields, then ask, recap or finish."""
    if extraction.intent == "question" and extraction.question:
        t.actions.append(AnswerQuestion(question=extraction.question))
    applied = apply_extraction(t.state, extraction.fields, t.config, t.now.date())
    t.events.extend(applied.events)
    if applied.knockout:
        _disqualify(t, applied.knockout)
    elif t.state.stage == Stage.RECAP:
        _on_recap_reply(t, extraction, applied)
    else:
        _ask_next(t, extraction, applied)


def _on_recap_reply(t: _Turn, extraction: ExtractionResult, applied: ApplyResult) -> None:
    if applied.added or applied.changed:
        t.actions.append(Recap())  # they corrected something: confirm the new version
    elif extraction.yes_no is True:
        _finalize(t)
    elif extraction.yes_no is False:
        t.say("ask_change")
    else:
        t.actions.append(Recap())


def _ask_next(t: _Turn, extraction: ExtractionResult, applied: ApplyResult) -> None:
    state = t.state
    awaited = state.awaiting
    reask_reason: InvalidReason | None = None
    if (
        awaited is not None
        and state.candidate.meta[awaited].status == FieldStatus.MISSING
        and extraction.intent in _ANSWER_INTENTS
        and not applied.progressed
    ):
        if register_failed_attempt(state, awaited, t.config):
            t.event(EventType.FIELD_GAVE_UP, field=awaited.value)
        else:
            reask_reason = applied.rejected.get(awaited, "unclear")

    if extraction.sentiment == "frustrated" and not state.offered_human:
        state.offered_human = True
        t.say("offer_human")

    nxt = next_missing_field(state.candidate, t.config)
    if nxt is None:
        state.stage = Stage.RECAP
        state.awaiting = None
        t.actions.append(Recap())
        return
    state.awaiting = nxt
    reason = (reask_reason or applied.rejected.get(nxt)) if nxt == awaited else None
    if reason:
        t.actions.append(ReAsk(field=nxt, reason=reason, offer_options=extraction.confused))
    else:
        t.actions.append(AskField(field=nxt, offer_options=extraction.confused))


def _on_keep_on_file(t: _Turn, extraction: ExtractionResult) -> None:
    if extraction.yes_no is None:
        t.say("keep_on_file_reask")
        return
    t.state.keep_on_file = extraction.yes_no
    t.state.stage = Stage.CLOSED
    t.event(EventType.KEEP_ON_FILE, value=extraction.yes_no)
    t.say("keep_on_file_yes" if extraction.yes_no else "keep_on_file_no")


def _on_scheduling(t: _Turn, extraction: ExtractionResult) -> None:
    state, calendar = t.state, t.ports.calendar
    location_id = state.candidate.location_id or ""
    choice = extraction.slot_choice
    if choice is not None and 1 <= choice <= len(state.offered_slots) and calendar:
        slot = state.offered_slots[choice - 1]
        if calendar.book(location_id, slot, state.conversation_id):
            state.interview_at = slot
            state.offered_slots = []
            state.stage = Stage.CLOSED
            t.event(EventType.INTERVIEW_BOOKED, at=slot.isoformat())
            t.actions.append(InterviewBooked(at=slot, location_id=location_id))
            return
        t.event(EventType.SLOT_TAKEN, at=slot.isoformat())
        t.say("slot_taken")
        _offer_slots(t)
        return
    if extraction.yes_no is False:
        state.no_slot_fits = True
        state.offered_slots = []
        state.stage = Stage.CLOSED
        t.event(EventType.NO_SLOT_FITS)
        t.say("no_slot_fits")
        return
    t.actions.append(OfferSlots(slots=state.offered_slots, location_id=location_id))


_STAGE_HANDLERS: dict[Stage, Callable[[_Turn, ExtractionResult], None]] = {
    Stage.CONSENT: _on_consent,
    Stage.COLLECTING: _on_answers,
    Stage.RECAP: _on_answers,
    Stage.KEEP_ON_FILE: _on_keep_on_file,
    Stage.SCHEDULING: _on_scheduling,
}


# --- Outcomes -------------------------------------------------------------------------------------


def _end(state: ConversationState, status: Status, stage: Stage = Stage.CLOSED) -> Status:
    """Set the final status and stop asking and reminding. Returns the previous status."""
    previous = state.status
    state.status = status
    state.stage = stage
    state.awaiting = None
    state.next_reminder_at = None
    return previous


def _close(t: _Turn, status: Status, stage: Stage = Stage.CLOSED) -> None:
    previous = _end(t.state, status, stage)
    t.state.ended_at = t.now
    reason = ",".join(str(r) for r in t.state.review_reasons) or None
    t.events.append(
        DomainEvent(
            type=EventType.STATUS_CHANGED,
            data={"from": previous.value, "to": status.value, "reason": reason},
        )
    )


def _disqualify(t: _Turn, reason: Reason) -> None:
    t.state.disqualify_reason = reason
    # One last question before closing: keep their details for future roles.
    _close(t, Status.DISQUALIFIED, stage=Stage.KEEP_ON_FILE)
    t.actions.append(Disqualify(reason=reason))


def _finalize(t: _Turn) -> None:
    for reason in final_review_reasons(t.state.candidate, t.config):
        add_review_reason(t.state, reason)
    if t.state.review_reasons:
        _close(t, Status.NEEDS_REVIEW)
        t.actions.append(HandOff(reason="review"))
    else:
        _close(t, Status.QUALIFIED)
        t.actions.append(Qualified())
        _offer_slots(t)


def _offer_slots(t: _Turn) -> None:
    """Qualified: book the interview right here in the chat (no callback, process design)."""
    location_id = t.state.candidate.location_id
    calendar = t.ports.calendar
    slots: list[datetime] = []
    if calendar is not None and location_id:
        after = t.now + timedelta(days=t.config.interview.min_days_ahead)
        slots = calendar.available_slots(location_id, after, t.config.interview.offer_count)
    if not slots or not location_id:
        t.state.stage = Stage.CLOSED
        t.say("qualified_no_slots")
        return
    t.state.stage = Stage.SCHEDULING
    t.state.offered_slots = slots
    t.event(EventType.SLOTS_OFFERED, count=len(slots))
    t.actions.append(OfferSlots(slots=slots, location_id=location_id))


def _ask_current(t: _Turn) -> None:
    """Repeat whatever we were waiting for (after a warning)."""
    if t.state.stage == Stage.CONSENT:
        t.say("consent_reask")
    elif t.state.stage == Stage.RECAP:
        t.actions.append(Recap())
    elif nxt := t.state.awaiting or next_missing_field(t.state.candidate, t.config):
        t.actions.append(AskField(field=nxt))


# --- Finishing ------------------------------------------------------------------------------------


def _finish(t: _Turn, schedule_reminder: bool = True) -> TurnResult:
    if schedule_reminder:
        _schedule_reminder(t)
    t.state.progress = progress(t.state.candidate, t.config)
    if t.state.status in TERMINAL_STATUSES:
        t.state.summary = build_summary(t.state, t.config)
    replies, render_events = render(t.actions, t.state, t.config, t.ports)
    return TurnResult(
        state=t.state,
        actions=t.actions,
        replies=replies,
        events=[*t.events, *render_events],
        redact_inbound=t.redact,
    )


def _schedule_reminder(t: _Turn) -> None:
    """While the screening is open, the next reminder counts from the candidate's last message."""
    hours = t.config.reminders_for(t.state.channel)
    base = t.state.last_inbound_at or t.state.started_at
    if t.state.status != Status.IN_PROGRESS or not hours or base is None:
        return
    t.state.next_reminder_at = base + timedelta(hours=hours[0])
    t.actions.append(ScheduleReminder(at=t.state.next_reminder_at))


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
    previous = _end(state, status)
    state.ended_at = state.ended_at or ports.clock.now()
    state.summary = build_summary(state, config)
    event = DomainEvent(
        type=EventType.OVERRIDE,
        data={"from": previous.value, "to": status.value, "reason": reason.strip(), "actor": actor},
    )
    return state, [event]
