import http.client
import json
import socket

import pytest

from reachy_signals import policy
from reachy_signals.bridge import Bridge, make_server
from reachy_signals.eventlog import EventLog
from reachy_signals.events import Event
from reachy_signals.settings import parse


def request(port, method, path, body=None, content_type="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": content_type} if body is not None else {}
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def post_event(port, **fields):
    return request(port, "POST", "/events", json.dumps({"source": "claude-code", **fields}))


def test_event_is_logged_with_decision(bridge_server):
    bridge, port, _ = bridge_server
    status, body = post_event(port, event="blocked", session="s1", detail="permission_prompt")
    assert (status, body) == (202, {"decision": "pass"})
    (record,) = bridge.event_log.tail()
    assert record["event"] == "blocked" and record["session"] == "s1"
    assert record["decision"] == "pass" and record["received"].endswith("Z")


def test_health(bridge_server):
    _, port, _ = bridge_server
    post_event(port, event="observe")
    status, body = request(port, "GET", "/health")
    assert status == 200 and body["ok"] is True and body["events"] == {"observe": 1}


@pytest.mark.parametrize(
    ("body", "ctype", "status"),
    [
        ("{not json", "application/json", 400),
        ('{"source": "x", "event": "nope"}', "application/json", 400),
        ('{"source": "x", "event": "done"}', "text/plain", 415),  # browser-style simple POST
        ("x" * 70_000, "application/json", 413),
    ],
)
def test_bad_requests_rejected_and_not_logged(bridge_server, body, ctype, status):
    bridge, port, _ = bridge_server
    assert request(port, "POST", "/events", body, ctype)[0] == status
    assert bridge.event_log.tail() == []


def test_unknown_path(bridge_server):
    _, port, _ = bridge_server
    assert request(port, "GET", "/nope")[0] == 404


def test_settings_reload_changes_decision(bridge_server):
    bridge, port, cfg = bridge_server
    cfg.write_text('[sources.claude-code]\nmode = "shadow"\n', encoding="utf-8")
    bridge.watcher.check()
    assert post_event(port, event="done")[1] == {"decision": "shadow"}


def test_second_bridge_on_same_port_fails(bridge_server, tmp_path):
    bridge, port, _ = bridge_server
    with pytest.raises(OSError):
        make_server(Bridge(bridge.watcher, EventLog(tmp_path / "x.jsonl")), port)


def non_loopback_ip():
    """This machine's outward IP, found without DNS (a UDP connect sends nothing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
            return s.getsockname()[0]
        except OSError:
            return None


def test_listens_on_localhost_only(bridge_server):
    _, port, _ = bridge_server
    host_ip = non_loopback_ip()
    if not host_ip or host_ip.startswith("127.") or host_ip == "0.0.0.0":
        pytest.skip("no non-loopback address to test against")
    with pytest.raises(OSError), socket.create_connection((host_ip, port), timeout=1):
        pass


@pytest.mark.parametrize(
    ("settings", "event", "decision"),
    [
        ({}, "started", policy.PASS),
        ({}, "observe", policy.OBSERVE),
        ({"enabled": False}, "blocked", policy.DISABLED),
        ({"enabled": False}, "observe", policy.OBSERVE),
        ({"sources": {"claude-code": {"mode": "off"}}}, "done", policy.SOURCE_OFF),
        ({"sources": {"claude-code": {"mode": "shadow"}}}, "done", policy.SHADOW),
    ],
)
def test_drop_or_pass(settings, event, decision):
    ev = Event(source="claude-code", event=event)
    assert policy.drop_or_pass(parse(settings), ev) == decision


def test_unknown_source_is_off():
    assert policy.drop_or_pass(parse({}), Event(source="other", event="done")) == policy.SOURCE_OFF


def test_log_rotation(tmp_path):
    log = EventLog(tmp_path / "events.jsonl", max_bytes=200)
    for i in range(10):
        log.append({"i": i, "pad": "x" * 30})
    assert (tmp_path / "events.1.jsonl").exists()
    assert log.tail(100)[-1]["i"] == 9
