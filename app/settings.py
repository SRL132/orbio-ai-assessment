"""Runtime settings, read from environment variables (and `.env` in development)."""

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: Literal["fake", "openrouter", "anthropic"] = "fake"
    llm_timeout_seconds: float = 20.0

    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = ""

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    # USD per million tokens, from Anthropic's pricing page. The API reports tokens, not cost;
    # without these the usage meter can't price Anthropic calls (e.g. for an eval budget).
    anthropic_input_usd_per_mtok: float | None = None
    anthropic_output_usd_per_mtok: float | None = None

    # Optional: speak voice-channel replies with ElevenLabs instead of the browser's voice.
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"

    # Optional: WhatsApp Cloud API (Meta). Set the access token to turn the channel on.
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_api_version: str = "v26.0"
    whatsapp_reminder_template: str = ""  # approved template for messages after 24 h (paid)
    whatsapp_dispatch_seconds: float = 1.0

    database_url: str = "sqlite:///./data/sazon.db"
    client_id: str = "grupo_sazon"

    # Demo mode: the clock can be fast-forwarded from the chat page to show reminders.
    demo_mode: bool = True
    debounce_seconds: float = 1.5
    reminder_interval_seconds: float = 30.0

    recruiter_users: SecretStr | None = None
    session_secret: SecretStr | None = None
    # When set, /metrics needs "Authorization: Bearer <token>" (else keep it off the internet).
    metrics_token: SecretStr | None = None

    # Rate limits (shared by every worker: counted in the database).
    login_attempts_per_ip_per_15_min: int = 20
    login_failures_per_email_per_15_min: int = 5  # only failures, so nobody can lock you out
    chat_starts_per_ip_per_hour: int = 30
    chat_messages_per_conversation_per_minute: int = 20  # caps LLM spend per conversation

    @field_validator(
        "openrouter_api_key",
        "anthropic_api_key",
        "elevenlabs_api_key",
        "whatsapp_access_token",
        "whatsapp_verify_token",
        "whatsapp_app_secret",
        "recruiter_users",
        "session_secret",
        "metrics_token",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        """`KEY=` in .env means "not set", never an empty secret."""
        return None if isinstance(value, str) and not value.strip() else value
