"""Bridge on another machine: clients with `bridge.url` set (D10).

The bridge and the client each get their own settings file, as on two machines.
"""

import json
import socket
import threading
import time

import pytest
from test_bridge import request

from reachy_signals.bridge import Bridge, make_server
from reachy_signals.cli import main
from reachy_signals.eventlog import EventLog
from reachy_signals.settings import BridgeSettings, SettingsError, load, parse
from reachy_signals.watch import SettingsWatcher


def until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def remote(fake_daemon, tmp_path, monkeypatch):
    """A bridge with its own settings file, and a client whose file only names its URL.

    Yields (bridge, bridge_cfg, client_cfg, url).
    """
    _, daemon_url = fake_daemon
    bridge_cfg = tmp_path / "bridge" / "config.toml"
    bridge_cfg.parent.mkdir()
    bridge_cfg.write_text(
        f'# bridge machine\nenabled = true\n\n[bridge]\ndaemon_url = "{daemon_url}"\n',
        encoding="utf-8",
    )
    watcher = SettingsWatcher(bridge_cfg, interval=0.1)  # hot reload, as `serve` runs it
    bridge = Bridge(watcher, EventLog(tmp_path / "logs" / "events.jsonl"))
    server = make_server(bridge, 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    watcher.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    client_cfg = tmp_path / "client" / "config.toml"
    client_cfg.parent.mkdir()
    client_text = f'# client machine\n[bridge]\nurl = "{url}/"\n'
    client_cfg.write_text(client_text, encoding="utf-8")
    monkeypatch.setenv("REACHY_SIGNALS_CONFIG", str(client_cfg))
    try:
        yield bridge, bridge_cfg, client_cfg, url
    finally:
        watcher.stop()
        server.shutdown()
        server.server_close()
        thread.join()
        assert client_cfg.read_text(encoding="utf-8") == client_text  # never edited


# --- settings ----------------------------------------------------------------


def test_url_defaults_to_local_bridge():
    b = parse({"bridge": {"port": 9000}}).bridge
    assert (b.url, b.remote, b.client_url) == ("", False, "http://127.0.0.1:9000")


def test_url_set_means_remote_and_trailing_slash_is_dropped():
    b = parse({"bridge": {"url": "https://host.example.ts.net:8765/"}}).bridge
    assert b.remote and b.client_url == "https://host.example.ts.net:8765"


@pytest.mark.parametrize("bad", ["host:8765", 8765, True])
def test_bad_url_rejected(bad):
    with pytest.raises(SettingsError, match="bridge.url"):
        parse({"bridge": {"url": bad}})


def test_default_bridge_settings_are_local():
    assert BridgeSettings().client_url == "http://127.0.0.1:8765"


# --- emit, status, try -------------------------------------------------------------


def test_emit_goes_to_remote_bridge(remote):
    bridge, _, _, _ = remote
    args = ["done", "--no-stdin", "--session", "r1", "--cwd", "/proj"]
    assert main(["emit", *args]) == 0
    (rec,) = bridge.event_log.tail()
    assert (rec["event"], rec["session"], rec["cwd"]) == ("done", "r1", "/proj")


def test_status_names_the_remote_bridge_and_its_settings_file(remote, capsys):
    _, bridge_cfg, _, url = remote
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"at {url}" in out
    assert f"settings:  {bridge_cfg}  (on the bridge machine)" in out
    assert "client:" in out


def test_status_remote_bridge_down_shows_no_local_settings(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[bridge]\nurl = "http://127.0.0.1:{free_port()}"\n', encoding="utf-8")
    monkeypatch.setenv("REACHY_SIGNALS_CONFIG", str(cfg))
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert "bridge not reachable" in out and "bridge machine on" in out
    assert "from the settings file" not in out


# --- settings changes go to the bridge's file ----------------------------------------


def test_off_on_edit_the_bridge_file(remote, capsys):
    _, bridge_cfg, _, _ = remote
    assert main(["off"]) == 0
    assert load(bridge_cfg).enabled is False
    assert main(["on"]) == 0
    assert load(bridge_cfg).enabled is True
    assert bridge_cfg.read_text(encoding="utf-8").startswith("# bridge machine")
    assert "Reactions on" in capsys.readouterr().out


def test_quiet_and_source_edit_the_bridge_file(remote, capsys):
    bridge, bridge_cfg, _, _ = remote
    assert main(["quiet", "on"]) == 0
    assert main(["source", "herdr", "shadow"]) == 0
    s = load(bridge_cfg)
    assert s.quiet.enabled and s.source_mode("herdr") == "shadow"
    assert until(lambda: bridge.watcher.current().quiet.enabled)  # hot reload on the bridge
    capsys.readouterr()
    assert main(["quiet"]) == 0
    assert "Quiet mode: on (manual)" in capsys.readouterr().out


def test_invalid_bridge_file_is_left_alone(remote, capsys):
    _, bridge_cfg, _, _ = remote
    bridge_cfg.write_text("enabled = = broken", encoding="utf-8")
    assert main(["off"]) == 1
    assert "fix it by hand" in capsys.readouterr().out
    assert bridge_cfg.read_text(encoding="utf-8") == "enabled = = broken"


def test_settings_change_while_bridge_down(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    text = f'[bridge]\nurl = "http://127.0.0.1:{free_port()}"\n'
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("REACHY_SIGNALS_CONFIG", str(cfg))
    assert main(["quiet", "on"]) == 1
    assert "bridge not reachable" in capsys.readouterr().out
    assert cfg.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"enabled": False, "quiet": True},
        {"nope": True},
        {"enabled": "false"},
        {"source": "herdr"},
        {"source": {"name": "herdr", "mode": "loud"}},
        {"source": {"name": "a.b", "mode": "off"}},
        [],
    ],
)
def test_bad_settings_requests_rejected(remote, body):
    _, bridge_cfg, _, url = remote
    before = bridge_cfg.read_text(encoding="utf-8")
    port = int(url.rsplit(":", 1)[1])
    status, reply = request(port, "POST", "/settings", json.dumps(body))
    assert status == 422 and reply["error"]
    assert bridge_cfg.read_text(encoding="utf-8") == before


def test_settings_endpoint_needs_json_content_type(remote):
    _, bridge_cfg, _, url = remote
    port = int(url.rsplit(":", 1)[1])
    status, _ = request(port, "POST", "/settings", '{"enabled": false}', "text/plain")
    assert status == 415 and load(bridge_cfg).enabled is True
