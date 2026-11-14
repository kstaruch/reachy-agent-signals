"""The bridge: one process, localhost TCP, receives events from adapters.

Receives events, decides drop/pass, logs them, and hands passing events to the
Reactor, the single thread that moves the robot.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socketserver
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import policy
from .eventlog import EventLog
from .events import Event, EventError, now_iso
from .reactor import Reactor
from .settings import quiet_active
from .watch import SettingsWatcher

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
MAX_BODY = 64_000
# The plugin's slash command. Its turn (started, then done) doesn't move the robot:
# `/reachy status` shouldn't nod and cheer.
CONTROL_COMMANDS = ("/reachy", "/reachy-signals:reachy")
TRY_KINDS = ("started", "done", "blocked", "escalation")


class Bridge:
    """Bridge state, independent of HTTP so it can be tested directly."""

    def __init__(
        self, watcher: SettingsWatcher, event_log: EventLog, reactor: Reactor | None = None
    ) -> None:
        self.watcher = watcher
        self.event_log = event_log
        self.reactor = reactor
        self.started_at = now_iso()
        self._counts: dict[str, int] = {}
        self._control_turns: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def handle(self, event: Event) -> str:
        decision = self._control_turn(event, policy.drop_or_pass(self.watcher.current(), event))
        self.event_log.append({"received": now_iso(), **event.to_dict(), "decision": decision})
        with self._lock:
            self._counts[decision] = self._counts.get(decision, 0) + 1
        if self.reactor is not None:
            self.reactor.submit(event, decision)
        log.log(
            logging.DEBUG if decision == policy.OBSERVE else logging.INFO,
            "%s %s %s session=%s -> %s",
            event.source,
            event.event,
            event.detail,
            event.session[:8],
            decision,
        )
        return decision

    def _control_turn(self, event: Event, decision: str) -> str:
        """``started`` from a /reachy command, and the ``done`` that ends its turn → CONTROL."""
        if event.event not in ("started", "done"):
            return decision
        key = (event.source, event.session)
        prompt = event.hook.get("prompt")
        slash = prompt.get("slash") if isinstance(prompt, dict) else None
        with self._lock:
            if event.event == "started":
                if slash in CONTROL_COMMANDS:
                    self._control_turns.add(key)
                    return policy.CONTROL
                self._control_turns.discard(key)
            elif key in self._control_turns:
                self._control_turns.discard(key)
                return policy.CONTROL
        return decision

    def health(self) -> dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
        return {
            "ok": True,
            "started_at": self.started_at,
            "events": counts,
            "log": str(self.event_log.path),
            **(self.reactor.status() if self.reactor else {}),
        }

    def status(self) -> dict[str, Any]:
        """Everything `reachy-signals status` shows: health, settings in effect, robot."""
        s = self.watcher.current()
        return {
            **self.health(),
            "settings": {
                "path": str(self.watcher.path),
                "enabled": s.enabled,
                "quiet": {
                    "enabled": s.quiet.enabled,
                    "active": quiet_active(s),
                    "schedule": [_fmt_range(r) for r in s.quiet.schedule],
                },
                "sources": dict(s.sources),
                "reactions": _reactions_dict(s.quiet.reactions if quiet_active(s) else s.reactions),
            },
            "robot": robot_state(s.bridge.daemon_url),
        }

    def try_reaction(self, kind: str) -> dict[str, Any]:
        if self.reactor is None:
            raise RuntimeError("this bridge has no reactor")
        s = self.watcher.current()
        result = self.reactor.try_reaction(kind)
        return {**result, "quiet": quiet_active(s), "enabled": s.enabled}


def robot_state(daemon_url: str) -> dict[str, Any]:
    """Daemon reachability, motors and app lock. Read-only, short timeouts."""
    from .daemon import DaemonClient, DaemonError

    client = DaemonClient(daemon_url, timeout=2.0)
    try:
        st = client.status()
        lock = client.lock_status()
    except DaemonError as e:
        return {"reachable": False, "daemon_url": daemon_url, "error": str(e)}
    backend = st.get("backend_status") or {}
    return {
        "reachable": True,
        "daemon_url": daemon_url,
        "version": st.get("version"),
        "state": st.get("state"),
        "simulation": bool(st.get("simulation_enabled") or st.get("mockup_sim_enabled")),
        "motors": backend.get("motor_control_mode"),
        "lock": lock.state,
        "lock_holder": lock.holder,
    }


def _fmt_range(r: Any) -> str:
    return f"{r.start:%H:%M}-{r.end:%H:%M}"


def _reactions_dict(r: Any) -> dict[str, list[str]]:
    return {e: list(r.for_event(e)) for e in ("started", "done", "blocked")}


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def _reply(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._reply(HTTPStatus.OK, self.server.bridge.health())
        elif self.path == "/status":
            self._reply(HTTPStatus.OK, self.server.bridge.status())
        else:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/events", "/try"):
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        body = self._json_body()
        if body is None:
            return
        if self.path == "/events":
            try:
                event = Event.from_dict(body)
            except EventError as e:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return
            decision = self.server.bridge.handle(event)
            self._reply(HTTPStatus.ACCEPTED, {"decision": decision})
            return
        kind = body.get("event") if isinstance(body, dict) else None
        if kind not in TRY_KINDS:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": f"event must be one of {TRY_KINDS}"})
            return
        try:
            result = self.server.bridge.try_reaction(kind)
        except RuntimeError as e:
            self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(e)})
            return
        self._reply(HTTPStatus.OK, result)

    def _json_body(self) -> Any:
        """The request's JSON body, or None after replying with an error."""
        # Requiring a JSON content type means a web page can't post here without a
        # CORS preflight, which this server never answers.
        if self.headers.get_content_type() != "application/json":
            self._reply(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "expected application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"})
            return None
        if length > MAX_BODY:
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
            self.close_connection = True
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError as e:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON: {e}"})
            return None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("http: " + format, *args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # POSIX: SO_REUSEADDR only lets a restarted bridge bind past TIME_WAIT; a second
    # live listener still fails. Windows: SO_REUSEADDR would let two processes share
    # the port, so leave it off there.
    allow_reuse_address = os.name != "nt"

    def __init__(self, bridge: Bridge, port: int) -> None:
        self.bridge = bridge
        super().__init__((HOST, port), _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup that took
        # 35 s on GitHub's macOS runners. We only ever bind 127.0.0.1, so skip it.
        socketserver.TCPServer.server_bind(self)
        self.server_name = HOST
        self.server_port = self.server_address[1]


def make_server(bridge: Bridge, port: int) -> _Server:
    """Bind to 127.0.0.1:port (0 = any free port). Raises OSError if the port is taken."""
    return _Server(bridge, port)


def _stop_on_signals() -> None:
    """SIGTERM (and Ctrl+Break on Windows) stop the bridge like Ctrl+C does."""

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handler)


def serve(config: Path, log_path: Path) -> int:
    """Run the bridge until Ctrl+C. Returns a process exit code."""
    from .settings import SettingsError

    try:
        watcher = SettingsWatcher(config)
    except SettingsError as e:
        log.error("invalid settings: %s", e)
        return 1
    port = watcher.current().bridge.port

    def on_change(new: Any) -> None:
        if new.bridge.port != port:
            log.warning("bridge.port changed to %s: restart `serve` to use it", new.bridge.port)

    watcher.on_change = on_change
    event_log = EventLog(log_path)
    reactor = Reactor(watcher.current, event_log)
    bridge = Bridge(watcher, event_log, reactor)
    try:
        server = make_server(bridge, port)
    except OSError as e:
        log.error("can't listen on %s:%s (%s). Is another bridge running?", HOST, port, e)
        return 1
    log.info("bridge listening on http://%s:%s", HOST, port)
    log.info("settings: %s", config)
    log.info("event log: %s", log_path)
    _stop_on_signals()
    threading.Thread(target=_check_moves, args=(watcher,), daemon=True).start()
    reactor.start()
    with watcher:
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            server.server_close()
            reactor.stop()
    return 0


def _check_moves(watcher: SettingsWatcher) -> None:
    """Warn at startup about reaction names that aren't in the move libraries."""
    from .daemon import DaemonClient, DaemonError

    s = watcher.current()
    names = [
        *(n for e in ("started", "done", "blocked") for n in s.reactions.for_event(e)),
        *(n for e in ("started", "done", "blocked") for n in s.quiet.reactions.for_event(e)),
        *s.escalation.blocked,
    ]
    try:
        unknown = DaemonClient(s.bridge.daemon_url).unknown_moves(names)
    except DaemonError as e:
        log.warning("daemon: %s (reactions will be skipped until it is up)", e)
        return
    if unknown:
        log.warning("settings: unknown move names: %s", ", ".join(unknown))
    else:
        log.info("daemon: reachable, all reaction moves found")
