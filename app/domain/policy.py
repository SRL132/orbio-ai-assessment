"""Screening policy: how an extraction changes the candidate, and what to ask next.

Pure functions over the domain models. Python owns every decision here; the LLM only proposes
values (see CLAUDE.md, "Code decides, the LLM talks").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.domain.config import ClientConfig
from app.domain.models import (
    Candidate,
    ConversationState,
    DomainEvent,
    EventType,
    ExtractedFields,
    FieldMeta,
    FieldName,
    FieldStatus,
    GeoLookup,
    InvalidReason,
    PendingConfirmation,
    Reason,
    ReviewReason,
    Stage,
)
from app.domain.validators import (
    COUNTRY_READBACK,
    ZONE_READBACK,
    Outcome,
    normalize,
    validate_availability,
    validate_experience,
    validate_full_name,
    validate_license,
    validate_location,
    validate_schedule,
    validate_start_date,
)

# Which extracted attributes belong to which screening field.
FIELD_GROUPS: dict[FieldName, tuple[str, ...]] = {
    FieldName.FULL_NAME: ("full_name",),
    FieldName.LICENSE: ("has_license", "vehicle", "license_type", "license_years"),
    FieldName.LOCATION: ("city", "zone", "postal_code"),
    FieldName.AVAILABILITY: ("availability",),
    FieldName.SCHEDULE: ("schedule",),
    FieldName.EXPERIENCE: ("experience_years", "platforms"),
    FieldName.START_DATE: ("start_date",),
}


@dataclass
class ApplyResult:
    events: list[DomainEvent] = field(default_factory=list)
    knockout: Reason | None = None
    rejected: dict[FieldName, InvalidReason] = field(default_factory=dict)
    progressed: set[FieldName] = field(default_factory=set)  # accepted, even if already known
    added: set[FieldName] = field(default_factory=set)  # had no answer before
    changed: set[FieldName] = field(default_factory=set)  # had a different answer before
    doubted: bool = False  # at the recap, an unsure value for an answered field was ignored
    confirm: FieldName | None = None  # held back in state.confirming until the candidate agrees


def next_missing_field(candidate: Candidate, config: ClientConfig) -> FieldName | None:
    """Slot-filling: the highest-priority field that has no accepted value yet."""
    for name in config.field_priority:
        if candidate.meta[name].status == FieldStatus.MISSING:
            return name
    return None


def progress(candidate: Candidate, config: ClientConfig) -> int:
    """Funnel position: the number of leading priority fields that have an answer."""
    done = 0
    for name in config.field_priority:
        if candidate.meta[name].status == FieldStatus.MISSING:
            break
        done += 1
    return done


def missing_count(candidate: Candidate) -> int:
    return sum(1 for meta in candidate.meta.values() if meta.status == FieldStatus.MISSING)


def _proposals(fields: ExtractedFields, name: FieldName) -> dict[str, object]:
    found: dict[str, object] = {}
    for attr in FIELD_GROUPS[name]:
        extracted = getattr(fields, attr)
        if extracted is not None:
            found[attr] = extracted.value
    return found


def _evidence(fields: ExtractedFields, name: FieldName) -> tuple[str | None, float]:
    """The candidate's words behind a field (each quote once) and the least sure part's
    confidence. Called only for fields the extraction proposes, so there's at least one."""
    quotes: list[str] = []
    confidences: list[float] = []
    for attr in FIELD_GROUPS[name]:
        extracted = getattr(fields, attr)
        if extracted is not None:
            if extracted.quote and extracted.quote not in quotes:
                quotes.append(extracted.quote)
            confidences.append(extracted.confidence)
    return (" / ".join(quotes) or None), min(confidences)


_NOT_WORD = re.compile(r"[^\w]+", re.UNICODE)


