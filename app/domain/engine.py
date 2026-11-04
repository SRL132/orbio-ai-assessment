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
    ConfirmField,
    ConversationState,
    Disqualify,
    DomainEvent,
    EventType,
    ExtractedFields,
    ExtractionResult,
    FieldName,
    FieldStatus,
    GeoLookup,
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
    FIELD_GROUPS,
    ApplyResult,
    add_review_reason,
    apply_confirmed,
    apply_extraction,
    has_words,
    must_confirm,
    next_missing_field,
    progress,
    register_failed_attempt,
)
from app.domain.ports import LLMUnavailable, Ports
from app.domain.render import render
from app.domain.summary import build_summary
from app.domain.validators import (
    final_review_reasons,
    is_known_place,
    nearest_location,
    says_no_language,
)

# The field each knockout comes from: a different answer to it reopens the screening.
_KNOCKOUT_FIELDS = {Reason.NO_LICENSE: FieldName.LICENSE, Reason.OUTSIDE_AREA: FieldName.LOCATION}

# A candidate's own words about when they could do the interview, kept for the recruiter.
_PREFERRED_TIMES_CHARS = 200
_BARE_ANSWER_WORDS = 2  # "no", "no gracias": a refusal with nothing else in it

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
    message: str = ""  # the candidate's words this turn: extracted values must quote them

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
    return _finish(t, schedule_reminder=not t.state.invite_pending)


def on_invite_opened(
    state: ConversationState, config: ClientConfig, ports: Ports
) -> TurnResult | None:
    """The candidate opened their invite link: the conversation really starts now, so its
    duration and its reminders count from here. Opening it again changes nothing."""
    if not state.invite_pending:
        return None
    t = _Turn.begin(state, config, ports)
    t.state.invite_pending = False
    t.state.started_at = t.now
    t.event(EventType.INVITE_OPENED)
    return _finish(t)


def turn(
    state: ConversationState, message: InboundMessage, config: ClientConfig, ports: Ports
) -> TurnResult:
    t = _Turn.begin(state, config, ports)
    t.event(EventType.MESSAGE_RECEIVED, stage=t.state.stage.value)
    if t.state.status == Status.ABANDONED and (
        t.state.went_silent or t.state.dropped_at == Stage.CONSENT.value
    ):
        _resume(t)  # silent, or declined consent and was told to write back if they changed mind
    if t.state.stage == Stage.CLOSED:
        _on_closed(t, message)
        return _finish(t)

    t.message = message.text
    # Nothing a candidate writes is kept before they say yes (see _is_plain_consent_answer).
    t.redact = t.state.stage == Stage.CONSENT
    t.state.last_inbound_at = t.now
    t.state.reminders_sent = 0
    try:
        extraction = ports.extractor.extract(t.state, message.text, config, t.now.date())
    except LLMUnavailable as exc:
        return _hold_for_retry(t, exc)
    except Exception as exc:  # graceful failure: ask again, never count it against the candidate
        t.event(EventType.EXTRACTOR_FAILED, error=type(exc).__name__)
        t.state.llm_failures, t.state.retry_turn_at = 0, None
        t.say("technical_retry")
        return _finish(t)
    t.state.llm_failures, t.state.retry_turn_at = 0, None

    own_name = (t.state.candidate.full_name or "").split()
    if not says_no_language(message.text, config, own_name):  # "madrid tetuan" keeps the language
        t.state.language = extraction.language
    if t.redact and _is_plain_consent_answer(extraction, message.text):
        t.redact = False
    if extraction.sensitive_data:
        t.redact = True
        t.say("sensitive_data")
        t.event(EventType.SENSITIVE_DATA_REDACTED)

    if extraction.intent == "data_request":
        _on_data_request(t)
    elif extraction.intent == "abuse":
        _on_abuse(t)
    elif extraction.intent == "human_request" and t.state.status == Status.DISQUALIFIED:
        _human_after_knockout(t)
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
        t.state.went_silent = True
        t.event(EventType.ABANDONED, drop_off=drop)
        _close(t, Status.ABANDONED)
    return _finish(t, schedule_reminder=False)


