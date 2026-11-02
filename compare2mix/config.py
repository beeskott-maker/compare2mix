"""User settings kept in ~/.compare2mix/config.json (Client ID, last library…)."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".compare2mix"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or CONFIG_FILE).read_text())
    except (OSError, ValueError):
        return {}


def update(path: Path | None = None, **values) -> dict:
    path = path or CONFIG_FILE
    config = load(path)
    config.update({k: v for k, v in values.items() if v is not None})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))
    return config
