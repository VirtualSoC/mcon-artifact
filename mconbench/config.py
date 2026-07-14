"""Minimal YAML config loader with ${ENV} expansion and dotted-key access."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class Config:
    """Wraps the parsed YAML tree; `get('a.b.c', default)` reads nested keys."""

    def __init__(self, data: dict) -> None:
        self.data = _expand(data or {})

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path) as fh:
            return cls(yaml.safe_load(fh))

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur
