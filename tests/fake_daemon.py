"""In-process fake of the Reachy Mini daemon 1.14.0 REST API, with the quirks we measured.

- A play while a move runs gets a uuid and is silently dropped.
- Plays are accepted while motors aren't enabled (``played_while_disabled`` records it).
- ``/move/stop`` needs a uuid body; an unknown uuid answers 500.
Moves last ``move_seconds`` so tests stay fast.
"""

from __future__ import annotations

import json
import re
import socketserver
import threading
import time
import urllib.parse
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIBRARIES = {
    "pollen-robotics/reachy-mini-emotions-library": {"cheerful1", "attentive1", "toc-toc-toc"},
    "pollen-robotics/reachy-mini-dances-library": {"yeah_nod", "simple_nod"},
}


class FakeDaemon:
    def __init__(self, move_seconds: float = 0.3) -> None:
        self.move_seconds = move_seconds
        self.motor_mode = "enabled"
        self.lock = {"state": "free", "holder_name": None}
        self.lock_ = threading.Lock()
        self.current: tuple[str, float] | None = None  # (uuid, ends_at)
        self.played: list[str] = []  # move names that actually started
        self.dropped: list[str] = []  # accepted but dropped because busy
        self.played_while_disabled: list[str] = []
        self.requests: list[str] = []

    def running(self) -> list[str]:
        with self.lock_:
            if self.current and time.monotonic() >= self.current[1]:
                self.current = None
            return [self.current[0]] if self.current else []

    def start(self, name: str, seconds: float | None = None) -> str:
        uid = str(uuidlib.uuid4())
        if self.running():
            self.dropped.append(name)
            return uid
        with self.lock_:
            self.current = (uid, time.monotonic() + (seconds or self.move_seconds))
        self.played.append(name)
        if self.motor_mode != "enabled":
            self.played_while_disabled.append(name)
        return uid


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        d = self.server.fake
        d.requests.append(f"GET {self.path}")
        if self.path == "/api/daemon/status":
            self._send(
                200,
                {
                    "state": "running",
                    "version": "1.14.0",
                    "mockup_sim_enabled": True,
                    "backend_status": {"motor_control_mode": d.motor_mode, "error": None},
                },
            )
        elif self.path == "/api/motors/status":
            self._send(200, {"mode": d.motor_mode})
        elif self.path == "/api/daemon/robot-app-lock-status":
            self._send(200, d.lock)
        elif self.path == "/api/move/running":
            self._send(200, [{"uuid": u} for u in d.running()])
        elif m := re.fullmatch(r"/api/move/recorded-move-datasets/list/(.+)", self.path):
            lib = LIBRARIES.get(urllib.parse.unquote(m.group(1)))
            self._send(200, sorted(lib)) if lib else self._send(404, {"detail": "not found"})
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        d = self.server.fake
        d.requests.append(f"POST {self.path}")
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        if m := re.fullmatch(r"/api/move/play/recorded-move-dataset/([^/]+)/([^/]+)", self.path):
            dataset, move = (urllib.parse.unquote(g) for g in m.groups())
            if move not in LIBRARIES.get(dataset, set()):
                self._send(404, {"detail": f"Move {move} not found in recorded moves library"})
            else:
                self._send(200, {"uuid": d.start(move)})
        elif self.path in ("/api/move/play/wake_up", "/api/move/play/goto_sleep"):
            self._send(200, {"uuid": d.start(self.path.rsplit("/", 1)[1], seconds=0.1)})
        elif m := re.fullmatch(r"/api/motors/set_mode/(\w+)", self.path):
            if m.group(1) not in ("enabled", "disabled", "gravity_compensation"):
                self._send(422, {"detail": "bad mode"})
            else:
                d.motor_mode = m.group(1)
                self._send(200, {"status": "ok"})
        elif self.path == "/api/move/stop":
            if not body or "uuid" not in body:
                self._send(422, {"detail": [{"msg": "Field required"}]})
            elif body["uuid"] in d.running():
                with d.lock_:
                    d.current = None
                self._send(200, {"message": f"Stopped move with UUID: {body['uuid']}"})
            else:
                self.send_response(500)
                self.send_header("Content-Length", "21")
                self.end_headers()
                self.wfile.write(b"Internal Server Error")
        else:
            self._send(404, {"detail": "Not Found"})

    def log_message(self, *args: object) -> None:
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fake: FakeDaemon) -> None:
        self.fake = fake
        super().__init__(("127.0.0.1", 0), _Handler)

    def server_bind(self) -> None:
        # Skip HTTPServer's reverse-DNS lookup (35 s on GitHub's macOS runners).
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]
