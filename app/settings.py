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

    # Optional: speak voice-channel replies with ElevenLabs instead of the browser's voice.
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"

    database_url: str = "sqlite:///./data/sazon.db"
    client_id: str = "grupo_sazon"

    # Demo mode: the clock can be fast-forwarded from the chat page to show reminders.
    demo_mode: bool = True
    debounce_seconds: float = 1.5
    reminder_interval_seconds: float = 30.0

    recruiter_users: SecretStr | None = None
    session_secret: SecretStr | None = None

    @field_validator(
        "openrouter_api_key",
        "anthropic_api_key",
        "elevenlabs_api_key",
        "recruiter_users",
        "session_secret",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        """`KEY=` in .env means "not set", never an empty secret."""
        return None if isinstance(value, str) and not value.strip() else value
