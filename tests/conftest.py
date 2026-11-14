import threading

import pytest

from reachy_signals.bridge import Bridge, make_server
from reachy_signals.eventlog import EventLog
from reachy_signals.watch import SettingsWatcher


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Never touch the real settings file or event log from tests."""
    monkeypatch.setenv("REACHY_SIGNALS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("REACHY_SIGNALS_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def bridge_server(tmp_path):
    """A real bridge on a free localhost port. Yields (bridge, port, config_path)."""
    cfg = tmp_path / "config.toml"
    bridge = Bridge(SettingsWatcher(cfg), EventLog(tmp_path / "logs" / "events.jsonl"))
    server = make_server(bridge, 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield bridge, server.server_address[1], cfg
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def fake_daemon():
    """A fake daemon on a free port. Yields (FakeDaemon, base_url)."""
    from fake_daemon import FakeDaemon, _Server

    fake = FakeDaemon()
    server = _Server(fake)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}/api"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
