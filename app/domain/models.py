"""Domain models: the candidate record, conversation state, LLM extraction and engine actions.

Pydantic models are the single schema: the same types are validated here, persisted by the
storage adapter, and handed to the LLM as the structured-output contract.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FieldName(StrEnum):
    FULL_NAME = "full_name"
    LICENSE = "license"
    LOCATION = "location"
    AVAILABILITY = "availability"
    SCHEDULE = "schedule"
    EXPERIENCE = "experience"
    START_DATE = "start_date"


class Status(StrEnum):
    IN_PROGRESS = "in_progress"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    NEEDS_REVIEW = "needs_review"
    ABANDONED = "abandoned"


TERMINAL_STATUSES = frozenset(
    {Status.QUALIFIED, Status.DISQUALIFIED, Status.NEEDS_REVIEW, Status.ABANDONED}
)


class Stage(StrEnum):
    CONSENT = "consent"
    COLLECTING = "collecting"
    RECAP = "recap"
    KEEP_ON_FILE = "keep_on_file"
    SCHEDULING = "scheduling"
    CLOSED = "closed"


class Vehicle(StrEnum):
    MOTO = "moto"
    CAR = "car"
    BOTH = "both"


class Availability(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    WEEKENDS = "weekends"


class Schedule(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    FLEXIBLE = "flexible"


class FieldStatus(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    NEEDS_REVIEW = "needs_review"


class Reason(StrEnum):
    """Why a candidate was knocked out or sent to review. Recruiters see each one described."""

    NO_LICENSE = "no_license"
    OUTSIDE_AREA = "outside_area"
    HUMAN_REQUESTED = "human_requested"
    FLAGGED_BEHAVIOUR = "flagged_behaviour"
    LICENSE_UNCLEAR = "license_unclear"
    BORDERLINE_ZONE = "borderline_zone"
    ZONE_NOT_LISTED = "zone_not_listed"
    POSTAL_CODE_NOT_LISTED = "postal_code_not_listed"
    FIELD_FAILED_TWICE = "field_failed_twice"
    UNGROUNDED_ANSWER = "ungrounded_answer"
    LOCATION_UNVERIFIED = "location_unverified"


# Knockouts end the screening; each needs a `disqualified_<reason>` template.
KNOCKOUT_REASONS = frozenset({Reason.NO_LICENSE, Reason.OUTSIDE_AREA})

# Why an answer was not accepted: the responder uses it to re-ask helpfully.
InvalidReason = Literal[
    "missing",
    "not_a_name",
    "surname_missing",
    "vehicle_missing",
    "zone_missing",
    "city_missing",
    "out_of_range",
    "in_past",
    "days_only",
    "unclear",
]


class ReviewReason(BaseModel):
    """Why a recruiter must look at a candidate, and which field it's about (if any)."""

    model_config = ConfigDict(frozen=True)

    reason: Reason
    field: FieldName | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, value: object) -> object:
        """Read the text form too ('borderline_zone:location'), as states saved before this
        model, the dashboard projection and eval scenarios store it."""
        if isinstance(value, str):
            reason, _, field = value.partition(":")
            return {"reason": reason, "field": field or None}
        return value

    def __str__(self) -> str:
        return f"{self.reason.value}:{self.field.value}" if self.field else self.reason.value


Language = Literal["es", "en"]
Channel = Literal["chat", "voice", "whatsapp"]


# --- Candidate record ---------------------------------------------------------------------------


class FieldMeta(BaseModel):
    """Traceability for one field: where the value came from and how sure we are."""

    status: FieldStatus = FieldStatus.MISSING
    quote: str | None = None
    confidence: float | None = None
    attempts: int = 0
    note: str | None = None


class GeoPlace(BaseModel):
    """A place found by the geocoder: what it's called and where it is."""

    name: str
    region: str | None = None
    country_code: str
    lat: float
    lon: float

    def label(self) -> str:
        """How the candidate is asked to confirm it: 'Móstoles (Comunidad de Madrid)'."""
        return f"{self.name} ({self.region})" if self.region else self.name


class GeoLookup(BaseModel):
    """The geocoder's answer for a place the config doesn't list, handed to the validator.
    `found` None with `failed` False: no such place; `failed`: the lookup itself didn't work."""

    place: str
    found: GeoPlace | None = None
    failed: bool = False


class PendingConfirmation(BaseModel):
    """A value held back until the candidate confirms it (the model wasn't sure, or the place
    came from the geocoder). Nothing is applied to the candidate until they say yes."""

    field: FieldName
    values: dict[str, Any]
    outcome: Literal["valid", "review", "knockout"]
    reason: Reason | None = None
    note: str | None = None
    quote: str | None = None
    confidence: float | None = None
    place: str | None = Field(default=None, description="Geocoded place label, if any.")
    asked: int = 0


