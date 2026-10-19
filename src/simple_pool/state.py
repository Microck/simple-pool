"""Persistent pool state: preferred index and cooldown map."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any


class PoolState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._persisted = False
        self.current_index: int = 0
        self.cooldowns: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            current_index = data.get("current_index", 0)
            if type(current_index) is not int:
                return
            raw_cooldowns = data.get("cooldowns")
            if raw_cooldowns is None:
                raw_cooldowns = {}
            if current_index < 0 or not isinstance(raw_cooldowns, dict):
                return
            cooldowns = {str(k): float(v) for k, v in raw_cooldowns.items()}
            if any(not math.isfinite(v) or v < 0 for v in cooldowns.values()):
                return
            self.current_index = current_index
            self.cooldowns = cooldowns
            self._persisted = True
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            OverflowError,
            json.JSONDecodeError,
        ):
            pass

    def _save(self) -> None:
        self._persisted = False
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"current_index": self.current_index, "cooldowns": self.cooldowns}, f)
        os.replace(tmp, str(self.path))
        self._persisted = True

    def mark_success(self, name: str, index: int) -> None:
        with self._lock:
            changed = name in self.cooldowns or self.current_index != index
            self.cooldowns.pop(name, None)
            self.current_index = index
            if changed or not self._persisted:
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
