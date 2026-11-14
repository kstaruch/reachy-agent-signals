"""Reactor end to end against the fake daemon (tests/fake_daemon.py)."""

import random
import time

import pytest

from reachy_signals import policy
from reachy_signals.eventlog import EventLog
from reachy_signals.events import Event
from reachy_signals.reactor import Reactor
from reachy_signals.settings import parse


def settings(url, **extra):
    return parse({"bridge": {"daemon_url": url}, **extra})


@pytest.fixture
def make(fake_daemon, tmp_path):
    fake, url = fake_daemon
    reactors = []

    def factory(**extra):
        cfg = {"s": settings(url, **extra)}
        log = EventLog(tmp_path / "events.jsonl")
        r = Reactor(lambda: cfg["s"], log, rng=random.Random(1))
        r.start()
        reactors.append(r)
        return r, log, cfg

    yield fake, factory
    for r in reactors:
        r.stop()


def send(r, event, session="s1", decision=policy.PASS, **hook):
    r.submit(Event(source="claude-code", event=event, session=session, hook=hook), decision)


def outcomes(log, timeout=3.0, count=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        recs = [x for x in log.tail(100) if x.get("kind") == "reaction"]
        if len(recs) >= count:
            return recs
        time.sleep(0.02)
    return [x for x in log.tail(100) if x.get("kind") == "reaction"]


def test_each_event_plays_its_reaction(make):
    fake, factory = make
    r, log, _ = factory()
    for event, move in (("started", "yeah_nod"), ("done", "cheerful1"), ("blocked", "attentive1")):
        send(r, event, session=event)
        assert outcomes(log, count=len(fake.played) + 1)[-1]["move"] == move
    assert fake.played == ["yeah_nod", "cheerful1", "attentive1"]
    assert all(x["outcome"] == "played" for x in outcomes(log, count=3))


def test_wakes_robot_before_playing(make):
    fake, factory = make
    fake.motor_mode = "disabled"
    r, log, _ = factory()
    send(r, "done")
    (rec,) = outcomes(log)
    assert rec["outcome"] == "played" and rec["woke"] is True
    assert fake.played == ["wake_up", "cheerful1"] and fake.played_while_disabled == []


def test_robot_held_by_an_app_is_left_alone(make):
    fake, factory = make
    fake.lock = {"state": "local_app", "holder_name": "conversation"}
    r, log, _ = factory()
    send(r, "blocked")
    (rec,) = outcomes(log)
    assert rec["outcome"] == "locked" and rec["holder"] == "conversation"
    assert fake.played == []


def test_blocked_interrupts_a_lower_priority_gesture(make):
    fake, factory = make
    fake.move_seconds = 2.0
    r, log, _ = factory()
    send(r, "done", session="a")
    time.sleep(0.3)
    send(r, "blocked", session="b")
    recs = outcomes(log, count=2, timeout=6)
    assert [(x["reaction"], x["outcome"]) for x in recs] == [
        ("done", "interrupted"),
        ("blocked", "played"),
    ]
    assert recs[0]["seconds"] < 1.5


def test_started_does_not_interrupt(make):
    fake, factory = make
    fake.move_seconds = 0.6
    r, log, _ = factory()
    send(r, "done", session="a")
    time.sleep(0.1)
    send(r, "started", session="b")
    recs = outcomes(log, count=2)
    assert [(x["reaction"], x["outcome"]) for x in recs] == [
        ("done", "played"),
        ("started", "played"),
    ]


def test_quiet_mode_uses_quiet_reactions(make):
    fake, factory = make
    r, log, _ = factory(
        quiet={"enabled": True, "reactions": {"done": ["simple_nod"], "started": []}}
    )
    send(r, "started", session="a")
    send(r, "done", session="b")
    recs = outcomes(log, count=2)
    assert {(x["reaction"], x["outcome"]) for x in recs} == {
        ("started", "no-reaction"),
        ("done", "played"),
    }
    assert fake.played == ["simple_nod"]


def test_random_pick_from_several(make):
    fake, factory = make
    r, log, _ = factory(reactions={"done": ["cheerful1", "simple_nod"]})
    for i in range(6):
        send(r, "done", session=f"s{i}")
        outcomes(log, count=i + 1)
    assert set(fake.played) == {"cheerful1", "simple_nod"}


def test_settings_change_while_queued_is_respected(make):
    fake, factory = make
    fake.move_seconds = 0.5
    r, log, cfg = factory()
    send(r, "done", session="a")
    time.sleep(0.1)
    send(r, "done", session="b")
    cfg["s"] = parse({"enabled": False, "bridge": cfg["s"].bridge.__dict__ | {}})
    recs = outcomes(log, count=2)
    assert recs[1]["outcome"] == "disabled"


def test_unknown_move_is_logged(make):
    fake, factory = make
    r, log, _ = factory(reactions={"done": ["moonwalk"]})
    send(r, "done")
    (rec,) = outcomes(log)
    assert rec["outcome"] == "unknown-move" and rec["move"] == "moonwalk"


def test_daemon_down_is_logged_and_dropped(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    s = settings("http://127.0.0.1:9/api")  # port 9: nothing listens
    r = Reactor(lambda: s, log)
    r.start()
    try:
        send(r, "done")
        (rec,) = outcomes(log, timeout=8)  # Windows takes ~2 s to report a refused connection
        assert rec["outcome"] == "daemon-unavailable"
    finally:
        r.stop()


def test_escalation_plays_reminder(make):
    fake, factory = make
    r, log, _ = factory(escalation={"blocked_after": 0.5, "blocked": ["toc-toc-toc"]})
    send(r, "blocked")
    recs = outcomes(log, count=2, timeout=4)
    assert [(x["reaction"], x["move"]) for x in recs] == [
        ("blocked", "attentive1"),
        ("escalation", "toc-toc-toc"),
    ]


def test_tool_use_cancels_escalation(make):
    fake, factory = make
    r, log, _ = factory(escalation={"blocked_after": 0.6, "blocked": ["toc-toc-toc"]})
    send(r, "blocked")
    outcomes(log)
    send(r, "observe", decision=policy.OBSERVE, hook_event_name="PostToolUse")
    time.sleep(1.0)
    assert fake.played == ["attentive1"]


def test_status_reports_blocked_sessions(make):
    fake, factory = make
    r, log, _ = factory()
    send(r, "blocked", session="waiting")
    outcomes(log)
    st = r.status()
    assert [b["session"] for b in st["blocked_sessions"]] == ["waiting"]
