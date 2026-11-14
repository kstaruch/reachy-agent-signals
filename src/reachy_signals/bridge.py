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
from .watch import SettingsWatcher

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
MAX_BODY = 64_000


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
        self._lock = threading.Lock()

    def handle(self, event: Event) -> str:
        decision = policy.drop_or_pass(self.watcher.current(), event)
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
        else:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/events":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        # Requiring a JSON content type means a web page can't post here without a
        # CORS preflight, which this server never answers.
        if self.headers.get_content_type() != "application/json":
            self._reply(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "expected application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"})
            return
        if length > MAX_BODY:
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
            self.close_connection = True
            return
        try:
            event = Event.from_dict(json.loads(self.rfile.read(length)))
        except (ValueError, EventError) as e:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            return
        decision = self.server.bridge.handle(event)
        self._reply(HTTPStatus.ACCEPTED, {"decision": decision})

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
