"""Settings: one TOML file, read with stdlib tomllib, defaults equal to config.example.toml."""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

log = logging.getLogger(__name__)

APP_NAME = "reachy-signals"
CONFIG_ENV = "REACHY_SIGNALS_CONFIG"
EVENTS = ("started", "done", "blocked")
MODES = ("drive", "shadow", "off")


class SettingsError(ValueError):
    """The settings file can't be parsed or holds an invalid value."""


@dataclass(frozen=True)
class TimeRange:
    """Local-time range ``start``–``end``; end is exclusive and may be past midnight."""

    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


@dataclass(frozen=True)
class Reactions:
    started: tuple[str, ...] = ("yeah_nod",)
    done: tuple[str, ...] = ("cheerful1",)
    blocked: tuple[str, ...] = ("attentive1",)

    def for_event(self, event: str) -> tuple[str, ...]:
        if event not in EVENTS:
            raise KeyError(event)
        return getattr(self, event)


@dataclass(frozen=True)
class BridgeSettings:
    port: int = 8765
    daemon_url: str = "http://127.0.0.1:8000/api"
    max_event_age: float = 10.0  # seconds


@dataclass(frozen=True)
class QuietSettings:
    enabled: bool = False
    schedule: tuple[TimeRange, ...] = ()
    reactions: Reactions = field(default_factory=Reactions)


@dataclass(frozen=True)
class EscalationSettings:
    blocked_after: float = 180.0  # seconds
    blocked: tuple[str, ...] = ("toc-toc-toc",)


def _default_sources() -> Mapping[str, str]:
    return {"claude-code": "drive", "herdr": "off"}


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    bridge: BridgeSettings = field(default_factory=BridgeSettings)
    sources: Mapping[str, str] = field(default_factory=_default_sources)
    quiet: QuietSettings = field(default_factory=QuietSettings)
    reactions: Reactions = field(default_factory=Reactions)
    escalation: EscalationSettings = field(default_factory=EscalationSettings)

    def source_mode(self, source: str) -> str:
        """Mode for a source; sources not in the file are ``off``."""
        return self.sources.get(source, "off")


def config_path() -> Path:
    """Settings file location: ``$REACHY_SIGNALS_CONFIG`` or the platformdirs config dir.

    macOS ``~/Library/Application Support/reachy-signals/config.toml``,
    Windows ``%APPDATA%\\reachy-signals\\config.toml``.
    """
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    return Path(user_config_dir(APP_NAME, appauthor=False, roaming=True)) / "config.toml"


def quiet_active(settings: Settings, now: datetime | None = None) -> bool:
    """Quiet mode is on by manual toggle or because local time is inside a schedule range."""
    if settings.quiet.enabled:
        return True
    t = (now or datetime.now()).time()
    return any(r.contains(t) for r in settings.quiet.schedule)


# --- parsing -----------------------------------------------------------------

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h)\s*$")
_UNITS = {"s": 1, "m": 60, "h": 3600}
_HHMM = r"([01]\d|2[0-3]):([0-5]\d)"
_RANGE = re.compile(rf"^\s*{_HHMM}\s*-\s*{_HHMM}\s*$")


def parse_duration(value: Any, key: str) -> float:
    """``"90s"``, ``"3m"``, ``"1h"`` or a plain number of seconds → seconds (> 0)."""
    if isinstance(value, bool):
        raise SettingsError(f"{key}: expected a duration like '90s' or '3m', got {value!r}")
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str) and (m := _DURATION.match(value)):
        seconds = float(m.group(1)) * _UNITS[m.group(2)]
    else:
        raise SettingsError(f"{key}: expected a duration like '90s' or '3m', got {value!r}")
    if seconds <= 0:
        raise SettingsError(f"{key}: duration must be greater than zero, got {value!r}")
    return seconds


def parse_time_range(value: Any, key: str) -> TimeRange:
    if not isinstance(value, str) or not (m := _RANGE.match(value)):
        raise SettingsError(f"{key}: expected 'HH:MM-HH:MM', got {value!r}")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    rng = TimeRange(time(h1, m1), time(h2, m2))
    if rng.start == rng.end:
        raise SettingsError(f"{key}: start and end are the same ({value!r})")
    return rng


def _table(data: Mapping[str, Any], name: str, key: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, Mapping):
        raise SettingsError(f"{key}: expected a table")
    return value


