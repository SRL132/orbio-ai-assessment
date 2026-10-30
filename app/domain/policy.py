"""Screening policy: how an extraction changes the candidate, and what to ask next.

Pure functions over the domain models. Python owns every decision here; the LLM only proposes
values (see CLAUDE.md, "Code decides, the LLM talks").
"""

from __future__ import annotations

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
    InvalidReason,
    Reason,
    ReviewReason,
)
from app.domain.validators import (
    Outcome,
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


def _evidence(fields: ExtractedFields, name: FieldName) -> tuple[str | None, float | None]:
    quotes: list[str] = []
    confidences: list[float] = []
    for attr in FIELD_GROUPS[name]:
        extracted = getattr(fields, attr)
        if extracted is not None:
            if extracted.quote and extracted.quote not in quotes:
                quotes.append(extracted.quote)
            confidences.append(extracted.confidence)
    quote = " / ".join(quotes) if quotes else None
    return quote, (min(confidences) if confidences else None)


def _validate(name: FieldName, draft: Candidate, config: ClientConfig, today: date) -> Outcome:
    match name:
        case FieldName.FULL_NAME:
            return validate_full_name(draft.full_name)
        case FieldName.LICENSE:
            return validate_license(draft.has_license, draft.vehicle)
        case FieldName.LOCATION:
            return validate_location(draft.city, draft.zone, draft.postal_code, config)
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


def apply_extraction(
    state: ConversationState, fields: ExtractedFields, config: ClientConfig, today: date
) -> ApplyResult:
    """Validate every proposed field and update the candidate in place. Stops at a knockout."""
    result = ApplyResult()
    candidate = state.candidate
    for name in config.field_priority:
        proposals = _proposals(fields, name)
        if not proposals:
            continue
        # Merge the new proposals over what we already know for this field (e.g. a zone that
        # completes a city given earlier), then validate the whole group.
        draft = candidate.model_copy(update=proposals)
        outcome = _validate(name, draft, config, today)
        quote, confidence = _evidence(fields, name)
        meta = candidate.meta[name]
        before = _snapshot(candidate, name)
        was_set = meta.status != FieldStatus.MISSING

        if outcome.kind == "knockout":
            meta.quote, meta.confidence = quote, confidence
            for attr, value in proposals.items():
                setattr(candidate, attr, value)
            result.knockout = outcome.verdict
            result.events.append(
                DomainEvent(
                    type=EventType.KNOCKOUT, data={"field": name.value, "reason": outcome.reason}
                )
            )
            return result

        if outcome.kind == "invalid":
            # Keep only the partial value the validator vouches for (e.g. a first name).
            for attr, value in outcome.updates.items():
                setattr(candidate, attr, value)
            problem = outcome.reason
            result.rejected[name] = (
                "unclear" if problem is None or isinstance(problem, Reason) else problem
            )
            result.events.append(
                DomainEvent(
                    type=EventType.FIELD_REJECTED,
                    data={"field": name.value, "reason": outcome.reason},
                )
            )
            continue

        for attr, value in {**proposals, **outcome.updates}.items():
            setattr(candidate, attr, value)
        meta.quote, meta.confidence = quote, confidence
        meta.note = outcome.note or (outcome.reason if outcome.kind == "review" else None)
        if outcome.kind == "review":
            meta.status = FieldStatus.NEEDS_REVIEW
            add_review_reason(state, outcome.verdict, name)
        else:
            meta.status = FieldStatus.VALID
        result.progressed.add(name)
        if not was_set:
            result.added.add(name)
        if was_set and _snapshot(candidate, name) != before:
            result.changed.add(name)
            result.events.append(
                DomainEvent(type=EventType.FIELD_CHANGED, data={"field": name.value})
            )
        result.events.append(
            DomainEvent(
                type=EventType.FIELD_EXTRACTED,
                data={"field": name.value, "status": meta.status.value, "confidence": confidence},
            )
        )
    return result


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
