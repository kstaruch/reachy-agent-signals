"""Hot reload: poll the settings file's mtime and size, reload when they change.

Polling instead of OS file-watch APIs keeps this dependency-free and identical on
macOS and Windows (D9). An invalid edit never replaces the last good settings.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from .settings import Settings, SettingsError, load

log = logging.getLogger(__name__)

Signature = tuple[int, int] | None


def _signature(path: Path) -> Signature:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


class SettingsWatcher:
    """Holds the current settings and reloads them when the file changes.

    Loading at construction raises :class:`SettingsError` for an invalid file, so a
    broken config is reported at startup. Later invalid edits are logged and ignored.
    """

    def __init__(
        self,
        path: Path,
        interval: float = 1.0,
        on_change: Callable[[Settings], None] | None = None,
    ) -> None:
        self.path = path
        self.interval = interval
        self._on_change = on_change
        self._lock = threading.Lock()
        self._sig = _signature(path)
        self._settings = load(path)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def current(self) -> Settings:
        with self._lock:
            return self._settings

    def check(self) -> bool:
        """Poll once. Returns True if new settings were loaded."""
        sig = _signature(self.path)
        if sig == self._sig:
            return False
        self._sig = sig  # remember even if invalid, so one bad edit logs once
        try:
            new = load(self.path)
        except SettingsError as e:
            log.error("settings: keeping previous settings: %s", e)
            return False
        with self._lock:
            self._settings = new
        log.info("settings: reloaded %s", self.path if sig else "(file removed: defaults)")
        if self._on_change:
            self._on_change(new)
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="settings-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check()
            except Exception:  # never let the watcher thread die
                log.exception("settings: watcher error")

    def __enter__(self) -> SettingsWatcher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
