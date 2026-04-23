"""Configuration helpers for workspace-local compiler settings.

This module provides a tiny read/write layer around `.brain/config.yaml`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "model": "gpt-5.4-mini",
    "language": "vi",
    "pageindex_threshold": 20,
}


def load_config(config_path: Path) -> dict[str, Any]:
    """Load compiler config and merge it with defaults.

    Args:
        config_path: Path to `.brain/config.yaml`.

    Returns:
        Effective configuration dictionary.
    """
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        config.update(data)
    return config


def save_config(config_path: Path, config: dict[str, Any]) -> None:
    """Persist compiler config to YAML.

    Args:
        config_path: Path to `.brain/config.yaml`.
        config: Configuration values to store.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=True)
