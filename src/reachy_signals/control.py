"""CLI control commands: on/off, quiet, source, status, try.

Settings changes edit the file (edit.py); the bridge picks them up by hot reload.
With ``bridge.url`` set the bridge runs on another machine: changes are sent to it
(``POST /settings``) and it edits its own file the same way.
``status`` and ``try`` ask the bridge: the CLI never calls the daemon itself.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from . import edit
from .settings import BridgeSettings, SettingsError, config_path, load

STATUS_TIMEOUT = 8.0  # the bridge asks the daemon, with 2 s timeouts
TRY_TIMEOUT = 60.0  # may wait behind a long gesture, then wake the robot, then play


class BridgeDown(Exception):
    pass


def _bridge() -> BridgeSettings:
    try:
        return load().bridge
    except SettingsError:
        return BridgeSettings()


def bridge_request(path: str, body: Any = None, timeout: float = STATUS_TIMEOUT) -> dict[str, Any]:
    bridge = _bridge()
    url = bridge.client_url
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{url}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise BridgeDown(
                "the running bridge is an older version: restart `reachy-signals serve`"
            ) from e
        try:
            msg = json.loads(e.read()).get("error", "")
        except ValueError:
            msg = ""
        raise BridgeDown(f"bridge answered HTTP {e.code} {msg}".strip()) from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        hint = (
            "is the bridge machine on and on the network?"
            if bridge.remote
            else "start it with `reachy-signals serve`"
        )
        raise BridgeDown(f"bridge not reachable at {url} ({hint})") from e


# --- settings changes ---------------------------------------------------------


def _summary(s: Any) -> dict[str, Any]:
    from .bridge import settings_summary

    return settings_summary(s, config_path())


def _apply(key: str, value: Any, change: Any) -> dict[str, Any] | None:
    """Make one settings change: in the local file, or on a remote bridge. Returns the
    resulting settings summary, or None after printing the error."""
    try:
        if _bridge().remote:
            return bridge_request("/settings", {key: value})["settings"]
        return _summary(edit.update(change))
    except (SettingsError, BridgeDown) as e:
        print(f"error: {e}")
        return None


def set_enabled(value: bool) -> int:
    if _apply("enabled", value, edit.set_enabled(value)) is None:
        return 1
    if value:
        print("Reactions on: the robot reacts to agent events.")
    else:
        print("Reactions off: events are still logged, the robot stays still.")
    return 0


def quiet(value: bool | None) -> int:
    if value is None:
        try:
            s = bridge_request("/status")["settings"] if _bridge().remote else _summary(load())
        except (SettingsError, BridgeDown) as e:
            print(f"error: {e}")
            return 1
    else:
        try:
            change = edit.set_quiet(value)
        except SettingsError as e:  # pragma: no cover - set_quiet takes any bool
            print(f"error: {e}")
            return 1
        s = _apply("quiet", value, change)
        if s is None:
            return 1
    print(f"Quiet mode: {_quiet_line(s['quiet'])}")
    if value is False and s["quiet"]["active"]:
        print("(still on: the current time is inside a quiet.schedule range)")
    return 0


def source(name: str, mode: str) -> int:
    try:
        change = edit.set_source(name, mode)
    except SettingsError as e:
        print(f"error: {e}")
        return 1
    if _apply("source", {"name": name, "mode": mode}, change) is None:
        return 1
    print(f"Source {name}: {mode}")
    return 0


def presence(mode: str | None) -> int:
    if mode is not None:
        try:
            change = edit.set_presence(mode)
        except SettingsError as e:  # pragma: no cover - argparse limits the choices
            print(f"error: {e}")
            return 1
        if _apply("presence", mode, change) is None:
            return 1
        print(f"Presence: {mode}  ({_PRESENCE_HELP[mode]})")
    try:
        st = bridge_request("/status")
        # The bridge picks a changed file up within ~1 s (hot reload): wait for it.
        deadline = time.monotonic() + 3.0
        while mode is not None and (st.get("presence") or {}).get("mode") not in (mode, None):
            if time.monotonic() > deadline:
                break
            time.sleep(0.25)
            st = bridge_request("/status")
    except BridgeDown as e:
        if mode is None:
            print(f"error: {e}")
            return 1
        return 0
    print(f"Now:      {_presence_line(st.get('presence'))}")
    return 0


_PRESENCE_HELP = {
    "off": "presence ignored, the robot reacts as before",
    "auto": "awake while your phone is near, asleep after it has been away a while",
    "awake": "kept awake whatever the phone does",
    "asleep": "kept asleep; missed events get one reaction when it wakes",
}


def _presence_line(p: dict[str, Any] | None) -> str:
    """``p`` is the ``presence`` part of the bridge's /status."""
    if not p:
        return "unknown (the bridge is an older version)"
    if p["mode"] == "off":
        return "off"
    phone = {"near": "phone near", "away": "phone away"}.get(p["phone"], "phone not seen yet")
    parts = [p["mode"], phone, f"robot {p['robot']}"]
    if "sleep_in" in p:
        parts.append(f"sleeps in {p['sleep_in']:.0f}s")
    if p.get("missed"):
        parts.append("missed something")
    return ", ".join(parts)


