"""Domain models: the candidate record, conversation state, LLM extraction and engine actions.

Pydantic models are the single schema: the same types are validated here, persisted by the
storage adapter, and handed to the LLM as the structured-output contract.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


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


Language = Literal["es", "en"]


# --- Candidate record ---------------------------------------------------------------------------


class FieldMeta(BaseModel):
    """Traceability for one field: where the value came from and how sure we are."""

    status: FieldStatus = FieldStatus.MISSING
    quote: str | None = None
    confidence: float | None = None
    attempts: int = 0
    note: str | None = None


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
    candidate: Candidate = Field(default_factory=Candidate)
    status: Status = Status.IN_PROGRESS
    stage: Stage = Stage.CONSENT
    awaiting: FieldName | None = None
    language: Language = "es"
    review_reasons: list[str] = Field(default_factory=list)
    disqualify_reason: str | None = None
    keep_on_file: bool | None = None
    abuse_strikes: int = 0
    offered_human: bool = False
    reminders_sent: int = 0
    next_reminder_at: datetime | None = None
    started_at: datetime | None = None
    last_inbound_at: datetime | None = None
    ended_at: datetime | None = None


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


# --- Engine output ------------------------------------------------------------------------------


class AskField(BaseModel):
    kind: Literal["ask_field"] = "ask_field"
    field: FieldName
    offer_options: bool = False


class ReAsk(BaseModel):
    kind: Literal["re_ask"] = "re_ask"
    field: FieldName
    reason: str
    offer_options: bool = False


class AnswerQuestion(BaseModel):
    kind: Literal["answer_question"] = "answer_question"
    question: str


class Disqualify(BaseModel):
    kind: Literal["disqualify"] = "disqualify"
    reason: str


class HandOff(BaseModel):
    kind: Literal["hand_off"] = "hand_off"
    reason: str


class Recap(BaseModel):
    kind: Literal["recap"] = "recap"


class Qualified(BaseModel):
    kind: Literal["qualified"] = "qualified"


class Say(BaseModel):
    """A fixed template message (consent, warnings, closings)."""

    kind: Literal["say"] = "say"
    template: str


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
    | Say
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


class DomainEvent(BaseModel):
    type: str
    data: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TurnResult(BaseModel):
    state: ConversationState
    actions: list[Action]
    replies: list[str]
    events: list[DomainEvent]
    redact_inbound: bool = False
