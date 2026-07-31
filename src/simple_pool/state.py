"""Persistent pool state: preferred index and cooldown map."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class PoolState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.current_index: int = 0
        self.cooldowns: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.current_index = int(data.get("current_index", 0))
            self.cooldowns = {str(k): float(v) for k, v in (data.get("cooldowns") or {}).items()}
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"current_index": self.current_index, "cooldowns": self.cooldowns}, f)
        os.replace(tmp, str(self.path))

    def mark_success(self, name: str, index: int) -> None:
        with self._lock:
            self.cooldowns.pop(name, None)
            self.current_index = index
            self._save()

    def mark_failure(self, name: str, cooldown: float) -> None:
        with self._lock:
            self.cooldowns[name] = time.time() + cooldown
            self._save()

    def available_order(self, backends: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
        """Return backends in preferred order, skipping those on cooldown."""
        now = time.time()
        with self._lock:
            n = len(backends)
            start = self.current_index % n if n else 0
            ordered = list(range(start, n)) + list(range(0, start))
            return [
                (i, backends[i])
                for i in ordered
                if self.cooldowns.get(backends[i]["name"], 0) <= now
            ]

    def all_on_cooldown(self, backends: list[dict[str, Any]]) -> bool:
        now = time.time()
        with self._lock:
            return all(self.cooldowns.get(b["name"], 0) > now for b in backends)
