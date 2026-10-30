"""Load a client configuration: `app/config/<client_id>/client.yaml` plus the client's own
`templates/<lang>/<channel>.yaml` (each client words its messages its own way)."""

from pathlib import Path

import yaml

from app.domain.config import ClientConfig
from app.domain.render import template_problems

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def load_client_config(client_id: str, config_dir: Path = CONFIG_DIR) -> ClientConfig:
    client_dir = config_dir / client_id
    raw = yaml.safe_load((client_dir / "client.yaml").read_text(encoding="utf-8"))
    if raw.get("client_id") != client_id:
        raise ValueError(f"{client_dir / 'client.yaml'} has client_id {raw.get('client_id')!r}")
    templates: dict[str, dict[str, str]] = {}
    voice: dict[str, dict[str, str]] = {}
    for lang_dir in sorted((client_dir / "templates").iterdir()):
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
