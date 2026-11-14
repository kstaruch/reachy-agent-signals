"""CLI control commands: on/off, quiet, source, status, try.

Settings changes edit the file (edit.py); the bridge picks them up by hot reload.
``status`` and ``try`` ask the bridge: the CLI never calls the daemon itself.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from . import edit
from .settings import Settings, SettingsError, config_path, load, quiet_active

STATUS_TIMEOUT = 8.0  # the bridge asks the daemon, with 2 s timeouts
TRY_TIMEOUT = 60.0  # may wait behind a long gesture, then wake the robot, then play


class BridgeDown(Exception):
    pass


def _port() -> int:
    try:
        return load().bridge.port
    except SettingsError:
        return 8765


def bridge_request(path: str, body: Any = None, timeout: float = STATUS_TIMEOUT) -> dict[str, Any]:
    port = _port()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
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
        raise BridgeDown(
            f"bridge not running on 127.0.0.1:{port} (start it with `reachy-signals serve`)"
        ) from e


# --- settings changes ---------------------------------------------------------


def _apply(change: Any) -> Settings | None:
    try:
        return edit.update(change)
    except SettingsError as e:
        print(f"error: {e}")
        return None


def set_enabled(value: bool) -> int:
    if _apply(edit.set_enabled(value)) is None:
        return 1
    if value:
        print("Reactions on: the robot reacts to agent events.")
    else:
        print("Reactions off: events are still logged, the robot stays still.")
    return 0


def quiet(value: bool | None) -> int:
    if value is None:
        try:
            s = load()
        except SettingsError as e:
            print(f"error: {e}")
            return 1
    else:
        s = _apply(edit.set_quiet(value))
        if s is None:
            return 1
    print(f"Quiet mode: {_quiet_line(s)}")
    if value is False and quiet_active(s):
        print("(still on: the current time is inside a quiet.schedule range)")
    return 0


def source(name: str, mode: str) -> int:
    s = _apply(edit.set_source(name, mode))
    if s is None:
        return 1
    print(f"Source {name}: {mode}")
    return 0


def _quiet_line(s: Settings) -> str:
    sched = ", ".join(f"{r.start:%H:%M}-{r.end:%H:%M}" for r in s.quiet.schedule)
    if s.quiet.enabled:
        state = "on (manual)"
    elif quiet_active(s, datetime.now()):
        state = "on (schedule)"
    else:
        state = "off"
    return f"{state}{f'  schedule {sched}' if sched else ''}"


# --- through the bridge ---------------------------------------------------------


def status(as_json: bool = False) -> int:
    file_error = None
    try:
        local = load()
    except SettingsError as e:
        local, file_error = None, str(e)
    try:
        st = bridge_request("/status")
    except BridgeDown as e:
        if as_json:
            print(json.dumps({"ok": False, "error": str(e), "settings_error": file_error}))
            return 1
        print(f"bridge:    {e}")
        if local is not None:
            print(f"reactions: {'on' if local.enabled else 'off'}  (from the settings file)")
            print(f"quiet:     {_quiet_line(local)}")
            print(f"sources:   {_sources_line(local.sources)}")
        _print_settings_file(file_error)
        return 1
    if as_json:
        print(json.dumps({**st, "settings_error": file_error}, indent=2))
        return 0
    s = st["settings"]
    counts = ", ".join(f"{k} {v}" for k, v in sorted(st.get("events", {}).items())) or "none yet"
    print(f"bridge:    running since {st['started_at'][11:19]} UTC  (events: {counts})")
    print(f"reactions: {'on' if s['enabled'] else 'off  (turn on: reachy-signals on)'}")
    q = s["quiet"]
    state = "on (manual)" if q["enabled"] else "on (schedule)" if q["active"] else "off"
    sched = f"  schedule {', '.join(q['schedule'])}" if q["schedule"] else ""
    print(f"quiet:     {state}{sched}")
    print(f"sources:   {_sources_line(s['sources'])}")
    print(f"robot:     {_robot_line(st['robot'])}")
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
    _print_settings_file(file_error)
    return 0


def _sources_line(sources: dict[str, str]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sources.items())


def _robot_line(r: dict[str, Any]) -> str:
    if not r.get("reachable"):
        return f"daemon not reachable at {r['daemon_url']}"
    parts = []
    if r.get("simulation"):
        parts.append("simulation")
    motors = r.get("motors")
    if motors == "enabled":
        parts.append("awake")
    else:
        parts.append(f"motors {motors} (the next reaction wakes it)")
    if r.get("lock") == "free":
        parts.append("free")
    else:
        parts.append(f"held by {r.get('lock_holder') or r.get('lock')} (reactions skipped)")
    return ", ".join(parts) + f"  daemon {r.get('version')}"


def _print_settings_file(error: str | None) -> None:
    path = config_path()
    print(f"settings:  {path}{'' if path.exists() else '  (not created yet: defaults)'}")
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
