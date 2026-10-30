"""Load a client configuration from `app/config/<client_id>.yaml` plus its templates."""

from pathlib import Path

import yaml

from app.domain.config import ClientConfig

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def load_client_config(client_id: str, config_dir: Path = CONFIG_DIR) -> ClientConfig:
    raw = yaml.safe_load((config_dir / f"{client_id}.yaml").read_text(encoding="utf-8"))
    templates: dict[str, dict[str, str]] = {}
    for lang_dir in sorted((config_dir / "templates").iterdir()):
        if lang_dir.is_dir():
            templates[lang_dir.name] = {}
            for path in sorted(lang_dir.glob("*.yaml")):
                templates[lang_dir.name].update(yaml.safe_load(path.read_text(encoding="utf-8")))
    raw["templates"] = templates
    return ClientConfig.model_validate(raw)
