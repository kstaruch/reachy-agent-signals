"""Bridge decisions that don't need the robot (spec → Bridge behaviour, step 1)."""

from __future__ import annotations

from .events import Event
from .settings import Settings

# Decision labels written to the event log.
OBSERVE = "observe"  # observation-only event, never reacts
DISABLED = "disabled"  # master switch off
SOURCE_OFF = "source-off"
SHADOW = "shadow"  # logged to compare sources, robot doesn't move
PASS = "pass"  # goes on to reaction choice (milestone 4)


def drop_or_pass(settings: Settings, event: Event) -> str:
    if event.event == "observe":
        return OBSERVE
    if not settings.enabled:
        return DISABLED
    mode = settings.source_mode(event.source)
    if mode == "off":
        return SOURCE_OFF
    if mode == "shadow":
        return SHADOW
    return PASS
