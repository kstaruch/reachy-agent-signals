"""Daemon client against the in-process fake (tests/fake_daemon.py)."""

import socket
import time

import pytest

from reachy_signals.daemon import (
    DANCES,
    EMOTIONS,
    DaemonClient,
    DaemonError,
    DaemonUnavailable,
    MoveNotFound,
)


@pytest.fixture
def client(fake_daemon):
    fake, url = fake_daemon
    return fake, DaemonClient(url)


def test_state(client):
    fake, c = client
    assert c.status()["state"] == "running"
    assert c.motor_mode() == "enabled"
    assert c.lock_status().free
    fake.lock = {"state": "local_app", "holder_name": "conversation"}
    lock = c.lock_status()
    assert not lock.free and lock.holder == "conversation"


def test_resolve_and_list_cached(client):
    fake, c = client
    assert c.resolve("cheerful1") == EMOTIONS
    assert c.resolve("yeah_nod") == DANCES
    lists = [r for r in fake.requests if "datasets/list" in r]
    c.resolve("simple_nod")
    assert [r for r in fake.requests if "datasets/list" in r] == lists  # cached
    with pytest.raises(MoveNotFound):
        c.resolve("moonwalk")


def test_unknown_moves(client):
    _, c = client
    assert c.unknown_moves(["yeah_nod", "moonwalk", "cheerful1", "moonwalk"]) == ["moonwalk"]


def test_play_and_wait(client):
    fake, c = client
    uid = c.play("yeah_nod")
    assert c.running() == [uid]
    assert c.wait_move(uid, timeout=2)
    assert c.running() == [] and fake.played == ["yeah_nod"]


def test_play_404_is_move_not_found(client):
    _, c = client
    with pytest.raises(MoveNotFound):
        c.play("moonwalk", dataset=DANCES)


def test_play_when_idle_waits_instead_of_being_dropped(client):
    fake, c = client
    c.play("cheerful1")
    c.play("yeah_nod")  # what a naive client does: silently dropped
    assert fake.dropped == ["yeah_nod"]
    c.play_when_idle("simple_nod", idle_timeout=2)
    assert fake.played == ["cheerful1", "simple_nod"]


def test_play_when_idle_gives_up(client):
    fake, c = client
    fake.start("toc-toc-toc", seconds=5)
    with pytest.raises(DaemonError, match="still busy"):
        c.play_when_idle("yeah_nod", idle_timeout=0.2)


def test_stop(client):
    fake, c = client
    uid = c.play("toc-toc-toc")
    assert c.stop(uid) is True
    assert c.running() == []
    assert c.stop(uid) is False  # already gone: daemon answers 500, client says False


def test_stop_all(client):
    fake, c = client
    fake.start("toc-toc-toc", seconds=5)
    assert c.stop_all() == 1 and c.running() == []


def test_ensure_ready_wakes_only_when_needed(client):
    fake, c = client
    assert c.ensure_ready() is False
    assert not any("wake_up" in r for r in fake.requests)
    for mode in ("disabled", "gravity_compensation"):
        fake.motor_mode = mode
        assert c.ensure_ready() is True
        assert fake.motor_mode == "enabled"
    assert fake.played.count("wake_up") == 2


def test_without_ensure_ready_moves_play_with_motors_off(client):
    """Documents the daemon quirk the bridge guards against."""
    fake, c = client
    fake.motor_mode = "disabled"
    c.play("yeah_nod")
    assert fake.played_while_disabled == ["yeah_nod"]


def test_unreachable_daemon_fails_fast():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = DaemonClient(f"http://127.0.0.1:{port}/api", timeout=1)
    start = time.monotonic()
    with pytest.raises(DaemonUnavailable):
        c.status()
    assert time.monotonic() - start < 2


def test_http_error_carries_status(client):
    _, c = client
    with pytest.raises(DaemonError) as e:
        c._request("POST", "/motors/set_mode/sleepy")
    assert e.value.status == 422