def _resume(t: _Turn) -> None:
    """A candidate abandoned for silence (or who declined consent) writes again: reopen at the
    stage they stopped at, so this message is read as their answer and the flow carries on with
    the missing fields."""
    state = t.state
    why = "resumed_after_silence" if state.went_silent else "resumed_after_decline"
    if state.dropped_at == Stage.CONSENT.value:
        state.stage = Stage.CONSENT
    elif state.dropped_at == Stage.RECAP.value:
        state.stage = Stage.RECAP
    else:
        state.stage = Stage.COLLECTING
        state.awaiting = next_missing_field(state.candidate, t.config)
    state.status = Status.IN_PROGRESS
    state.went_silent = False
    state.ended_at = None
    state.summary = None
    t.events.append(
        DomainEvent(
            type=EventType.STATUS_CHANGED,
            data={
                "from": Status.ABANDONED.value,
                "to": Status.IN_PROGRESS.value,
                "reason": f"{why}:{state.dropped_at}",
            },
        )
    )
    state.dropped_at = None


def _hold_for_retry(t: _Turn, exc: LLMUnavailable) -> TurnResult:
    """No LLM answered. Keep the candidate's message and retry it later, telling them once
    that we're on it, so an outage never loses what they wrote. When the retries run out, ask
    them to write it again: reminders still follow, so the conversation is never dropped."""
    attempt = t.state.llm_failures + 1
    delays = t.config.llm_retry_seconds
    gave_up = attempt > len(delays)
    t.event(EventType.EXTRACTOR_FAILED, error=type(exc).__name__, attempt=attempt, gave_up=gave_up)
    if gave_up:
        t.state.llm_failures, t.state.retry_turn_at = 0, None
        t.say("technical_unavailable")
        return _finish(t)
    if attempt == 1:
        t.say("technical_hold")
    t.state.llm_failures = attempt
    t.state.retry_turn_at = t.now + timedelta(seconds=delays[attempt - 1])
    return _finish(t, hold=True)


# --- Stage handlers -------------------------------------------------------------------------------


def _is_plain_consent_answer(extraction: ExtractionResult, text: str) -> bool:
    """What may be stored from a message sent before consent: one that gives it, or a bare "no"."""
    if extraction.yes_no is True:
        return True
    return extraction.yes_no is False and len(text.split()) <= _BARE_ANSWER_WORDS


def _on_closed(t: _Turn, message: InboundMessage) -> None:
    """The screening is over, but a request only a person can act on still has to reach one, so
    the closing message's "a recruiter will get in touch" stays true."""
    status = t.state.status
    try:
        extraction = t.ports.extractor.extract(t.state, message.text, t.config, t.now.date())
    except Exception as exc:  # the closed reply needs no LLM
        t.event(EventType.EXTRACTOR_FAILED, error=type(exc).__name__)
    else:
        t.redact = extraction.sensitive_data
        if extraction.intent == "data_request":
            t.state.language = extraction.language
            _on_data_request(t)
            return
        if extraction.intent == "interview_change" and status != Status.DISQUALIFIED:
            t.state.language = extraction.language
            t.event(EventType.INTERVIEW_CHANGE_REQUESTED)
            _send_to_review(t, Reason.INTERVIEW_CHANGE)
            t.say("interview_change_noted")
            return
        if extraction.intent == "human_request" and status == Status.DISQUALIFIED:
            t.state.language = extraction.language
            _human_after_knockout(t)
            return
        if extraction.intent == "human_request" and status == Status.QUALIFIED:
            t.state.language = extraction.language
            _send_to_review(t, Reason.HUMAN_REQUESTED)
            t.actions.append(HandOff(reason="human_requested"))
            return
    t.say("closed_disqualified" if status == Status.DISQUALIFIED else "closed")


def _on_data_request(t: _Turn) -> None:
    """They want to stop or have their data deleted: a right, not a screening answer, so nothing
    else is asked. A recruiter handles it from the review queue. That holds after a knockout too:
    deleting their data can't win anyone a job, and the knockout reason stays on the record."""
    t.event(EventType.DATA_REQUESTED, status=t.state.status.value)
    t.state.keep_on_file = False
    add_review_reason(t.state, Reason.DATA_REQUEST)
    if t.state.status != Status.NEEDS_REVIEW or t.state.stage != Stage.CLOSED:
        _close(t, Status.NEEDS_REVIEW)
    t.say("data_request_received")


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