def _quiet_line(q: dict[str, Any]) -> str:
    """``q`` is the ``quiet`` part of a settings summary."""
    state = "on (manual)" if q["enabled"] else "on (schedule)" if q["active"] else "off"
    sched = f"  schedule {', '.join(q['schedule'])}" if q["schedule"] else ""
    return f"{state}{sched}"


# --- through the bridge ---------------------------------------------------------


def status(as_json: bool = False) -> int:
    file_error = None
    try:
        local = load()
    except SettingsError as e:
        local, file_error = None, str(e)
    remote = local is not None and local.bridge.remote
    try:
        st = bridge_request("/status")
    except BridgeDown as e:
        if as_json:
            print(json.dumps({"ok": False, "error": str(e), "settings_error": file_error}))
            return 1
        print(f"bridge:    {e}")
        if local is not None and not remote:
            print(f"reactions: {'on' if local.enabled else 'off'}  (from the settings file)")
            print(f"quiet:     {_quiet_line(_summary(local)['quiet'])}")
            print(f"sources:   {_sources_line(local.sources)}")
        _print_settings_file(file_error)
        return 1
    if as_json:
        print(json.dumps({**st, "settings_error": file_error}, indent=2))
        return 0
    s = st["settings"]
    counts = ", ".join(f"{k} {v}" for k, v in sorted(st.get("events", {}).items())) or "none yet"
    where = f"  at {local.bridge.url}" if remote else ""
    print(f"bridge:    running since {st['started_at'][11:19]} UTC{where}  (events: {counts})")
    print(f"reactions: {'on' if s['enabled'] else 'off  (turn on: reachy-signals on)'}")
    print(f"quiet:     {_quiet_line(s['quiet'])}")
    print(f"sources:   {_sources_line(s['sources'])}")
    print(f"presence:  {_presence_line(st.get('presence'))}")
    print(f"robot:     {_robot_line(st['robot'], st.get('presence'))}")
    if st.get("playing"):
        p = st["playing"]
        print(f"playing:   {p['move']} ({p['kind']})")
    blocked = st.get("blocked_sessions") or []
    if blocked:
        print(f"blocked:   {len(blocked)} session(s)")
        for b in blocked:
            esc = ", reminded" if b["escalated"] else ""
            print(
                f"           {b['cwd'] or b['session']}  {b['seconds']:.0f}s ({b['detail']}{esc})"
            )
    else:
        print("blocked:   none")
    if remote:
        print(f"settings:  {s['path']}  (on the bridge machine)")
    _print_settings_file(file_error)
    return 0


def _sources_line(sources: dict[str, str]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sources.items())


def _robot_line(r: dict[str, Any], presence: dict[str, Any] | None = None) -> str:
    if not r.get("reachable"):
        return f"daemon not reachable at {r['daemon_url']}"
    parts = []
    if r.get("simulation"):
        parts.append("simulation")
    motors = r.get("motors")
    if motors == "enabled":
        parts.append("awake")
    elif presence and presence.get("mode") != "off" and presence.get("robot") == "asleep":
        parts.append("asleep (presence)")
    else:
        parts.append(f"motors {motors} (the next reaction wakes it)")
    if r.get("lock") == "free":
        parts.append("free")
    else:
        parts.append(f"held by {r.get('lock_holder') or r.get('lock')} (reactions skipped)")
    return ", ".join(parts) + f"  daemon {r.get('version')}"


def _print_settings_file(error: str | None) -> None:
    path = config_path()
    label = "client:   " if _bridge().remote else "settings: "
    print(f"{label} {path}{'' if path.exists() else '  (not created yet: defaults)'}")
    if error:
        print(f"           error: {error}")
        print("           the bridge keeps using the last valid settings")


def try_reaction(kind: str) -> int:
    print(f"Asking the bridge to play the {kind} reaction...", flush=True)
    try:
        r = bridge_request("/try", {"event": kind}, timeout=TRY_TIMEOUT)
    except BridgeDown as e:
        print(f"error: {e}")
        return 1
    outcome = r.get("outcome")
    move = r.get("move")
    notes = []
    if r.get("quiet"):
        notes.append("quiet reactions")
    if r.get("woke"):
        notes.append("woke the robot first")
    if r.get("enabled") is False:
        notes.append("reactions are off for real events")
    note = f"  ({', '.join(notes)})" if notes else ""
    if outcome in ("played", "interrupted"):
        print(f"{outcome}: {move} in {r.get('seconds')} s{note}")
        return 0
    if outcome == "no-reaction":
        print(f"no reaction configured for {kind}{note}")
        return 0
    detail = r.get("holder") or r.get("error") or ""
    print(f"{outcome}{f': {move}' if move else ''}{f' ({detail})' if detail else ''}{note}")
    return 1
