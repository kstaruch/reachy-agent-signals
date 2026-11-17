"""Control commands end to end: CLI → settings file / bridge → reactor → fake daemon."""

import json
import random
import socket
import threading
import time

import pytest

from reachy_signals import policy
from reachy_signals.bridge import Bridge, make_server
from reachy_signals.cli import main
from reachy_signals.eventlog import EventLog
from reachy_signals.events import Event
from reachy_signals.reactor import Reactor
from reachy_signals.settings import load
from reachy_signals.watch import SettingsWatcher


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(fake_daemon, tmp_path, monkeypatch):
    """Bridge + reactor + watcher thread, wired to the fake daemon like `serve` does."""
    fake, url = fake_daemon
    port = free_port()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'# my comment\nenabled = true\n\n[bridge]\nport = {port}\ndaemon_url = "{url}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("REACHY_SIGNALS_CONFIG", str(cfg))
    watcher = SettingsWatcher(cfg, interval=0.1)
    log = EventLog(tmp_path / "logs" / "events.jsonl")
    reactor = Reactor(watcher.current, log, rng=random.Random(1))
    bridge = Bridge(watcher, log, reactor)
    server = make_server(bridge, port)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    reactor.start()
    watcher.start()
    try:
        yield fake, bridge, cfg
    finally:
        watcher.stop()
        reactor.stop()
        server.shutdown()
        server.server_close()
        thread.join()


def until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_off_on_reaches_running_bridge(live, capsys):
    _, bridge, cfg = live
    assert main(["off"]) == 0
    assert "Reactions off" in capsys.readouterr().out
    assert until(lambda: bridge.watcher.current().enabled is False, timeout=1.5)
    ev = Event(source="claude-code", event="done", session="s")
    assert bridge.handle(ev) == policy.DISABLED
    assert main(["on"]) == 0
    assert until(lambda: bridge.watcher.current().enabled is True, timeout=1.5)
    assert "# my comment" in cfg.read_text(encoding="utf-8")


def test_quiet_and_source(live, capsys):
    _, bridge, _ = live
    assert main(["quiet", "on"]) == 0
    assert main(["source", "herdr", "shadow"]) == 0
    assert until(lambda: bridge.watcher.current().quiet.enabled, timeout=1.5)
    assert until(lambda: bridge.watcher.current().source_mode("herdr") == "shadow")
    capsys.readouterr()
    assert main(["quiet"]) == 0
    assert "Quiet mode: on (manual)" in capsys.readouterr().out


def test_quiet_off_inside_schedule_says_still_on(tmp_path, capsys):
    cfg = tmp_path / "config.toml"  # isolated_paths points the CLI here
    cfg.write_text('[quiet]\nschedule = ["00:00-23:59"]\n', encoding="utf-8")
    assert main(["quiet", "off"]) == 0
    assert "still on" in capsys.readouterr().out


def test_invalid_file_is_left_alone(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text("enabled = = broken", encoding="utf-8")
    assert main(["off"]) == 1
    assert "fix it by hand" in capsys.readouterr().out
    assert cfg.read_text(encoding="utf-8") == "enabled = = broken"


def test_status_through_bridge(live, capsys):
    fake, _, _ = live
    fake.motor_mode = "disabled"
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "bridge:    running" in out
    assert "reactions: on" in out
    assert "motors disabled (the next reaction wakes it)" in out
    assert "blocked:   none" in out


def test_status_json_reports_blocked_and_lock(live, capsys):
    fake, bridge, _ = live
    fake.lock = {"state": "local_app", "holder_name": "conversation"}
    bridge.handle(Event(source="claude-code", event="blocked", session="w", cwd="/p"))
    assert until(lambda: bridge.reactor.status()["blocked_sessions"])
    assert main(["status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["robot"]["lock"] == "local_app" and st["robot"]["lock_holder"] == "conversation"
    assert [b["cwd"] for b in st["blocked_sessions"]] == ["/p"]


def test_status_bridge_down(tmp_path, capsys):
    (tmp_path / "config.toml").write_text(f"[bridge]\nport = {free_port()}\n", encoding="utf-8")
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert "bridge not reachable" in out and "reactions: on  (from the settings file)" in out


def test_try_plays_and_reports(live, capsys):
    fake, _, _ = live
    assert main(["try", "done"]) == 0
    assert "played: cheerful1" in capsys.readouterr().out
    assert fake.played == ["cheerful1"]


def test_try_escalation_and_quiet(live, capsys):
    fake, bridge, cfg = live
    with cfg.open("a", encoding="utf-8") as f:
        f.write('\n[quiet]\nenabled = true\n[quiet.reactions]\nblocked = ["simple_nod"]\n')
    assert until(lambda: bridge.watcher.current().quiet.enabled)
    assert main(["try", "blocked"]) == 0
    assert "simple_nod" in capsys.readouterr().out
    assert main(["try", "escalation"]) == 0
    assert fake.played == ["simple_nod", "toc-toc-toc"]


def test_try_works_with_reactions_off_but_not_when_locked(live, capsys):
    fake, bridge, _ = live
    main(["off"])
    assert until(lambda: bridge.watcher.current().enabled is False)
    capsys.readouterr()
    assert main(["try", "started"]) == 0
    assert "reactions are off for real events" in capsys.readouterr().out
    fake.lock = {"state": "remote_session", "holder_name": None}
    assert main(["try", "started"]) == 1
    assert "locked" in capsys.readouterr().out
    assert fake.played == ["yeah_nod"]


def test_try_leaves_no_blocked_session(live):
    _, bridge, _ = live
    main(["try", "blocked"])
    assert bridge.reactor.status()["blocked_sessions"] == []


def test_try_bridge_down(tmp_path, capsys):
    (tmp_path / "config.toml").write_text(f"[bridge]\nport = {free_port()}\n", encoding="utf-8")
    assert main(["try", "done"]) == 1
    assert "bridge not reachable" in capsys.readouterr().out


def test_settings_file_round_trip_through_cli(live):
    _, _, cfg = live
    main(["off"])
    main(["quiet", "on"])
    main(["source", "claude-code", "shadow"])
    s = load(cfg)
    assert (s.enabled, s.quiet.enabled, s.source_mode("claude-code")) == (False, True, "shadow")
    assert cfg.read_text(encoding="utf-8").startswith("# my comment")
