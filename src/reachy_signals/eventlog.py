"""JSONL event log: every event the bridge receives, with what it decided."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from platformdirs import user_log_dir

LOG_DIR_ENV = "REACHY_SIGNALS_LOG_DIR"
MAX_BYTES = 10_000_000  # then rotate to events.1.jsonl (one backup kept)


def log_dir() -> Path:
    """``$REACHY_SIGNALS_LOG_DIR``, else platformdirs: macOS ``~/Library/Logs/reachy-signals``,
    Windows ``%LOCALAPPDATA%\\reachy-signals\\Logs``."""
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    return Path(user_log_dir("reachy-signals", appauthor=False))


class EventLog:
    def __init__(self, path: Path, max_bytes: int = MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except FileNotFoundError:
            return
        self.path.replace(self.path.with_suffix(".1.jsonl"))

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]
