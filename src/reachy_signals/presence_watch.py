"""``reachy-signals presence-watch``: is the owner's phone at the desk? (docs/presence.md)

Runs on the bridge host (Linux, BlueZ). Reads ``bluetoothctl``'s LE scan output
rather than talking D-Bus, so there's no new dependency (D9). The paired phone's
broadcasts are reported under its fixed address: BlueZ resolves its rotating one.

:class:`Detector` is the pure, clock-driven part; :func:`run` wires it to
``bluetoothctl`` and posts ``near`` / ``away`` to the bridge.
"""

from __future__ import annotations

import logging
import queue
import re
import shutil
import statistics
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .events import Event
from .settings import DetectorSettings

log = logging.getLogger(__name__)

MIN_READINGS = 3  # fewer than this in the window → no median, no decision from signal
HEARTBEAT = 60.0  # re-send the current state this often (a restarted bridge learns it)
QUIET_RESTART = 60.0  # no output at all from bluetoothctl this long → restart it
SCAN_COMMANDS = ("menu scan", "transport le", "duplicate-data on", "back", "scan on")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CHG = re.compile(r"\[(?:CHG|NEW)\] Device ([0-9A-F:]{17}) (.*)")
_RSSI = re.compile(r"RSSI: \S+ \((-?\d+)\)|RSSI: (-?\d+)")


def parse_line(line: str, device: str) -> tuple[bool, int | None]:
    """(heard, rssi) for one line of bluetoothctl output about ``device``."""
    m = _CHG.search(_ANSI.sub("", line))
    if not m or m.group(1) != device:
        return False, None
    r = _RSSI.search(m.group(2))
    if not r:
        return True, None
    return True, int(r.group(1) or r.group(2))


@dataclass
class Detector:
    cfg: DetectorSettings
    started: float
    state: str | None = None  # near | away | None (not decided yet)
    last_heard: float | None = None
    readings: deque[tuple[float, int]] = field(default_factory=deque)

    def heard(self, now: float, rssi: int | None = None) -> None:
        self.last_heard = now
        if rssi is not None:
            self.readings.append((now, rssi))

    def median(self, now: float) -> float | None:
        while self.readings and now - self.readings[0][0] > self.cfg.window:
            self.readings.popleft()
        if len(self.readings) < MIN_READINGS:
            return None
        return statistics.median(r for _, r in self.readings)

    def decide(self, now: float) -> str | None:
        """What the state should be now; the current state when nothing decides."""
        silent_since = self.last_heard if self.last_heard is not None else self.started
        if now - silent_since >= self.cfg.lost_after:
            return "away"
        m = self.median(now)
        if m is None:
            return self.state
        if m >= self.cfg.near_rssi:
            return "near"
        if m <= self.cfg.far_rssi:
            return "away"
        return self.state

    def update(self, now: float) -> str | None:
        """The new state if it changed, else None."""
        new = self.decide(now)
        if new is None or new == self.state:
            return None
        self.state = new
        return new


# --- running it -----------------------------------------------------------------


class _Scanner:
    """A ``bluetoothctl`` process running an LE scan; lines arrive on ``lines``."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert self.proc.stdin is not None
        for c in SCAN_COMMANDS:
            self.proc.stdin.write(c + "\n")
        self.proc.stdin.flush()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.lines.put(line)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None and self.proc.stdin:
                self.proc.stdin.write("scan off\nquit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc = None


def _post(state: str) -> bool:
    from .control import BridgeDown, bridge_request

    event = Event(source="presence", event=state, session="presence", detail="ble")
    try:
        bridge_request("/events", event.to_dict(), timeout=3.0)
        return True
    except BridgeDown as e:
        log.warning("can't tell the bridge the phone is %s: %s", state, e)
        return False


def run(config: Path, calibrate: bool = False) -> int:
    """Watch until Ctrl+C. With ``calibrate``, print readings and post nothing."""
    from .settings import SettingsError
    from .watch import SettingsWatcher

    if shutil.which("bluetoothctl") is None:
        log.error("bluetoothctl not found: presence-watch needs Linux with BlueZ")
        return 1
    try:
        watcher = SettingsWatcher(config)
    except SettingsError as e:
        log.error("invalid settings: %s", e)
        return 1
    cfg = watcher.current().presence.detector
    if not cfg.device:
        log.error("set [presence.detector] device to the paired phone's address (%s)", config)
        return 1

    det = Detector(cfg, started=time.monotonic())
    scanner = _Scanner()
    last_post, last_line, last_print = 0.0, time.monotonic(), 0.0
    unsent: str | None = None
    log.info(
        "watching %s: near >= %d dBm, away <= %d dBm, median over %.0fs, lost after %.0fs",
        cfg.device[:8] + ":…",
        cfg.near_rssi,
        cfg.far_rssi,
        cfg.window,
        cfg.lost_after,
    )
    with watcher:
        try:
            while True:
                now = time.monotonic()
                if not scanner.alive():
                    if scanner.proc is not None:
                        log.warning("bluetoothctl exited; restarting in 5 s")
                        scanner.stop()
                        time.sleep(5)
                    scanner.start()
                    last_line = time.monotonic()
                try:
                    line = scanner.lines.get(timeout=1.0)
                except queue.Empty:
                    line = None
                now = time.monotonic()
                if line is not None:
                    last_line = now
                    heard, rssi = parse_line(line, cfg.device)
                    if heard:
                        det.heard(now, rssi)
                    while True:  # drain what's queued
                        try:
                            more = scanner.lines.get_nowait()
                        except queue.Empty:
                            break
                        heard, rssi = parse_line(more, cfg.device)
                        if heard:
                            det.heard(now, rssi)
                elif now - last_line > QUIET_RESTART:
                    log.warning("no scan output for %.0f s; restarting bluetoothctl", QUIET_RESTART)
                    scanner.stop()
                    continue

                new_cfg = watcher.current().presence.detector
                if new_cfg != det.cfg:
                    log.info("detector settings changed")
                    det.cfg = new_cfg
                changed = det.update(now)
                if calibrate:
                    if now - last_print >= 1.0:
                        _print_calibration(det, now)
                        last_print = now
                    continue
                if changed:
                    log.info("phone %s", changed)
                    unsent = changed
                if det.state and (unsent or now - last_post >= HEARTBEAT):
                    if _post(det.state):
                        unsent = None
                    last_post = now
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            scanner.stop()
    return 0


def _print_calibration(det: Detector, now: float) -> None:
    last = det.readings[-1][1] if det.readings else None
    m = det.median(now)
    heard = "never" if det.last_heard is None else f"{now - det.last_heard:4.1f}s ago"
    print(
        f"last {last if last is not None else '—':>4}  "
        f"median {f'{m:.0f}' if m is not None else '—':>4}  "
        f"n {len(det.readings):2d}  heard {heard:>10}  "
        f"→ {det.state or 'undecided'}  "
        f"(near ≥ {det.cfg.near_rssi}, away ≤ {det.cfg.far_rssi})",
        flush=True,
    )
