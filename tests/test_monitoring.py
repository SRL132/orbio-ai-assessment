"""The alert rules only work if they name metrics the app really exports."""

import re
from pathlib import Path

import yaml

from app import metrics

RULES = Path(__file__).parent.parent / "monitoring" / "alerts.yml"
HISTOGRAM_PARTS = re.compile(r"_(bucket|sum|count)$")


def rule_expressions() -> list[str]:
    groups = yaml.safe_load(RULES.read_text())["groups"]
    return [rule["expr"] for group in groups for rule in group["rules"]]


def exported_metrics() -> set[str]:
    source = Path(metrics.__file__).read_text()
    backlog = {
        name
        for key in metrics._BACKLOG_HELP
        for name in (f"sazon_{key}", f"sazon_{key}_oldest_seconds")
    }
    return set(re.findall(r"\"(sazon_[a-z_]+)\"", source)) | backlog


def test_every_metric_in_an_alert_rule_exists() -> None:
    exported = exported_metrics()
    assert exported
    used = {name for expr in rule_expressions() for name in re.findall(r"sazon_[a-z_]+", expr)}
    assert used
    assert {HISTOGRAM_PARTS.sub("", name) for name in used} <= exported
