"""Bridge decisions that don't need the robot (spec → Bridge behaviour, step 1)."""

from __future__ import annotations

from .events import PRESENCE_EVENTS, Event
from .settings import Settings

# Decision labels written to the event log.
OBSERVE = "observe"  # observation-only event, never reacts
DISABLED = "disabled"  # master switch off
SOURCE_OFF = "source-off"
SHADOW = "shadow"  # logged to compare sources, robot doesn't move
PASS = "pass"  # goes on to reaction choice
CONTROL = "control"  # a /reachy command's own turn: the robot doesn't react to itself
PRESENCE = "presence"  # near/away from presence-watch: changes presence state, no reaction
ASLEEP = "asleep"  # would have passed, but presence has the robot asleep (set by the bridge)


def drop_or_pass(settings: Settings, event: Event) -> str:
    if event.event in PRESENCE_EVENTS:
        return PRESENCE
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
