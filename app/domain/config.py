"""Client configuration schema.

Everything client-specific lives in config (see CLAUDE.md, "Rules live in config"). Loading it
from YAML is an adapter concern; the domain only defines and reads this schema.
"""

from pydantic import BaseModel, Field

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
    nearby: list[str] = Field(
        default_factory=list,
        description="Places just outside the area: they go to human review, not a knockout.",
    )


class LicenseRule(BaseModel):
    """A license type that needs a minimum number of years for a vehicle (else: review)."""

    vehicle: Vehicle
    license_type: str
    min_years: float


class CountryLicenseRules(BaseModel):
    accepted_for_moto: list[str] = Field(default_factory=list)
    accepted_for_car: list[str] = Field(default_factory=list)
    conditional: list[LicenseRule] = Field(default_factory=list)


class InterviewConfig(BaseModel):
    slot_times: list[str] = Field(default_factory=lambda: ["10:00", "12:00", "17:00"])
    offer_count: int = 3
    min_days_ahead: int = 1


class Persona(BaseModel):
    name: str
    company: str


class FaqEntry(BaseModel):
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
    max_abuse_strikes: int = 2
    far_start_days: int = 60
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

    def template(self, language: Language, key: str, channel: Channel = "chat") -> str:
        if channel == "voice" and key in self.voice_templates.get(language, {}):
            return self.voice_templates[language][key]
        return self.templates[language][key]

    def reminders_for(self, channel: Channel) -> list[float]:
        return self.channel_reminder_hours.get(channel, self.reminder_hours)

    def location(self, location_id: str) -> ServiceLocation | None:
        return next((loc for loc in self.locations if loc.id == location_id), None)
