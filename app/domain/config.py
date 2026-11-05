"""Client configuration schema.

Everything client-specific lives in config (see CLAUDE.md, "Rules live in config"). Loading it
from YAML is an adapter concern; the domain only defines and reads this schema.
"""

from typing import Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from app.domain.models import Channel, FieldName, Language, Vehicle


class ServiceLocation(BaseModel):
    id: str
    country: str
    city: str
    aliases: list[str] = Field(default_factory=list)
    zones: list[str] = Field(
        default_factory=list, description="Served zones. Empty means the whole city is served."
    )
    postal_prefixes: list[str] = Field(default_factory=list)
    timezone: str = "Europe/Madrid"
    shared_name: bool = Field(
        default=False,
        description="Another country we hire in has a different city of this name (Guadalajara, "
        "Spain and Mexico): read the country back, unless the postal code settles it.",
    )
    nearby: list[str] = Field(
        default_factory=list,
        description="Places just outside the area: they go to human review, not a knockout.",
    )
    lat: float | None = Field(default=None, description="City centre, for geocoded distances.")
    lon: float | None = None


class LicenseRule(BaseModel):
    """A license type that needs a minimum number of years for a vehicle (else: review)."""

    vehicle: Vehicle
    license_type: str
    min_years: float


class CountryLicenseRules(BaseModel):
    accepted_for_moto: list[str] = Field(default_factory=list)
    accepted_for_car: list[str] = Field(default_factory=list)
    conditional: list[LicenseRule] = Field(default_factory=list)


class DaysOnlyRule(BaseModel):
    """Availability answers that only name days, e.g. "entre semana": neither full nor part time."""

    phrases: list[str] = Field(default_factory=list)
    unless: list[str] = Field(
        default_factory=list, description="Words that do say how much: then the answer counts."
    )


class InterviewConfig(BaseModel):
    slot_times: list[str] = Field(default_factory=lambda: ["10:00", "12:00", "17:00"])
    offer_count: int = 3
    min_days_ahead: int = 1


class Persona(BaseModel):
    name: str
    company: str


class FaqEntry(BaseModel):
    id: str
    question: str
    answer: dict[Language, str]
    keywords: list[str] = Field(
        default_factory=list, description="Offline matching only; the LLM reads the whole FAQ."
    )


class ClientConfig(BaseModel):
    client_id: str
    persona: Persona
    field_priority: list[FieldName] = Field(default_factory=lambda: list(FieldName))
    locations: list[ServiceLocation]
    license_rules: dict[str, CountryLicenseRules] = Field(default_factory=dict)
    known_platforms: list[str] = Field(default_factory=list)
    max_attempts: int = 2
    max_recap_rounds: int = 3
    confirm_below_confidence: float = Field(
        default=0.5,
        description="A value the model is less sure of than this is confirmed with the "
        "candidate (yes/no) before it counts, including a knockout.",
    )
    always_confirm: dict[Channel, list[FieldName]] = Field(
        default_factory=dict,
        description="Fields confirmed on this channel whatever the model's confidence. On voice "
        "the model only sees the transcript, so it can be sure of a name or place that speech "
        "recognition got wrong. If the candidate says no, they're asked to type it instead.",
    )
    template_questions_on: list[Channel] = Field(
        default_factory=list,
        description="Channels where a plain next question uses its template instead of the LLM. "
        "Re-asks and questions with options after confusion still use the LLM.",
    )
    geo_review_radius_km: float = Field(
        default=30.0,
        description="A place not in the config but this close to a served city goes to review "
        "(after the candidate confirms it); farther away is outside the area.",
    )
    availability_days_only: DaysOnlyRule = Field(default_factory=DaysOnlyRule)
    max_abuse_strikes: int = 2
    far_start_days: int = 60
    default_timezone: str = Field(
        default="Europe/Madrid",
        description='What "today" means until the candidate\'s location, and so its timezone, '
        "is known.",
    )
    country_names: dict[str, dict[Language, str]] = Field(
        default_factory=dict,
        description="Country names per language, for reading back a location with `shared_name`.",
    )
    reminder_hours: list[float] = Field(default_factory=lambda: [2.0, 24.0])
    channel_reminder_hours: dict[str, list[float]] = Field(
        default_factory=dict,
        description="Per-channel override, e.g. WhatsApp keeps reminders inside its 24 h window.",
    )
    abandon_after_hours: float = 24.0
    llm_retry_seconds: list[float] = Field(
        default_factory=lambda: [20.0, 60.0, 180.0, 600.0],
        description="When no LLM answers, the candidate's message is held and retried after "
        "each delay in turn (one holding message first). After the last, they're asked to "
        "write it again; reminders still follow, so nobody is dropped silently.",
    )
    retention_days: int = Field(
        default=183,
        description="Disqualified and abandoned candidates are erased this long after their last "
        "activity, unless they agreed to stay on file (docs/process-design-appendix.md).",
    )
    max_reply_chars: int = 250
    screening_target_hours: float = Field(
        default=24.0,
        description="Pilot success metric: share of applicants who reach an outcome this many "
        "hours after applying (analytics).",
    )
    interview: InterviewConfig = Field(default_factory=InterviewConfig)
    templates: dict[Language, dict[str, str]]
    voice_templates: dict[Language, dict[str, str]] = Field(
        default_factory=dict, description="Spoken overrides; missing keys fall back to chat."
    )
    faq: list[FaqEntry] = Field(default_factory=list)
    privacy_url: str = Field(
        default="/privacy",
        description="Where the consent message points ({privacy_url}). The app serves "
        "`privacy_notice` at /privacy; WhatsApp needs an absolute URL here.",
    )
    privacy_notice: dict[Language, str] = Field(
        default_factory=dict, description="The privacy notice shown at /privacy, per language."
    )

    @model_validator(mode="after")
    def _faq_ids_are_unique(self) -> Self:
        ids = [entry.id for entry in self.faq]
        if len(ids) != len(set(ids)):
            raise ValueError("FAQ ids must be unique")
        return self

    @model_validator(mode="after")
    def _places_can_be_used(self) -> Self:
        """A bad timezone or a missing country name should stop the app at startup, not fail a
        candidate's turn."""
        for zone in (self.default_timezone, *(loc.timezone for loc in self.locations)):
            ZoneInfo(zone)
        for loc in self.locations:
            if loc.shared_name and loc.country not in self.country_names:
                raise ValueError(f"{loc.id} has shared_name: country_names needs {loc.country}")
        return self

    def faq_answer(self, faq_id: str | None, language: Language) -> str | None:
        """The approved answer for this id; None for no id or one the config doesn't know."""
        entry = next((e for e in self.faq if e.id == faq_id), None)
        return entry.answer[language] if entry else None

    def template(self, language: Language, key: str, channel: Channel = "chat") -> str:
        if channel == "voice" and key in self.voice_templates.get(language, {}):
            return self.voice_templates[language][key]
        return self.templates[language][key]

    def reminders_for(self, channel: Channel) -> list[float]:
        return self.channel_reminder_hours.get(channel, self.reminder_hours)

    def location(self, location_id: str) -> ServiceLocation | None:
        return next((loc for loc in self.locations if loc.id == location_id), None)
