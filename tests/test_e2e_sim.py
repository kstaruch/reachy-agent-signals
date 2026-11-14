"""emit → bridge → reactor → mockup-sim daemon. Skips without a simulation daemon."""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from reachy_signals.bridge import Bridge, make_server
from reachy_signals.daemon import DaemonClient, DaemonError
from reachy_signals.eventlog import EventLog
from reachy_signals.reactor import Reactor
from reachy_signals.watch import SettingsWatcher

SIM_URL = os.environ.get("REACHY_SIGNALS_SIM_URL", "http://127.0.0.1:8001/api")

pytestmark = pytest.mark.sim


@pytest.fixture
def stack(tmp_path):
    try:
        status = DaemonClient(SIM_URL).status()
    except DaemonError:
        pytest.skip(f"no mockup-sim daemon at {SIM_URL}")
    if not (status.get("mockup_sim_enabled") or status.get("simulation_enabled")):
        pytest.skip("daemon is not a simulation: refusing to move a real robot")
    cfg = tmp_path / "config.toml"
    log = EventLog(tmp_path / "events.jsonl")
    watcher = SettingsWatcher(cfg)  # no file yet: defaults until the port is known
    reactor = Reactor(watcher.current, log)
    server = make_server(Bridge(watcher, log, reactor), 0)
    port = server.server_address[1]
    cfg.write_text(f'[bridge]\nport = {port}\ndaemon_url = "{SIM_URL}"\n', encoding="utf-8")
    watcher.check()
    assert watcher.current().bridge.daemon_url == SIM_URL
    reactor.start()
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    t.start()
    yield cfg, log
    server.shutdown()
    server.server_close()
    t.join()
    reactor.stop()


def emit(cfg, event, payload):
    subprocess.run(
        [sys.executable, "-m", "reachy_signals", "emit", event],
        input=json.dumps(payload),
        text=True,
        timeout=10,
        check=True,
        env={**os.environ, "REACHY_SIGNALS_CONFIG": str(cfg)},
    )


def wait_for(log, n, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        recs = [x for x in log.tail(200) if x.get("kind") == "reaction"]
        if len(recs) >= n:
            return recs
        time.sleep(0.1)
    return [x for x in log.tail(200) if x.get("kind") == "reaction"]


def test_hook_event_plays_on_the_sim(stack):
    cfg, log = stack
    emit(
        cfg,
        "started",
        {"session_id": "e2e", "cwd": "/w", "hook_event_name": "UserPromptSubmit", "prompt": "hi"},
    )
    (rec,) = wait_for(log, 1)
    assert (rec["reaction"], rec["move"], rec["outcome"]) == ("started", "yeah_nod", "played")
    assert 1.0 < rec["seconds"] < 5


def test_blocked_interrupts_done_on_the_sim(stack):
    cfg, log = stack
    emit(cfg, "done", {"session_id": "a", "hook_event_name": "Stop"})
    time.sleep(1.0)
    emit(
        cfg,
        "blocked",
        {
            "session_id": "b",
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
        },
    )
    recs = wait_for(log, 2)
    assert [(x["reaction"], x["outcome"]) for x in recs] == [
        ("done", "interrupted"),
        ("blocked", "played"),
    ]