def _human_after_knockout(t: _Turn) -> None:
    """A knocked-out candidate asks for a person: the knockout stands (a review queue entry would
    let every rejected candidate skip it), but the request is logged and shown to recruiters."""
    t.state.human_after_knockout = True
    t.event(EventType.HUMAN_REQUESTED, after_knockout=True)
    t.say("handoff_disqualified")
    if t.state.stage == Stage.KEEP_ON_FILE:
        t.say("keep_on_file_reask")


def _send_to_review(t: _Turn, reason: Reason) -> None:
    """A person has to act on this, whatever the outcome was: put it in the review queue. The
    time the outcome was first reached is kept, so analytics still measure the screening."""
    add_review_reason(t.state, reason)
    if t.state.status == Status.NEEDS_REVIEW:
        return
    ended_at = t.state.ended_at
    _close(t, Status.NEEDS_REVIEW)
    t.state.ended_at = ended_at or t.now


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
            t.actions.append(AnswerQuestion(question=extraction.question, faq_id=extraction.faq_id))
        t.say("consent_reask")


def _on_answers(t: _Turn, extraction: ExtractionResult) -> None:
    """Collecting and recap: apply the extracted fields, then ask, recap or finish."""
    if extraction.intent == "question" and extraction.question:
        t.actions.append(AnswerQuestion(question=extraction.question, faq_id=extraction.faq_id))
    fields = extraction.fields
    if t.state.confirming is not None:
        rest = _on_confirmation(t, extraction)
        if rest is None:
            return
        fields = rest
    applied = apply_extraction(
        t.state, fields, t.config, t.now.date(), message=t.message, geo=_locate(t, fields)
    )
    t.events.extend(applied.events)
    if applied.knockout:
        _disqualify(t, applied.knockout)
    elif t.state.stage == Stage.RECAP:
        _on_recap_reply(t, extraction, applied)
    else:
        _ask_next(t, extraction, applied)


def _on_confirmation(t: _Turn, extraction: ExtractionResult) -> ExtractedFields | None:
    """The candidate answers our "did I get that right?" question. Returns the fields still to
    apply from this message, or None when this turn is fully handled."""
    pending = t.state.confirming
    if pending is None:
        return extraction.fields
    group = FIELD_GROUPS[pending.field]
    # Code decides: a "yes" only confirms if the candidate actually wrote something.
    said_yes = extraction.yes_no is True and has_words(t.message)
    corrected = not said_yes and any(getattr(extraction.fields, attr) is not None for attr in group)
    others = [a for a in type(extraction.fields).model_fields if a not in group]
    if not said_yes and not corrected and any(getattr(extraction.fields, a) for a in others):
        # They answered the next question instead ("no tengo carnet" after "Luis Pérez,
        # ¿correcto?"): that "no" isn't about our value. Apply the answer, keep ours pending.
        return extraction.fields
    if said_yes:
        applied = apply_confirmed(t.state)
        t.events.extend(applied.events)
        t.event(EventType.CONFIRMATION_ANSWERED, field=pending.field.value, answer="yes")
        if applied.knockout:
            _disqualify(t, applied.knockout)
            return None
        if t.state.stage == Stage.RECAP:
            t.actions.append(Recap())
            return None
        # Go on with the rest of the message, minus the value just confirmed.
        return extraction.fields.model_copy(update=dict.fromkeys(group))
    if extraction.yes_no is False or corrected:
        t.state.confirming = None
        t.event(EventType.CONFIRMATION_ANSWERED, field=pending.field.value, answer="no")
        if corrected:  # "no, Móstoles": the new value goes through the checks again
            return extraction.fields
        if t.state.stage == Stage.RECAP:
            t.actions.append(Recap())  # keep the answer they had before the correction
            return None
        t.state.awaiting = pending.field
        # Misheard by speech recognition: ask them to type it (still one attempt), so saying it
        # again doesn't loop. Once the field gives up, _ask_next moves on as usual.
        if must_confirm(pending.field, t.state, t.config) and _count_unusable_answer(
            t, extraction, ApplyResult()
        ):
            t.say("retype_hint")
            return None
        _ask_next(t, extraction, ApplyResult())  # counts the attempt and asks again
        return None
    if extraction.intent in _ANSWER_INTENTS:
        pending.asked += 1
    if pending.asked >= t.config.max_attempts:  # no clear yes or no: ask the question afresh
        t.state.confirming = None
        t.state.awaiting = pending.field
        _ask_next(t, extraction, ApplyResult())
        return None
    t.actions.append(ConfirmField(field=pending.field))
    return None


