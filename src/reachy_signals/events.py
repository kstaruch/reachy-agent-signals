"""Event shape shared by adapters (``emit``) and the bridge.

Stdlib only: ``emit`` imports this on every hook, so it must stay cheap to import.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# Events that can make the robot react.
REACTION_EVENTS = ("started", "done", "blocked")
# ``observe``: logged for the spec's open questions, never reacted to.
EVENT_NAMES = (*REACTION_EVENTS, "observe")

_SLASH = re.compile(r"^\s*(/[\w:.-]+)")

# Hook payload fields worth keeping in the event log. Never the prompt text or tool input.
_HOOK_FIELDS = (
    "hook_event_name",
    "notification_type",
    "tool_name",
    "permission_mode",
    "stop_hook_active",
    "agent_type",
    "reason",
    "error",
)


class EventError(ValueError):
    """An event payload is missing fields or has the wrong types."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    source: str
    event: str
    session: str = ""
    cwd: str = ""
    detail: str = ""
    ts: str = field(default_factory=now_iso)
    hook: Mapping[str, Any] = field(default_factory=dict)  # selected hook payload fields

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["hook"] = dict(self.hook)
        return d

    @classmethod
    def from_dict(cls, data: Any) -> Event:
        if not isinstance(data, Mapping):
            raise EventError("event must be a JSON object")
        source, event = data.get("source"), data.get("event")
        if not isinstance(source, str) or not source:
            raise EventError("'source' must be a non-empty string")
        if event not in EVENT_NAMES:
            raise EventError(f"'event' must be one of {', '.join(EVENT_NAMES)}, got {event!r}")
        values: dict[str, Any] = {"source": source, "event": event}
        for key in ("session", "cwd", "detail", "ts"):
            if key in data:
                if not isinstance(data[key], str):
                    raise EventError(f"'{key}' must be a string")
                values[key] = data[key]
        hook = data.get("hook", {})
        if not isinstance(hook, Mapping):
            raise EventError("'hook' must be an object")
        values["hook"] = dict(hook)
        return cls(**values)

    def timestamp(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def hook_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Selected, privacy-safe fields of a Claude Code hook payload.

    The prompt is reduced to its slash command (if any) and length: enough to tell
    ``/reachy`` or ``/loop`` from a human prompt without logging what was typed.
    """
    out: dict[str, Any] = {
        k: payload[k]
        for k in _HOOK_FIELDS
        if k in payload and isinstance(payload[k], str | bool | int | float)
    }
    if "agent_id" in payload:
        out["subagent"] = True
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        m = _SLASH.match(prompt)
        out["prompt"] = {"length": len(prompt), "slash": m.group(1) if m else None}
    return out


def from_hook(event: str, payload: Mapping[str, Any], source: str = "claude-code") -> Event:
    """Build an event from a Claude Code hook payload (see docs/spec.md → Claude Code mapping)."""
    hook = hook_summary(payload)
    name = hook.get("hook_event_name", "")
    if event == "observe":
        detail = ":".join(
            str(p) for p in (name, hook.get("notification_type") or hook.get("tool_name")) if p
        )
    else:
        detail = str(hook.get("notification_type", ""))
    session = payload.get("session_id")
    cwd = payload.get("cwd")
    return Event(
        source=source,
        event=event,
        session=session if isinstance(session, str) else "",
        cwd=cwd if isinstance(cwd, str) else "",
        detail=detail,
        hook=hook,
    )
