"""Templates: the config must cover everything the domain can render, checked at startup."""

import pytest

from app.adapters.config.yaml_loader import load_client_config
from app.domain.config import ClientConfig
from app.domain.models import Candidate, FieldName, Reason, Vehicle
from app.domain.render import field_values, required_templates, template_problems
from app.domain.summary import REASON_TEXT


def test_shipped_config_has_every_template(config: ClientConfig) -> None:
    assert template_problems(config) == []


def test_reminder_templates_follow_the_configured_reminders(config: ClientConfig) -> None:
    longer = config.model_copy(update={"reminder_hours": [1, 2, 3]})
    assert "reminder_3" in required_templates(longer)
    assert "es/chat: missing reminder_3" in template_problems(longer)


@pytest.mark.parametrize(
    ("key", "text", "problem"),
    [
        ("qualified", "¡Bien, {nombre}!", "es/chat: qualified: unknown placeholder nombre"),
        ("weekdays", "lun,mar", "es/chat: weekdays needs 7 comma-separated items"),
        ("closed", "{", "es/chat: closed: Single '{' encountered in format string"),
    ],
)
def test_bad_templates_are_reported(
    config: ClientConfig, key: str, text: str, problem: str
) -> None:
    templates = {**config.templates, "es": {**config.templates["es"], key: text}}
    assert problem in template_problems(config.model_copy(update={"templates": templates}))


def test_missing_template_stops_the_config_from_loading(tmp_path, config: ClientConfig) -> None:
    import shutil

    from app.adapters.config.yaml_loader import CONFIG_DIR

    shutil.copytree(CONFIG_DIR, tmp_path, dirs_exist_ok=True)
    chat = tmp_path / config.client_id / "templates" / "en" / "chat.yaml"
    chat.write_text(
        "\n".join(line for line in chat.read_text().splitlines() if not line.startswith("closed:"))
    )
    with pytest.raises(ValueError, match="en/chat: missing closed"):
        load_client_config(config.client_id, tmp_path)


def test_every_reason_has_a_description_for_recruiters() -> None:
    assert set(REASON_TEXT) == set(Reason)


def test_recap_shows_the_license_type_and_years(config: ClientConfig) -> None:
    candidate = Candidate(has_license=True, vehicle=Vehicle.MOTO, license_type="B", license_years=1)
    values = field_values(candidate, config, "es")
    assert values[FieldName.LICENSE] == "moto (tipo B, 1 año)"
    candidate.license_years = None
    assert field_values(candidate, config, "en")[FieldName.LICENSE] == "moto (B license)"


def test_no_experience_reads_as_words_not_zero_years(config: ClientConfig) -> None:
    candidate = Candidate(experience_years=0)
    assert field_values(candidate, config, "es")[FieldName.EXPERIENCE] == "sin experiencia"
    candidate.platforms = ["Glovo"]
    assert field_values(candidate, config, "es")[FieldName.EXPERIENCE] == "0 años · Glovo"