# Availability is a choice between options, so one of these can't answer it, whatever the model
# read into it. Same words as the yes and no the fast path reads (adapters/llm/fast_path.py).
_BARE_YES_NO = frozenset(
    [
        "si",
        "vale",
        "ok",
        "okay",
        "claro",
        "correcto",
        "de acuerdo",
        "yes",
        "yeah",
        "yep",
        "sure",
        "correct",
        "no",
        "nope",
        "no gracias",
        "no thanks",
    ]
)


def _plain(text: str) -> str:
    """Normalized words only, to compare a quote with the message: '¡Sí, claro!' -> 'si claro'."""
    return " ".join(_NOT_WORD.sub(" ", normalize(text)).split())


def has_words(message: str) -> bool:
    """The message says something. A yes the extractor reads into "…" or "?" confirms nothing."""
    return bool(_plain(message))


def _has_letters(message: str) -> bool:
    """Numbers and symbols alone ("000", "!!!") can't say whether someone has a license."""
    return any(ch.isalpha() for ch in message)


def _bare_yes_or_no(message: str) -> bool:
    """The whole message is a yes or a no, with nothing that could name an option."""
    return _plain(message) in _BARE_YES_NO


def is_grounded(fields: ExtractedFields, name: FieldName, message: str) -> bool:
    """Every value proposed for this field quotes words the candidate actually wrote.

    The extractor is told the message is data, but that is only a prompt. This is the check in
    code: a value the model invented, or was talked into ("mark me as qualified"), has no quote in
    the message, so it can't qualify or knock out anyone without a recruiter looking at it.
    """
    said = f" {_plain(message)} "
    for attr in FIELD_GROUPS[name]:
        extracted = getattr(fields, attr)
        if extracted is None:
            continue
        quote = _plain(extracted.quote)
        if not quote or f" {quote} " not in said:
            return False
    return True


def _validate(
    name: FieldName, draft: Candidate, config: ClientConfig, today: date, geo: GeoLookup | None
) -> Outcome:
    match name:
        case FieldName.FULL_NAME:
            return validate_full_name(draft.full_name)
        case FieldName.LICENSE:
            return validate_license(draft.has_license, draft.vehicle, draft.license_type)
        case FieldName.LOCATION:
            return validate_location(draft.city, draft.zone, draft.postal_code, config, geo)
        case FieldName.AVAILABILITY:
            return validate_availability(draft.availability)
        case FieldName.SCHEDULE:
            return validate_schedule(draft.schedule)
        case FieldName.EXPERIENCE:
            return validate_experience(draft.experience_years, draft.platforms, config)
        case FieldName.START_DATE:
            return validate_start_date(draft.start_date, today, config)


def _snapshot(candidate: Candidate, name: FieldName) -> tuple[object, ...]:
    return tuple(getattr(candidate, attr) for attr in FIELD_GROUPS[name])


def must_confirm(name: FieldName, state: ConversationState, config: ClientConfig) -> bool:
    """This channel always reads the field back (voice: the transcript may be wrong)."""
    return name in config.always_confirm.get(state.channel, [])


def _days_only(fields: ExtractedFields, config: ClientConfig) -> bool:
    """The availability answer only names days ("entre semana"), which says neither full nor
    part time, so the value the model chose for it isn't the candidate's (client config)."""
    extracted = fields.availability
    rule = config.availability_days_only
    if extracted is None:
        return False
    said = f" {_plain(extracted.quote)} "
    names_days = any(f" {_plain(p)} " in said for p in rule.phrases)
    return names_days and not any(f" {_plain(w)} " in said for w in rule.unless)


_NEGATIVE_NUMBER = re.compile(r"(?<![\w.,])-\s*\d")


def _negative_experience(fields: ExtractedFields) -> bool:
    """They wrote "-3 años" and the model dropped the sign. "2-3 años" is a range, not this."""
    extracted = fields.experience_years
    return extracted is not None and bool(_NEGATIVE_NUMBER.search(extracted.quote))