def _locate(t: _Turn, fields: ExtractedFields) -> GeoLookup | None:
    """Look up a place the config doesn't list, so distance decides instead of a knockout.
    Only the place name goes to the geocoder."""
    geocoder = t.ports.geocoder
    if geocoder is None:
        return None
    city = fields.city.value if fields.city else None
    zone = fields.zone.value if fields.zone else None
    place = city or (zone if t.state.candidate.city is None else None)
    if not place or is_known_place(place, t.config):
        return None
    countries = sorted({loc.country for loc in t.config.locations})
    try:
        found = geocoder.locate(place, countries)
    except Exception as exc:  # the lookup failed: a recruiter decides, nobody is rejected
        t.event(EventType.LOCATION_GEOCODED, found=False, error=type(exc).__name__)
        return GeoLookup(place=place, failed=True)
    nearest = nearest_location(found, t.config) if found else None
    km = round(nearest[1], 1) if nearest else None
    t.event(EventType.LOCATION_GEOCODED, found=found is not None, km=km)
    return GeoLookup(place=place, found=found)


def _on_recap_reply(t: _Turn, extraction: ExtractionResult, applied: ApplyResult) -> None:
    if applied.confirm is not None:
        t.actions.append(ConfirmField(field=applied.confirm))
    elif applied.added or applied.changed:
        t.state.recap_rounds = 0
        t.actions.append(Recap())  # they corrected something: confirm the new version
    elif extraction.yes_no is True:
        _finalize(t)
    elif extraction.yes_no is False or extraction.intent in _ANSWER_INTENTS:
        t.state.recap_rounds += 1
        if t.state.recap_rounds >= t.config.max_recap_rounds:  # the recap isn't asked forever
            add_review_reason(t.state, Reason.RECAP_UNCONFIRMED)
            _close(t, Status.NEEDS_REVIEW)
            t.actions.append(HandOff(reason="review"))
        elif extraction.yes_no is False:
            t.say("ask_change")
        else:
            t.actions.append(Recap())
    else:
        t.actions.append(Recap())  # a question, just answered: back to the recap


def _ask_next(t: _Turn, extraction: ExtractionResult, applied: ApplyResult) -> None:
    """After the candidate's answers: offer a person if they're frustrated, then ask the next
    missing field (again, with the reason, if the answer to it didn't count), or recap."""
    reask_reason = _count_unusable_answer(t, extraction, applied)
    if extraction.sentiment == "frustrated" and not t.state.offered_human:
        t.state.offered_human = True
        t.say("offer_human")
    if t.state.confirming is not None:
        t.state.awaiting = t.state.confirming.field
        t.actions.append(ConfirmField(field=t.state.confirming.field))
        return

    awaited = t.state.awaiting
    nxt = next_missing_field(t.state.candidate, t.config)
    t.state.awaiting = nxt
    if nxt is None:
        t.state.stage = Stage.RECAP
        t.actions.append(Recap())
        return
    reason = (reask_reason or applied.rejected.get(nxt)) if nxt == awaited else None
    if reason:
        t.actions.append(ReAsk(field=nxt, reason=reason, offer_options=extraction.confused))
    else:
        t.actions.append(AskField(field=nxt, offer_options=extraction.confused))


