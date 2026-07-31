"""Configuration loading with environment variable expansion."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def expand_env(value: str) -> str:
    """Replace ${VAR} or $VAR placeholders with environment variable values."""

    def replacer(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        result = os.environ.get(name, "")
        return result

    return re.sub(r"\$\{([^}]+)\}|\$([A-Z_][A-Z0-9_]*)", replacer, value)


def expand_env_recursive(obj: Any) -> Any:
    if isinstance(obj, str):
        return expand_env(obj)
    if isinstance(obj, dict):
        return {k: expand_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_recursive(item) for item in obj]
    return obj


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML or JSON configuration file with env-var expansion."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise ImportError(
                "PyYAML is required to load YAML configs. "
                "Install it with: pip install simple-pool[yaml]"
            ) from exc
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)

    return expand_env_recursive(raw or {})


def parse_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw config into internal representation."""
    defaults = {
        "host": "127.0.0.1",
        "port": 8000,
        "model": "default",
        "request_timeout": 120,
        "cooldown": 1200,
        "failure_codes": [403, 408, 429, 500, 502, 503, 504],
        "auth_token": None,
        "state_path": ".simple-pool-state.json",
    }
    config = {**defaults, **{k: v for k, v in raw.items() if k != "backends"}}
    config["failure_codes"] = set(config["failure_codes"])

    backends = []
    for entry in raw.get("backends", []):
        backend = {
            "name": entry["name"],
            "base_url": entry["base_url"].rstrip("/"),
            "api_key": entry.get("api_key", ""),
            "model": entry.get("model"),
        }
        backends.append(backend)
    config["backends"] = backends
    return config