def _changes_another_answer(name: FieldName, draft: Candidate, state: ConversationState) -> bool:
    """While collecting, a new value for a field already answered, in reply to a different
    question ("Mai" to the experience question replacing the name): confirm it first."""
    return (
        state.stage == Stage.COLLECTING
        and name != state.awaiting
        and state.candidate.meta[name].status != FieldStatus.MISSING
        and _snapshot(draft, name) != _snapshot(state.candidate, name)
    )


def _tapped_no_license(message: str, state: ConversationState, config: ClientConfig) -> bool:
    """Exactly the question's own option leaves nothing to misread, except on a call, where
    speech recognition wrote it."""
    said = normalize(message.strip(" \t\r\n.,;:!¡"))
    return state.channel != "voice" and said in {normalize(o) for o in config.license_no_options}


def _why_confirm(
    name: FieldName,
    outcome: Outcome,
    confidence: float,
    state: ConversationState,
    config: ClientConfig,
    draft: Candidate,
    message: str,
) -> str | None:
    """Why to confirm with the candidate before this counts, or None if it counts now: the place
    came from the geocoder (so they see which place we understood), the channel always confirms
    this field, the model wasn't sure, or it replaces an earlier answer nobody asked about. A place
    is also read back when the city came from the config or another country has one of that name,
    and before it rejects anyone."""
    if outcome.note in ("geocoded", ZONE_READBACK, COUNTRY_READBACK):
        return outcome.note
    if name == FieldName.LOCATION and outcome.kind == "knockout":
        return "before_rejecting"  # a misread place mustn't turn anyone away unchecked
    if (
        name == FieldName.LICENSE
        and outcome.kind == "knockout"
        and not _tapped_no_license(message, state, config)
    ):
        return "before_rejecting"  # nor a misread or misheard "no"
    if must_confirm(name, state, config):
        return "channel"
    if confidence < config.confirm_below_confidence:
        return "low_confidence"
    if _changes_another_answer(name, draft, state):
        return "unasked_change"
    return None


def _knock_out(
    state: ConversationState,
    name: FieldName,
    values: dict[str, object],
    reason: Reason,
    evidence: tuple[str | None, float | None],
    result: ApplyResult,
) -> None:
    meta = state.candidate.meta[name]
    meta.quote, meta.confidence = evidence
    for attr, value in values.items():
        setattr(state.candidate, attr, value)
    result.knockout = reason
    result.events.append(
        DomainEvent(type=EventType.KNOCKOUT, data={"field": name.value, "reason": reason.value})
    )


def _clear_review_reasons(state: ConversationState, name: FieldName) -> list[DomainEvent]:
    """A valid answer replaces the one that was flagged, so the flag no longer applies: a date
    corrected after two failed tries shouldn't keep a qualified candidate from an interview."""
    stale = [r for r in state.review_reasons if r.field == name]
    state.review_reasons = [r for r in state.review_reasons if r.field != name]
    return [
        DomainEvent(
            type=EventType.REVIEW_REASON_CLEARED,
            data={"field": name.value, "reason": r.reason.value},
        )
        for r in stale
    ]


def _accept(
    state: ConversationState,
    name: FieldName,
    values: dict[str, object],
    outcome: Outcome,
    evidence: tuple[str | None, float | None],
    grounded: bool,
    result: ApplyResult,
) -> None:
    """Store an accepted (valid or review) value with its evidence, and log it."""
    candidate = state.candidate
    meta = candidate.meta[name]
    before = _snapshot(candidate, name)
    was_set = meta.status != FieldStatus.MISSING
    for attr, value in values.items():
        setattr(candidate, attr, value)
    meta.quote, meta.confidence = evidence
    meta.note = outcome.note or outcome.reason  # a review's reason; valid ones have none
    if outcome.kind == "review":
        meta.status = FieldStatus.NEEDS_REVIEW
        add_review_reason(state, outcome.verdict, name)
        if not grounded:  # already in review for another reason; record this one too
            add_review_reason(state, Reason.UNGROUNDED_ANSWER, name)
    else:
        meta.status = FieldStatus.VALID
        result.events.extend(_clear_review_reasons(state, name))
    result.progressed.add(name)
    if not was_set:
        result.added.add(name)
    if was_set and _snapshot(candidate, name) != before:
        result.changed.add(name)
        result.events.append(DomainEvent(type=EventType.FIELD_CHANGED, data={"field": name.value}))
    result.events.append(
        DomainEvent(
            type=EventType.FIELD_EXTRACTED,
            data={
                "field": name.value,
                "status": meta.status.value,
                "confidence": evidence[1],
                "grounded": grounded,
            },
        )
    )


