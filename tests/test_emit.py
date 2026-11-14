import json
import socket
import subprocess
import sys
import time

import pytest

from reachy_signals.cli import main

HOOK = {
    "session_id": "sess-1",
    "cwd": "/work/proj",
    "hook_event_name": "Stop",
    "stop_hook_active": False,
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def use_port(cfg, port):
    cfg.write_text(f"[bridge]\nport = {port}\n", encoding="utf-8")


def run_emit(args, stdin, cfg, timeout=10):
    """Run the real CLI in a subprocess, as a hook would."""
    env_cfg = {"REACHY_SIGNALS_CONFIG": str(cfg), "PYTHONUTF8": "1"}
    import os

    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "reachy_signals", "emit", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **env_cfg},
    )
    return proc, time.monotonic() - start


def test_emit_reaches_bridge(bridge_server):
    bridge, port, cfg = bridge_server
    use_port(cfg, port)
    proc, _ = run_emit(["done"], json.dumps(HOOK), cfg)
    assert proc.returncode == 0, proc.stderr
    (rec,) = bridge.event_log.tail()
    assert (rec["event"], rec["session"], rec["cwd"]) == ("done", "sess-1", "/work/proj")
    assert rec["hook"]["hook_event_name"] == "Stop"


def test_bridge_down_exits_0_fast(tmp_path):
    cfg = tmp_path / "config.toml"
    use_port(cfg, free_port())  # nothing listening
    proc, elapsed = run_emit(["blocked"], json.dumps(HOOK), cfg)
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""
    assert elapsed < 3  # includes interpreter start; the post itself times out in 0.5 s


@pytest.mark.parametrize(
    "args",
    [["done"], ["not-an-event"], [], ["done", "--bogus"]],
)
def test_bad_input_still_exits_0(tmp_path, args):
    cfg = tmp_path / "config.toml"
    use_port(cfg, free_port())
    proc, _ = run_emit(args, "{{{ not json", cfg)
    assert proc.returncode == 0


def test_invalid_settings_still_exits_0(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("enabled = = broken", encoding="utf-8")
    assert run_emit(["done"], json.dumps(HOOK), cfg)[0].returncode == 0


def test_garbage_stdin_still_sends_event(bridge_server):
    bridge, port, cfg = bridge_server
    use_port(cfg, port)
    assert run_emit(["started"], "not json at all", cfg)[0].returncode == 0
    (rec,) = bridge.event_log.tail()
    assert rec["event"] == "started" and rec["session"] == ""


def test_manual_emit_without_stdin(bridge_server):
    bridge, port, cfg = bridge_server
    use_port(cfg, port)
    args = ["done", "--no-stdin", "--source", "herdr", "--session", "p1", "--cwd", "/x"]
    assert main(["emit", *args]) == 0
    (rec,) = bridge.event_log.tail()
    assert (rec["source"], rec["session"], rec["cwd"]) == ("herdr", "p1", "/x")