def _count_unusable_answer(
    t: _Turn, extraction: ExtractionResult, applied: ApplyResult
) -> InvalidReason | None:
    """The candidate tried to answer the field we asked for, but nothing usable came out.
    Count the attempt: returns why (to re-ask), or None once the field gives up and moves on."""
    awaited = t.state.awaiting
    tried = (
        extraction.intent in _ANSWER_INTENTS and not applied.progressed and applied.confirm is None
    )
    if awaited is None or not tried:
        return None
    if t.state.candidate.meta[awaited].status != FieldStatus.MISSING:
        return None
    if register_failed_attempt(t.state, awaited, t.config):
        t.event(EventType.FIELD_GAVE_UP, field=awaited.value)
        return None
    return applied.rejected.get(awaited, "unclear")


def _on_keep_on_file(t: _Turn, extraction: ExtractionResult) -> None:
    knocked_out = _corrected_knockout(t, extraction)
    if knocked_out is not None:  # "sorry, I meant Madrid": check the new answer instead
        _reopen_after_knockout(t, knocked_out)
        _on_answers(t, extraction)
        return
    keep = extraction.yes_no
    if keep is None and t.state.keep_on_file_reasks < t.config.max_attempts:
        t.state.keep_on_file_reasks += 1
        t.say("keep_on_file_reask")
        return
    # Still no clear answer: nothing is kept without a yes, and the question isn't asked forever.
    t.state.keep_on_file = bool(keep)
    t.state.stage = Stage.CLOSED
    t.event(EventType.KEEP_ON_FILE, value=keep)
    t.say("keep_on_file_yes" if keep else "keep_on_file_no")


def _corrected_knockout(t: _Turn, extraction: ExtractionResult) -> FieldName | None:
    """The field that knocked the candidate out, if this message gives a different answer for it.
    Code still decides: the new answer goes through the same validators."""
    reason = t.state.disqualify_reason
    name = _KNOCKOUT_FIELDS.get(reason) if reason else None
    if name is None:
        return None
    candidate = t.state.candidate
    for attr in FIELD_GROUPS[name]:
        proposed = getattr(extraction.fields, attr)
        if proposed is not None and proposed.value != getattr(candidate, attr):
            return name
    return None


def _reopen_after_knockout(t: _Turn, name: FieldName) -> None:
    state = t.state
    reason = state.disqualify_reason
    for attr in FIELD_GROUPS[name]:  # forget the answer that knocked them out
        setattr(state.candidate, attr, None)
    state.disqualify_reason = None
    state.status = Status.IN_PROGRESS
    state.stage = Stage.COLLECTING
    state.awaiting = name
    state.ended_at = None
    state.summary = None
    t.event(EventType.KNOCKOUT_CORRECTED, field=name.value)
    t.events.append(
        DomainEvent(
            type=EventType.STATUS_CHANGED,
            data={
                "from": Status.DISQUALIFIED.value,
                "to": Status.IN_PROGRESS.value,
                "reason": f"knockout_corrected:{reason}",
            },
        )
    )


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
    if state.no_slot_fits:  # we asked which days and times would suit them: pass it on
        if not t.redact and not (
            extraction.yes_no is False and len(t.message.split()) <= _BARE_ANSWER_WORDS
        ):
            state.preferred_times = t.message.strip()[:_PREFERRED_TIMES_CHARS]
        state.offered_slots = []
        state.stage = Stage.CLOSED
        t.event(EventType.PREFERRED_TIMES_GIVEN, given=state.preferred_times is not None)
        t.say("preferred_times_noted")
        return
    if extraction.yes_no is False:
        state.no_slot_fits = True  # the offer stays open: they can still pick one
        t.event(EventType.NO_SLOT_FITS)
        t.say("no_slot_fits")
        return
    if choice is not None or extraction.intent in _ANSWER_INTENTS:
        t.say("slot_unclear")  # "7", or a time that isn't on the list: say why we ask again
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
    elif t.state.confirming is not None:
        t.actions.append(ConfirmField(field=t.state.confirming.field))
    elif t.state.stage == Stage.RECAP:
        t.actions.append(Recap())
    elif nxt := t.state.awaiting or next_missing_field(t.state.candidate, t.config):
        t.actions.append(AskField(field=nxt))


# --- Finishing ------------------------------------------------------------------------------------


def _finish(t: _Turn, schedule_reminder: bool = True, hold: bool = False) -> TurnResult:
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
        hold_inbound=hold,
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
