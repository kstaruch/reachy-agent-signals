"""Presence on the bridge side: is the phone near, is the robot asleep, was anything missed.

Pure and clock-driven like scheduler.py (callers pass ``now``, monotonic seconds).
The reactor thread asks :meth:`Presence.due` what to do and does the daemon calls.
See docs/spec.md → Presence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .settings import Settings

SLEEP = "sleep"
WAKE = "wake"
RETRY = 30.0  # seconds before trying again after a sleep/wake that couldn't be done


@dataclass
class Presence:
    phone: str | None = None  # near | away | None: nothing heard since the bridge started
    since: float = 0.0  # when ``phone`` last changed
    robot: str = "awake"  # the daemon wakes the robot at boot; asleep = put to sleep by us
    missed: bool = False
    retry_at: float = 0.0

    def observe(self, state: str, now: float) -> bool:
        """A ``near``/``away`` event. True if it changed what we know."""
        if state == self.phone:
            return False
        self.phone, self.since, self.retry_at = state, now, 0.0
        return True

    def asleep(self, settings: Settings) -> bool:
        """True while reactions must not play: the bridge has put the robot to sleep."""
        return settings.presence.mode != "off" and self.robot == "asleep"

    def note(self, event: str, settings: Settings) -> None:
        """An event that would have played arrived while asleep."""
        if event in settings.presence.missed_events:
            self.missed = True

    def due(self, settings: Settings, now: float) -> str | None:
        """``SLEEP``, ``WAKE`` or None: what the robot should do now."""
        if settings.presence.mode == "off":
            # Presence switched off: behave as without it. A robot we left asleep is
            # woken by the next reaction (reactor step 4), so forget our view of it.
            self.robot, self.missed = "awake", False
            return None
        if now < self.retry_at:
            return None
        want = self._want(settings, now)
        if want == "asleep" and self.robot == "awake":
            return SLEEP
        if want == "awake" and self.robot == "asleep":
            return WAKE
        return None

    def next_wakeup(self, settings: Settings) -> float | None:
        """Monotonic time when :meth:`due` may next change by itself (sleep timer, retry)."""
        if settings.presence.mode == "off":
            return None
        if self.retry_at:
            return self.retry_at
        if settings.presence.mode == "auto" and self.phone == "away" and self.robot == "awake":
            return self.since + settings.presence.sleep_after
        return None

    def done(self, action: str) -> bool:
        """Record a finished sleep/wake. For ``WAKE``, returns (and clears) ``missed``."""
        self.retry_at = 0.0
        if action == SLEEP:
            self.robot = "asleep"
            return False
        self.robot = "awake"
        missed, self.missed = self.missed, False
        return missed

    def failed(self, now: float) -> None:
        self.retry_at = now + RETRY

    def summary(self, settings: Settings, now: float) -> dict[str, Any]:
        mode = settings.presence.mode
        out: dict[str, Any] = {
            "mode": mode,
            "phone": self.phone,
            "robot": "asleep" if self.asleep(settings) else "awake",
            "missed": self.missed,
        }
        wake = self.next_wakeup(settings)
        if wake is not None and not self.retry_at:
            out["sleep_in"] = round(max(0.0, wake - now), 1)
        return out

    def _want(self, settings: Settings, now: float) -> str | None:
        mode = settings.presence.mode
        if mode in ("awake", "asleep"):
            return mode
        if self.phone == "near":
            return "awake"
        if self.phone == "away" and now - self.since >= settings.presence.sleep_after:
            return "asleep"
        return None