class Candidate(BaseModel):
    full_name: str | None = None
    has_license: bool | None = None
    vehicle: Vehicle | None = None
    license_type: str | None = None
    license_years: float | None = None
    city: str | None = None
    zone: str | None = None
    postal_code: str | None = None
    location_id: str | None = None
    availability: list[Availability] = Field(default_factory=list)
    schedule: list[Schedule] = Field(default_factory=list)
    experience_years: float | None = None
    platforms: list[str] = Field(default_factory=list)
    start_date: date | None = None
    meta: dict[FieldName, FieldMeta] = Field(
        default_factory=lambda: {name: FieldMeta() for name in FieldName}
    )


class ConversationState(BaseModel):
    conversation_id: UUID = Field(default_factory=uuid4)
    client_id: str
    channel: Channel = "chat"
    candidate: Candidate = Field(default_factory=Candidate)
    status: Status = Status.IN_PROGRESS
    stage: Stage = Stage.CONSENT
    awaiting: FieldName | None = None
    confirming: PendingConfirmation | None = None
    language: Language = "es"
    review_reasons: list[ReviewReason] = Field(default_factory=list)
    disqualify_reason: Reason | None = None
    keep_on_file: bool | None = None
    abuse_strikes: int = 0
    offered_human: bool = False
    reminders_sent: int = 0
    next_reminder_at: datetime | None = None
    applied_at: datetime | None = Field(
        default=None,
        description="When the candidate applied (from the ATS or job board). None: the chat is "
        "where they applied, so time to screen counts from started_at.",
    )
    started_at: datetime | None = None
    last_inbound_at: datetime | None = None
    ended_at: datetime | None = None
    summary: str | None = None
    offered_slots: list[datetime] = Field(default_factory=list)
    interview_at: datetime | None = None
    no_slot_fits: bool = False
    preferred_times: str | None = None  # their words, after no offered slot fit
    human_after_knockout: bool = False  # asked for a person after a knockout (status stands)
    dropped_at: str | None = Field(
        default=None, description="Where an abandoned candidate stopped (for drop-off analytics)."
    )
    went_silent: bool = Field(
        default=False,
        description="Abandoned for not replying (not for declining consent): a reply resumes it.",
    )
    progress: int = Field(
        default=0,
        description="How many fields, in the client's priority order, are answered (funnel).",
    )
    llm_failures: int = Field(
        default=0,
        description="Retries so far of a message no LLM could answer (it's held, not lost).",
    )
    retry_turn_at: datetime | None = Field(
        default=None, description="When to retry the held message, while llm_failures > 0."
    )
    turns: int = Field(default=0, description="Candidate turns processed (funnel metrics).")
    llm_cost_usd: float = Field(
        default=0.0, description="LLM spend on this conversation, where the price is known."
    )


class InboundMessage(BaseModel):
    conversation_id: UUID
    text: str
    received_at: datetime
    message_id: str | None = None
    channel: str = "web"


# --- LLM extraction (the structured-output contract) ---------------------------------------------


class Extracted[T](BaseModel):
    value: T
    quote: str = Field(description="The candidate's own words this value came from.")
    confidence: float = Field(ge=0.0, le=1.0)


Intent = Literal["answer", "question", "human_request", "off_topic", "abuse", "other"]
Sentiment = Literal["positive", "neutral", "frustrated"]


class ExtractedFields(BaseModel):
    full_name: Extracted[str] | None = None
    has_license: Extracted[bool] | None = None
    vehicle: Extracted[Vehicle] | None = None
    license_type: Extracted[str] | None = None
    license_years: Extracted[float] | None = None
    city: Extracted[str] | None = None
    zone: Extracted[str] | None = None
    postal_code: Extracted[str] | None = None
    availability: Extracted[list[Availability]] | None = None
    schedule: Extracted[list[Schedule]] | None = None
    experience_years: Extracted[float] | None = None
    platforms: Extracted[list[str]] | None = None
    start_date: Extracted[date] | None = None


class ExtractionResult(BaseModel):
    fields: ExtractedFields = Field(default_factory=ExtractedFields)
    language: Language = "es"
    intent: Intent = "answer"
    sentiment: Sentiment = "neutral"
    confused: bool = False
    yes_no: bool | None = Field(
        default=None, description="Answer to the last yes/no question, if the message gives one."
    )
    sensitive_data: bool = Field(
        default=False, description="The message contains an ID number, health data or similar."
    )
    question: str | None = Field(default=None, description="A question the candidate asked.")
    slot_choice: int | None = Field(
        default=None, description="Number of the interview slot the candidate picked (1-based)."
    )


# --- Engine output ------------------------------------------------------------------------------


class AskField(BaseModel):
    kind: Literal["ask_field"] = "ask_field"
    field: FieldName
    offer_options: bool = False


class ReAsk(BaseModel):
    kind: Literal["re_ask"] = "re_ask"
    field: FieldName
    reason: InvalidReason
    offer_options: bool = False


class AnswerQuestion(BaseModel):
    kind: Literal["answer_question"] = "answer_question"
    question: str


class Disqualify(BaseModel):
    kind: Literal["disqualify"] = "disqualify"
    reason: Reason