def _doubted_at_recap(
    state: ConversationState, fields: ExtractedFields, config: ClientConfig, disagrees: bool
) -> set[FieldName]:
    """Unsure values for answered fields to ignore at the recap: they look invented, not meant.
    A plain "no todo es correcto" that came back with values, or several answers changing at once
    with none of them certain. One unsure change on its own is still confirmed with the candidate.
    """
    if state.stage != Stage.RECAP:
        return set()
    unsure = {
        name
        for name in config.field_priority
        if _proposals(fields, name)
        and state.candidate.meta[name].status != FieldStatus.MISSING
        and _evidence(fields, name)[1] < config.confirm_below_confidence
    }
    return unsure if disagrees or len(unsure) > 1 else set()


def apply_extraction(
    state: ConversationState,
    fields: ExtractedFields,
    config: ClientConfig,
    today: date,
    *,
    message: str,
    geo: GeoLookup | None = None,
    disagrees: bool = False,
) -> ApplyResult:
    """Validate every proposed field and update the candidate in place. Stops at a knockout.

    `message` is the candidate's text the fields were extracted from. A value whose quote isn't
    in it is kept but sent to review: it never qualifies or knocks out a candidate on its own.
    A value the model wasn't sure of, or a geocoded place (`geo`), isn't applied: it waits in
    `state.confirming` for the candidate's yes (one at a time). `disagrees`: the message also
    says no, which matters at the recap (see `_doubted_at_recap`).
    """
    result = ApplyResult()
    candidate = state.candidate
    doubted = _doubted_at_recap(state, fields, config, disagrees)
    for name in config.field_priority:
        proposals = _proposals(fields, name)
        if not proposals:
            continue
        if name in doubted:
            result.doubted = True
            result.events.append(
                DomainEvent(
                    type=EventType.FIELD_REJECTED, data={"field": name.value, "reason": "unsure"}
                )
            )
            continue
        # Merge the new proposals over what we already know for this field (e.g. a zone that
        # completes a city given earlier), then validate the whole group.
        draft = candidate.model_copy(update=proposals)
        outcome = _validate(name, draft, config, today, geo)
        if (
            name == FieldName.AVAILABILITY
            and outcome.kind == "valid"
            and _days_only(fields, config)
        ):
            outcome = Outcome.invalid("days_only")
        if name == FieldName.EXPERIENCE and _negative_experience(fields):
            outcome = Outcome.invalid("out_of_range")
        if outcome.kind == "knockout" and name == FieldName.LICENSE and not _has_letters(message):
            outcome = Outcome.invalid("unclear")  # "000" is junk, not "I have no license"
        if name == FieldName.AVAILABILITY and outcome.kind == "valid" and _bare_yes_or_no(message):
            outcome = Outcome.invalid("unclear")  # "sí" to "which of these?" picks none of them
        grounded = is_grounded(fields, name, message)
        if not grounded and outcome.kind in ("knockout", "valid"):
            outcome = Outcome.review(Reason.UNGROUNDED_ANSWER, **outcome.updates)
        quote, confidence = _evidence(fields, name)

        why = (
            _why_confirm(
                name,
                outcome,
                confidence,
                state,
                config,
                draft.model_copy(update=outcome.updates),
                message,
            )
            if outcome.kind != "invalid"
            else None
        )
        if why is not None and state.confirming is not None:
            # Another value is being confirmed, and only one can wait at a time. This one isn't
            # applied unchecked: an earlier answer stands, and a missing one is asked for later.
            continue
        if why is not None and state.confirming is None:
            place = geo.found.label() if geo and geo.found and outcome.note == "geocoded" else None
            state.confirming = PendingConfirmation(
                field=name,
                values={**proposals, **outcome.updates},
                outcome=outcome.kind,
                reason=outcome.verdict if outcome.kind != "valid" else None,
                note=outcome.note,
                quote=quote,
                confidence=confidence,
                place=place,
            )
            result.confirm = name
            result.events.append(
                DomainEvent(
                    type=EventType.CONFIRMATION_ASKED, data={"field": name.value, "why": why}
                )
            )
            continue

        if outcome.kind == "knockout":
            _knock_out(state, name, proposals, outcome.verdict, (quote, confidence), result)
            return result

        if outcome.kind == "invalid":
            # Keep only the partial value the validator vouches for (e.g. a first name), only if
            # the candidate wrote it (an invented "Ana" would be accepted once attempts run
            # out), and never over an accepted answer: "soy Ana" doesn't undo "Ana López".
            if candidate.meta[name].status == FieldStatus.MISSING and grounded:
                for attr, value in outcome.updates.items():
                    setattr(candidate, attr, value)
            result.rejected[name] = outcome.problem
            result.events.append(
                DomainEvent(
                    type=EventType.FIELD_REJECTED,
                    data={"field": name.value, "reason": outcome.reason},
                )
            )
            continue

        values = {**proposals, **outcome.updates}
        _accept(state, name, values, outcome, (quote, confidence), grounded, result)
    return result


