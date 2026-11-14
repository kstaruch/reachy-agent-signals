"""Reachy Mini daemon client over stdlib urllib (D9). Only the bridge uses this.

Behaviour verified against daemon 1.14.0 (docs/reachy-daemon.md):
- A play request while another move runs is accepted, gets a uuid, and is then
  silently dropped. Always wait for idle before playing (:meth:`play_when_idle`).
- With motors not ``enabled`` a play is accepted and does nothing on hardware.
  Call :meth:`ensure_ready` first.
- ``POST /move/stop`` stops one move by uuid; an unknown uuid answers 500.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

EMOTIONS = "pollen-robotics/reachy-mini-emotions-library"
DANCES = "pollen-robotics/reachy-mini-dances-library"
DATASETS = (EMOTIONS, DANCES)  # lookup order for a bare move name

DEFAULT_TIMEOUT = 3.0
SLOW_TIMEOUT = 60.0  # first use of a dataset downloads it from Hugging Face


class DaemonError(Exception):
    """The daemon answered with an error or something unexpected."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DaemonUnavailable(DaemonError):
    """No daemon reachable at the configured URL (not running, or timed out)."""


class MoveNotFound(DaemonError):
    """The move name isn't in any of the libraries."""


@dataclass(frozen=True)
class LockStatus:
    state: str  # free | local_app | remote_session
    holder: str | None = None

    @property
    def free(self) -> bool:
        return self.state == "free"


class DaemonClient:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._moves: dict[str, frozenset[str]] = {}

    # --- HTTP ---------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: Any = None, timeout: float | None = None
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise DaemonError(f"{method} {path}: HTTP {e.code}: {_detail(e)}", status=e.code) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            reason = getattr(e, "reason", e)
            raise DaemonUnavailable(f"daemon not reachable at {self.base_url}: {reason}") from e
        try:
            return json.loads(raw) if raw else None
        except ValueError as e:
            raise DaemonError(f"{method} {path}: response is not JSON") from e

    # --- state --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/daemon/status")

    def motor_mode(self) -> str:
        """``enabled``, ``disabled`` or ``gravity_compensation``."""
        return self._request("GET", "/motors/status")["mode"]

    def lock_status(self) -> LockStatus:
        d = self._request("GET", "/daemon/robot-app-lock-status")
        return LockStatus(state=d["state"], holder=d.get("holder_name"))

    def running(self) -> list[str]:
        """UUIDs of moves currently playing."""
        return [m["uuid"] for m in self._request("GET", "/move/running")]

    # --- moves --------------------------------------------------------------

    def list_moves(self, dataset: str) -> frozenset[str]:
        if dataset not in self._moves:
            path = f"/move/recorded-move-datasets/list/{_q(dataset)}"
            self._moves[dataset] = frozenset(self._request("GET", path, timeout=SLOW_TIMEOUT))
        return self._moves[dataset]

    def resolve(self, move: str) -> str:
        """The dataset a bare move name belongs to (emotions first, then dances)."""
        for dataset in DATASETS:
            if move in self.list_moves(dataset):
                return dataset
        raise MoveNotFound(f"move {move!r} is in neither library")

    def unknown_moves(self, names: Iterable[str]) -> list[str]:
        """Names from settings that aren't in any library, for a startup warning."""
        known = frozenset().union(*(self.list_moves(d) for d in DATASETS))
        return sorted({n for n in names if n not in known})

    def play(self, move: str, dataset: str | None = None) -> str:
        """Start a library move and return its uuid. Doesn't wait or check readiness.

        If another move is running the daemon drops this one silently: use
        :meth:`play_when_idle` unless you've just checked.
        """
        dataset = dataset or self.resolve(move)
        path = f"/move/play/recorded-move-dataset/{_q(dataset)}/{_q(move)}"
        try:
            return self._request("POST", path, timeout=SLOW_TIMEOUT)["uuid"]
        except DaemonError as e:
            if e.status == 404:
                raise MoveNotFound(f"move {move!r} not found in {dataset}", status=404) from e
            raise

    def play_when_idle(self, move: str, idle_timeout: float = 30.0) -> str:
        if not self.wait_idle(idle_timeout):
            raise DaemonError(f"robot still busy after {idle_timeout:.0f}s; not playing {move!r}")
        return self.play(move)

    def wait_idle(self, timeout: float, poll: float = 0.1) -> bool:
        """True once no move is running; False if still busy after ``timeout``."""
        deadline = time.monotonic() + timeout
        while True:
            if not self.running():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def wait_move(self, uuid: str, timeout: float, poll: float = 0.1) -> bool:
        """True once ``uuid`` is no longer running; False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            if uuid not in self.running():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def stop(self, uuid: str) -> bool:
        """Stop one move. False if it had already finished (the daemon answers 500)."""
        try:
            self._request("POST", "/move/stop", body={"uuid": uuid})
        except DaemonError as e:
            if e.status == 500 and uuid not in self.running():
                return False
            raise
        return True

    def stop_all(self) -> int:
        """Stop every running move; returns how many were stopped."""
        return sum(self.stop(u) for u in self.running())

    # --- readiness ----------------------------------------------------------

    def ensure_ready(self, wake_timeout: float = 10.0) -> bool:
        """Enable motors and wake up if needed. Returns True if it had to wake the robot.

        Moves the robot (wake-up pose) when the motors weren't enabled.
        """
        if self.motor_mode() == "enabled":
            return False
        self._request("POST", "/motors/set_mode/enabled")
        uuid = self._request("POST", "/move/play/wake_up")["uuid"]
        if not self.wait_move(uuid, wake_timeout):
            raise DaemonError(f"wake-up didn't finish within {wake_timeout:.0f}s")
        return True

    def goto_sleep(self, timeout: float = 10.0) -> None:
        """Sleep pose (moves the robot). Motors stay as they are."""
        uuid = self._request("POST", "/move/play/goto_sleep")["uuid"]
        self.wait_move(uuid, timeout)


def _q(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def _detail(e: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(e.read() or b"{}")
    except ValueError:
        return e.reason or ""
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return str(detail)[:300]