class HandOff(BaseModel):
    kind: Literal["hand_off"] = "hand_off"
    reason: Literal["human_requested", "review"]


class Recap(BaseModel):
    kind: Literal["recap"] = "recap"


class Qualified(BaseModel):
    kind: Literal["qualified"] = "qualified"


class ConfirmField(BaseModel):
    """A yes/no check of the value in `state.confirming` (fixed template, no LLM)."""

    kind: Literal["confirm_field"] = "confirm_field"
    field: FieldName


# Fixed messages the engine can say. Every key must exist in each language's templates; the
# config loader checks that at startup (`render.template_problems`).
SayTemplate = Literal[
    "greeting_consent",
    "closed",
    "closed_disqualified",
    "technical_retry",
    "technical_hold",
    "technical_unavailable",
    "sensitive_data",
    "abuse_warning",
    "abuse_end",
    "consent_reask",
    "consent_declined",
    "keep_on_file_reask",
    "keep_on_file_yes",
    "keep_on_file_no",
    "ask_change",
    "offer_human",
    "qualified_no_slots",
    "slot_taken",
    "no_slot_fits",
    "retype_hint",
    "slot_unclear",
    "handoff_disqualified",
    "preferred_times_noted",
]


class Say(BaseModel):
    """A fixed template message (consent, warnings, closings)."""

    kind: Literal["say"] = "say"
    template: SayTemplate


class Remind(BaseModel):
    """The n-th reminder to a silent candidate (template `reminder_<n>`)."""

    kind: Literal["remind"] = "remind"
    number: int


class OfferSlots(BaseModel):
    kind: Literal["offer_slots"] = "offer_slots"
    slots: list[datetime]
    location_id: str


class InterviewBooked(BaseModel):
    kind: Literal["interview_booked"] = "interview_booked"
    at: datetime
    location_id: str


class ScheduleReminder(BaseModel):
    kind: Literal["schedule_reminder"] = "schedule_reminder"
    at: datetime


Action = Annotated[
    AskField
    | ReAsk
    | AnswerQuestion
    | Disqualify
    | HandOff
    | Recap
    | Qualified
    | ConfirmField
    | Say
    | Remind
    | OfferSlots
    | InterviewBooked
    | ScheduleReminder,
    Field(discriminator="kind"),
]


class StoredMessage(BaseModel):
    """A chat message as persisted: inbound from the candidate or outbound from the agent."""

    id: UUID
    conversation_id: UUID
    seq: int
    direction: Literal["in", "out"]
    text: str
    created_at: datetime


class Outbound(BaseModel):
    """An agent message waiting to be delivered on an external channel (the outbox)."""

    message_id: UUID
    conversation_id: UUID
    external_id: str
    text: str
    attempts: int


EXTERNAL_CHANNELS = frozenset({"whatsapp"})


class EventType(StrEnum):
    """Everything the event log can record. Analytics and the dashboard read these."""

    CONVERSATION_STARTED = "conversation_started"
    MESSAGE_RECEIVED = "message_received"
    CONSENT_GIVEN = "consent_given"
    CONSENT_DECLINED = "consent_declined"
    FIELD_EXTRACTED = "field_extracted"
    FIELD_REJECTED = "field_rejected"
    FIELD_CHANGED = "field_changed"
    FIELD_GAVE_UP = "field_gave_up"
    KNOCKOUT = "knockout"
    KNOCKOUT_CORRECTED = "knockout_corrected"
    STATUS_CHANGED = "status_changed"
    KEEP_ON_FILE = "keep_on_file"
    SLOTS_OFFERED = "slots_offered"
    SLOT_TAKEN = "slot_taken"
    NO_SLOT_FITS = "no_slot_fits"
    PREFERRED_TIMES_GIVEN = "preferred_times_given"
    HUMAN_REQUESTED = "human_requested"
    INTERVIEW_BOOKED = "interview_booked"
    REMINDER_SENT = "reminder_sent"
    ABANDONED = "abandoned"
    ABUSE_FLAGGED = "abuse_flagged"
    SENSITIVE_DATA_REDACTED = "sensitive_data_redacted"
    CONFIRMATION_ASKED = "confirmation_asked"
    CONFIRMATION_ANSWERED = "confirmation_answered"
    LOCATION_GEOCODED = "location_geocoded"
    EXTRACTOR_FAILED = "extractor_failed"
    RESPONDER_FAILED = "responder_failed"
    REPLY_REJECTED = "reply_rejected"
    NON_TEXT_MESSAGE = "non_text_message"
    # Recruiter actions
    OVERRIDE = "override"
    NOTE_ADDED = "note_added"
    VIEWED = "viewed"
    EXPORTED = "exported"


class DomainEvent(BaseModel):
    type: EventType
    data: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TurnResult(BaseModel):
    state: ConversationState
    actions: list[Action]
    replies: list[str]
    events: list[DomainEvent]
    redact_inbound: bool = False
    hold_inbound: bool = Field(
        default=False,
        description="Keep the inbound messages unprocessed: no LLM answered, retry them at "
        "state.retry_turn_at.",
    )
