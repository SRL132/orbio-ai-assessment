"""Post-conversation summary for recruiters: 3-5 lines, deterministic, built from the record.

Written in English for the dashboard. It is built from validated fields and review reasons only,
so it can never contradict the data it summarises.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.domain.config import ClientConfig
from app.domain.models import (
    ConversationState,
    FieldName,
    FieldStatus,
    Reason,
    ReviewReason,
    Status,
)

REASON_TEXT: dict[str, str] = {
    Reason.NO_LICENSE: "no driving license",
    Reason.OUTSIDE_AREA: "lives outside the service area",
    Reason.HUMAN_REQUESTED: "asked to talk to a person",
    Reason.DATA_REQUEST: "asked to stop or to delete their data",
    Reason.FLAGGED_BEHAVIOUR: "abusive messages",
    Reason.LICENSE_UNCLEAR: "license may not cover a motorbike",
    Reason.BORDERLINE_ZONE: "borderline zone",
    Reason.ZONE_NOT_LISTED: "zone not in the service list",
    Reason.POSTAL_CODE_NOT_LISTED: "postal code not in the service list",
    Reason.FIELD_FAILED_TWICE: "no usable answer after two tries",
    Reason.UNGROUNDED_ANSWER: "answer not found in the candidate's own words",
    Reason.LOCATION_UNVERIFIED: "place could not be checked (lookup failed)",
    Reason.INTERVIEW_CHANGE: "asked to change the interview",
    Reason.RECAP_UNCONFIRMED: "did not confirm the recap after several tries",
}


def local_time(moment: datetime, state: ConversationState, config: ClientConfig) -> str:
    """'02/11/2026 12:00 (Madrid time)': recruiters read times in the location's timezone."""
    location = config.location(state.candidate.location_id or "")
    if location is None:
        return f"{moment:%d/%m/%Y %H:%M} UTC"
    return f"{moment.astimezone(ZoneInfo(location.timezone)):%d/%m/%Y %H:%M} ({location.city} time)"


def describe_reason(reason: ReviewReason | Reason | str) -> str:
    """'borderline_zone:location' -> 'borderline zone (location)'. Also takes the projection's
    text form, and an unknown reason (an old one) falls back to its words."""
    if isinstance(reason, ReviewReason):
        key, field = reason.reason.value, reason.field.value if reason.field else ""
    else:
        key, _, field = str(reason).partition(":")
    text = REASON_TEXT.get(key, key.replace("_", " "))
    return f"{text} ({field.replace('_', ' ')})" if field else text


def build_summary(state: ConversationState, config: ClientConfig) -> str:
    c = state.candidate
    match state.status:
        case Status.QUALIFIED:
            headline = "Qualified: meets every hard requirement."
        case Status.DISQUALIFIED:
            headline = f"Disqualified: {describe_reason(state.disqualify_reason or 'unknown')}."
        case Status.NEEDS_REVIEW:
            reasons = "; ".join(describe_reason(r) for r in state.review_reasons)
            headline = f"Needs review: {reasons or 'see transcript'}."
        case Status.ABANDONED:
            where = state.awaiting.value.replace("_", " ") if state.awaiting else state.stage.value
            headline = f"Abandoned at: {where}."
        case _:
            headline = "In progress."

    where = ", ".join(p for p in (c.zone, c.city) if p) or "location unknown"
    vehicle = f"{c.vehicle.value} license" if c.vehicle else "license unknown"
    if c.vehicle and c.license_type:
        held = f", {_years(c.license_years)}" if c.license_years is not None else ""
        vehicle += f" ({c.license_type}{held})"
    lines = [headline, f"{c.full_name or 'Name unknown'} · {where} · {vehicle}"]

    if c.availability or c.schedule or c.start_date:
        availability = ", ".join(a.value.replace("_", " ") for a in c.availability) or "-"
        schedule = ", ".join(s.value for s in c.schedule) or "-"
        start = c.start_date.strftime("%d/%m/%Y") if c.start_date else "-"
        lines.append(f"Availability: {availability} · Hours: {schedule} · Start: {start}")
    if c.experience_years is not None:
        platforms = f" ({', '.join(c.platforms)})" if c.platforms else ""
        lines.append(f"Experience: {_years(c.experience_years)}{platforms}")

    if state.interview_at is not None:
        lines.append(f"Interview booked: {local_time(state.interview_at, state, config)}")

    flags: list[str] = []
    if state.no_slot_fits:
        prefers = f' (prefers: "{state.preferred_times}")' if state.preferred_times else ""
        flags.append(f"no offered interview slot fits: contact to schedule{prefers}")
    if state.human_after_knockout:
        flags.append("asked to talk to a person after the knockout")
    if c.meta[FieldName.FULL_NAME].note == "surname_missing":
        flags.append("first name only")
    if c.meta[FieldName.START_DATE].note == "start_date_far":
        flags.append(f"start date more than {config.far_start_days} days away")
    low = [
        n.value
        for n, m in c.meta.items()
        if m.status == FieldStatus.VALID and (m.confidence or 1) < 0.6
    ]
    if low:
        flags.append("low-confidence answers: " + ", ".join(low))
    if state.keep_on_file is not None:
        flags.append(f"keep on file: {'yes' if state.keep_on_file else 'no'}")
    if flags:
        lines.append("Flags: " + "; ".join(flags))
    return "\n".join(lines)


def _years(value: float) -> str:
    return f"{value:g} year" if value == 1 else f"{value:g} years"
