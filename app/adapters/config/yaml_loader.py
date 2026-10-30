"""Load a client configuration from `app/config/<client_id>.yaml` plus its templates."""

from pathlib import Path

import yaml

from app.domain.config import ClientConfig
from app.domain.render import template_problems

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def load_client_config(client_id: str, config_dir: Path = CONFIG_DIR) -> ClientConfig:
    raw = yaml.safe_load((config_dir / f"{client_id}.yaml").read_text(encoding="utf-8"))
    templates: dict[str, dict[str, str]] = {}
    voice: dict[str, dict[str, str]] = {}
    for lang_dir in sorted((config_dir / "templates").iterdir()):
        if lang_dir.is_dir():
            chat_file = lang_dir / "chat.yaml"
            voice_file = lang_dir / "voice.yaml"
            templates[lang_dir.name] = yaml.safe_load(chat_file.read_text(encoding="utf-8"))
            if voice_file.exists():
                voice[lang_dir.name] = yaml.safe_load(voice_file.read_text(encoding="utf-8"))
    raw["templates"] = templates
    raw["voice_templates"] = voice
    config = ClientConfig.model_validate(raw)
    if problems := template_problems(config):  # fail at startup, not mid-conversation
        raise ValueError(f"{client_id} templates: " + "; ".join(problems))
    return config