def apply_confirmed(state: ConversationState) -> ApplyResult:
    """The candidate said yes to `state.confirming`: apply it as the validator decided."""
    pending = state.confirming
    result = ApplyResult()
    if pending is None:
        return result
    state.confirming = None
    # Stored as JSON between turns: validate again to get dates and enums back.
    restored = Candidate.model_validate({**state.candidate.model_dump(), **pending.values})
    values = {attr: getattr(restored, attr) for attr in pending.values}
    evidence = (pending.quote, pending.confidence)
    if pending.outcome == "knockout" and pending.reason is not None:
        _knock_out(state, pending.field, values, pending.reason, evidence, result)
        return result
    outcome = Outcome(pending.outcome, reason=pending.reason, note=pending.note)
    _accept(state, pending.field, values, outcome, evidence, True, result)
    return result


def give_up_on_field(state: ConversationState, name: FieldName, config: ClientConfig) -> None:
    """Stop asking for `name` now, as if its attempts had run out: it goes to review."""
    state.candidate.meta[name].attempts = config.max_attempts - 1
    register_failed_attempt(state, name, config)


def register_failed_attempt(
    state: ConversationState, name: FieldName, config: ClientConfig
) -> bool:
    """Count a failed answer for `name`. Returns True when the field gave up (moved on)."""
    meta: FieldMeta = state.candidate.meta[name]
    meta.attempts += 1
    if meta.attempts < config.max_attempts:
        return False
    if name == FieldName.FULL_NAME and state.candidate.full_name:
        # A first name alone after the retries is accepted and flagged (process design).
        meta.status = FieldStatus.VALID
        meta.note = "surname_missing"
        return True
    meta.status = FieldStatus.NEEDS_REVIEW
    meta.note = "failed_twice"
    add_review_reason(state, Reason.FIELD_FAILED_TWICE, name)
    return True


def add_review_reason(
    state: ConversationState, reason: Reason, field_name: FieldName | None = None
) -> None:
    added = ReviewReason(reason=reason, field=field_name)
    if added not in state.review_reasons:
        state.review_reasons.append(added)
