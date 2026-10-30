"""Post-conversation summary for recruiters: 3-5 lines, deterministic, built from the record.

Written in English for the dashboard. It is built from validated fields and review reasons only,
so it can never contradict the data it summarises.
"""

from __future__ import annotations

from app.domain.config import ClientConfig
from app.domain.models import ConversationState, FieldName, FieldStatus, Status

_REASONS = {
    "no_license": "no driving license",
    "outside_area": "lives outside the service area",
    "human_requested": "asked to talk to a person",
    "flagged_behaviour": "abusive messages",
    "license_unclear": "license may not cover a motorbike",
    "borderline_zone": "borderline zone",
    "zone_not_listed": "zone not in the service list",
    "postal_code_not_listed": "postal code not in the service list",
    "field_failed_twice": "no usable answer after two tries",
}


def describe_reason(reason: str) -> str:
    """'borderline_zone:location' -> 'borderline zone (location)'."""
    key, _, field = reason.partition(":")
    text = _REASONS.get(key, key.replace("_", " "))
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
    lines = [headline, f"{c.full_name or 'Name unknown'} · {where} · {vehicle}"]

    if c.availability or c.schedule or c.start_date:
        availability = ", ".join(a.value.replace("_", " ") for a in c.availability) or "-"
        schedule = ", ".join(s.value for s in c.schedule) or "-"
        start = c.start_date.strftime("%d/%m/%Y") if c.start_date else "-"
        lines.append(f"Availability: {availability} · Hours: {schedule} · Start: {start}")
    if c.experience_years is not None:
        platforms = f" ({', '.join(c.platforms)})" if c.platforms else ""
        lines.append(f"Experience: {c.experience_years:g} years{platforms}")

    flags: list[str] = []
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