def _warn_unknown(data: Mapping[str, Any], known: set[str], prefix: str) -> None:
    for k in data:
        if k not in known:
            log.warning("settings: unknown key %r ignored", f"{prefix}{k}")


def _bool(data: Mapping[str, Any], name: str, default: bool, key: str) -> bool:
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise SettingsError(f"{key}: expected true or false, got {value!r}")
    return value


def _names(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise SettingsError(f"{key}: expected a list of move names, got {value!r}")
    return tuple(v.strip() for v in value)


def _reactions(data: Mapping[str, Any], prefix: str) -> Reactions:
    _warn_unknown(data, set(EVENTS), prefix)
    defaults = Reactions()
    values = {
        e: _names(data[e], f"{prefix}{e}") if e in data else defaults.for_event(e) for e in EVENTS
    }
    return Reactions(**values)


def _bridge(data: Mapping[str, Any]) -> BridgeSettings:
    _warn_unknown(data, {"port", "daemon_url", "max_event_age"}, "bridge.")
    d = BridgeSettings()
    port = data.get("port", d.port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SettingsError(f"bridge.port: expected a port number 1-65535, got {port!r}")
    url = data.get("daemon_url", d.daemon_url)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise SettingsError(f"bridge.daemon_url: expected an http(s) URL, got {url!r}")
    age = data.get("max_event_age")
    max_age = d.max_event_age if age is None else parse_duration(age, "bridge.max_event_age")
    return BridgeSettings(port=port, daemon_url=url.rstrip("/"), max_event_age=max_age)


def _sources(data: Mapping[str, Any]) -> Mapping[str, str]:
    sources = dict(_default_sources())
    for name, table in data.items():
        key = f"sources.{name}"
        if not isinstance(table, Mapping):
            raise SettingsError(f"{key}: expected a table")
        _warn_unknown(table, {"mode"}, f"{key}.")
        mode = table.get("mode", "off")
        if mode not in MODES:
            raise SettingsError(f"{key}.mode: expected one of {', '.join(MODES)}, got {mode!r}")
        sources[name] = mode
    return sources


def _quiet(data: Mapping[str, Any]) -> QuietSettings:
    schedule = data.get("schedule", [])
    if not isinstance(schedule, list):
        raise SettingsError(f"quiet.schedule: expected a list, got {schedule!r}")
    return QuietSettings(
        enabled=_bool(data, "enabled", False, "quiet.enabled"),
        schedule=tuple(parse_time_range(v, f"quiet.schedule[{i}]") for i, v in enumerate(schedule)),
        reactions=_reactions(_table(data, "reactions", "quiet.reactions"), "quiet.reactions."),
    )


def _escalation(data: Mapping[str, Any]) -> EscalationSettings:
    _warn_unknown(data, {"blocked_after", "blocked"}, "escalation.")
    d = EscalationSettings()
    after = data.get("blocked_after")
    return EscalationSettings(
        blocked_after=(
            d.blocked_after if after is None else parse_duration(after, "escalation.blocked_after")
        ),
        blocked=_names(data["blocked"], "escalation.blocked") if "blocked" in data else d.blocked,
    )


def parse(data: Mapping[str, Any]) -> Settings:
    """Build Settings from parsed TOML. Missing keys take defaults; unknown keys warn."""
    _warn_unknown(data, {"enabled", "bridge", "sources", "quiet", "reactions", "escalation"}, "")
    quiet = _table(data, "quiet", "quiet")
    _warn_unknown(quiet, {"enabled", "schedule", "reactions"}, "quiet.")
    return Settings(
        enabled=_bool(data, "enabled", True, "enabled"),
        bridge=_bridge(_table(data, "bridge", "bridge")),
        sources=_sources(_table(data, "sources", "sources")),
        quiet=_quiet(quiet),
        reactions=_reactions(_table(data, "reactions", "reactions"), "reactions."),
        escalation=_escalation(_table(data, "escalation", "escalation")),
    )


def load(path: Path | None = None) -> Settings:
    """Load settings from ``path`` (default :func:`config_path`). A missing file means defaults."""
    path = path or config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as e:
        raise SettingsError(f"{path}: can't read: {e}") from e
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{path}: invalid TOML: {e}") from e
    return parse(data)
