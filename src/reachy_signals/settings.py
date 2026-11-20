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
# Reaction lists in [reactions] / [quiet.reactions]: one per event, plus `missed`
# (played after presence wakes the robot, if something arrived while it slept).
REACTION_KINDS = (*EVENTS, "missed")
MODES = ("drive", "shadow", "off")
PRESENCE_MODES = ("off", "auto", "awake", "asleep")


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
    missed: tuple[str, ...] = ("curious1",)

    def for_event(self, event: str) -> tuple[str, ...]:
        """Reaction list for an event name or ``missed``."""
        if event not in REACTION_KINDS:
            raise KeyError(event)
        return getattr(self, event)


@dataclass(frozen=True)
class BridgeSettings:
    port: int = 8765
    daemon_url: str = "http://127.0.0.1:8000/api"
    max_event_age: float = 10.0  # seconds
    url: str = ""  # clients only: a bridge on another machine; empty = this machine

    @property
    def remote(self) -> bool:
        """True when clients talk to a bridge elsewhere (``url`` set), not a local one."""
        return bool(self.url)

    @property
    def client_url(self) -> str:
        """Base URL the CLI and adapters use to reach the bridge."""
        return self.url or f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class QuietSettings:
    enabled: bool = False
    schedule: tuple[TimeRange, ...] = ()
    reactions: Reactions = field(default_factory=Reactions)


@dataclass(frozen=True)
class EscalationSettings:
    blocked_after: float = 180.0  # seconds
    blocked: tuple[str, ...] = ("toc-toc-toc",)


@dataclass(frozen=True)
class DetectorSettings:
    """``[presence.detector]``: read by ``presence-watch`` on the bridge host only."""

    device: str = ""  # the paired phone's Bluetooth address
    near_rssi: int = -65
    far_rssi: int = -70
    window: float = 8.0  # seconds of readings the median is taken over
    lost_after: float = 30.0  # nothing heard this long → away


@dataclass(frozen=True)
class PresenceSettings:
    mode: str = "off"  # off | auto | awake | asleep
    sleep_after: float = 120.0  # seconds away before the robot sleeps
    missed_events: tuple[str, ...] = ("done", "blocked")
    detector: DetectorSettings = field(default_factory=DetectorSettings)


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
    presence: PresenceSettings = field(default_factory=PresenceSettings)

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
    _warn_unknown(data, set(REACTION_KINDS), prefix)
    defaults = Reactions()
    values = {
        e: _names(data[e], f"{prefix}{e}") if e in data else defaults.for_event(e)
        for e in REACTION_KINDS
    }
    return Reactions(**values)


def _bridge(data: Mapping[str, Any]) -> BridgeSettings:
    _warn_unknown(data, {"port", "daemon_url", "max_event_age", "url"}, "bridge.")
    d = BridgeSettings()
    port = data.get("port", d.port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SettingsError(f"bridge.port: expected a port number 1-65535, got {port!r}")
    url = data.get("daemon_url", d.daemon_url)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise SettingsError(f"bridge.daemon_url: expected an http(s) URL, got {url!r}")
    age = data.get("max_event_age")
    max_age = d.max_event_age if age is None else parse_duration(age, "bridge.max_event_age")
    bridge_url = data.get("url", d.url)
    if not isinstance(bridge_url, str) or (
        bridge_url and not bridge_url.startswith(("http://", "https://"))
    ):
        raise SettingsError(f'bridge.url: expected an http(s) URL or "", got {bridge_url!r}')
    return BridgeSettings(
        port=port,
        daemon_url=url.rstrip("/"),
        max_event_age=max_age,
        url=bridge_url.rstrip("/"),
    )


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


def _int(data: Mapping[str, Any], name: str, default: int, key: str) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError(f"{key}: expected a whole number, got {value!r}")
    return value


def _detector(data: Mapping[str, Any]) -> DetectorSettings:
    prefix = "presence.detector."
    _warn_unknown(data, {"device", "near_rssi", "far_rssi", "window", "lost_after"}, prefix)
    d = DetectorSettings()
    device = data.get("device", d.device)
    if not isinstance(device, str) or (
        device and not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", device.strip())
    ):
        raise SettingsError(
            f'{prefix}device: expected a Bluetooth address like "AA:BB:CC:DD:EE:FF" or "", '
            f"got {device!r}"
        )
    near = _int(data, "near_rssi", d.near_rssi, f"{prefix}near_rssi")
    far = _int(data, "far_rssi", d.far_rssi, f"{prefix}far_rssi")
    if near <= far:
        raise SettingsError(
            f"{prefix}near_rssi ({near}) must be greater than far_rssi ({far}): "
            "near is the stronger signal"
        )
    window, lost = data.get("window"), data.get("lost_after")
    return DetectorSettings(
        device=device.strip().upper(),
        near_rssi=near,
        far_rssi=far,
        window=d.window if window is None else parse_duration(window, f"{prefix}window"),
        lost_after=d.lost_after if lost is None else parse_duration(lost, f"{prefix}lost_after"),
    )


def _presence(data: Mapping[str, Any]) -> PresenceSettings:
    _warn_unknown(data, {"mode", "sleep_after", "missed_events", "detector"}, "presence.")
    d = PresenceSettings()
    mode = data.get("mode", d.mode)
    if mode not in PRESENCE_MODES:
        raise SettingsError(
            f"presence.mode: expected one of {', '.join(PRESENCE_MODES)}, got {mode!r}"
        )
    after = data.get("sleep_after")
    missed = data.get("missed_events", list(d.missed_events))
    if not isinstance(missed, list) or not all(e in EVENTS for e in missed):
        raise SettingsError(
            f"presence.missed_events: expected a list of {', '.join(EVENTS)}, got {missed!r}"
        )
    return PresenceSettings(
        mode=mode,
        sleep_after=(
            d.sleep_after if after is None else parse_duration(after, "presence.sleep_after")
        ),
        missed_events=tuple(missed),
        detector=_detector(_table(data, "detector", "presence.detector")),
    )


def parse(data: Mapping[str, Any]) -> Settings:
    """Build Settings from parsed TOML. Missing keys take defaults; unknown keys warn."""
    _warn_unknown(
        data, {"enabled", "bridge", "sources", "quiet", "reactions", "escalation", "presence"}, ""
    )
    quiet = _table(data, "quiet", "quiet")
    _warn_unknown(quiet, {"enabled", "schedule", "reactions"}, "quiet.")
    return Settings(
        enabled=_bool(data, "enabled", True, "enabled"),
        bridge=_bridge(_table(data, "bridge", "bridge")),
        sources=_sources(_table(data, "sources", "sources")),
        quiet=_quiet(quiet),
        reactions=_reactions(_table(data, "reactions", "reactions"), "reactions."),
        escalation=_escalation(_table(data, "escalation", "escalation")),
        presence=_presence(_table(data, "presence", "presence")),
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
