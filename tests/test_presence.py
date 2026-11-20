"""Presence (M6): settings, the BLE detector, bridge-side state, reactor end to end."""

import random
import time

import pytest

from reachy_signals import edit, policy
from reachy_signals.bridge import Bridge
from reachy_signals.control import _presence_line, _robot_line
from reachy_signals.eventlog import EventLog
from reachy_signals.events import Event, EventError
from reachy_signals.presence import SLEEP, WAKE, Presence
from reachy_signals.presence_watch import Detector, parse_line
from reachy_signals.reactor import Reactor
from reachy_signals.settings import DetectorSettings, SettingsError, load, parse
from reachy_signals.watch import SettingsWatcher

PHONE = "AA:BB:CC:DD:EE:FF"

# --- settings -----------------------------------------------------------------


def test_presence_defaults_keep_old_behaviour():
    s = parse({})
    assert s.presence.mode == "off"
    assert s.presence.sleep_after == 120.0
    assert s.presence.missed_events == ("done", "blocked")
    assert s.presence.detector == DetectorSettings()
    assert s.reactions.missed == ("inquiring1",) and s.quiet.reactions.missed == ("inquiring1",)


def test_presence_settings_parse():
    s = parse(
        {
            "presence": {
                "mode": "auto",
                "sleep_after": "90s",
                "missed_events": ["blocked"],
                "detector": {"device": "aa:bb:cc:dd:ee:ff", "near_rssi": -60, "far_rssi": -75},
            },
            "reactions": {"missed": ["curious1"]},
        }
    )
    assert s.presence.mode == "auto" and s.presence.sleep_after == 90
    assert s.presence.missed_events == ("blocked",)
    assert s.presence.detector.device == PHONE  # upper-cased, as bluetoothctl prints it
    assert (s.presence.detector.near_rssi, s.presence.detector.far_rssi) == (-60, -75)
    assert s.reactions.missed == ("curious1",)


@pytest.mark.parametrize(
    "presence",
    [
        {"mode": "sometimes"},
        {"missed_events": ["observe"]},
        {"missed_events": "done"},
        {"sleep_after": "0s"},
        {"detector": {"device": "not-an-address"}},
        {"detector": {"near_rssi": -70, "far_rssi": -65}},  # near must be the stronger
        {"detector": {"near_rssi": -70, "far_rssi": -70}},
        {"detector": {"near_rssi": "-65"}},
    ],
)
def test_bad_presence_settings_rejected(presence):
    with pytest.raises(SettingsError):
        parse({"presence": presence})


def test_set_presence_edits_the_file(tmp_path):
    path = tmp_path / "config.toml"
    edit.update(edit.set_presence("auto"), path)
    assert load(path).presence.mode == "auto"
    with pytest.raises(SettingsError):
        edit.set_presence("maybe")


def test_presence_events_are_valid_events():
    for name in ("near", "away"):
        assert Event.from_dict({"source": "presence", "event": name}).event == name
    with pytest.raises(EventError):
        Event.from_dict({"source": "presence", "event": "nearby"})


def test_presence_events_get_their_own_decision():
    s = parse({})
    for name in ("near", "away"):
        assert policy.drop_or_pass(s, Event(source="presence", event=name)) == policy.PRESENCE


# --- detector -------------------------------------------------------------------

CFG = DetectorSettings(device=PHONE, near_rssi=-65, far_rssi=-70, window=8.0, lost_after=30.0)


def test_parse_line_reads_rssi_for_the_phone_only():
    line = f"[\x1b[0;93mCHG\x1b[0m] Device {PHONE} RSSI: 0xffffffc7 (-57)\n"
    assert parse_line(line, PHONE) == (True, -57)
    assert parse_line(f"[CHG] Device {PHONE} RSSI: -61", PHONE) == (True, -61)  # older BlueZ
    assert parse_line(line.replace(PHONE, "11:22:33:44:55:66"), PHONE) == (False, None)
    assert parse_line(f"[CHG] Device {PHONE} ManufacturerData.Key: 0x004c", PHONE) == (True, None)
    assert parse_line("[bluetooth]# scan on", PHONE) == (False, None)


def feed(det, start, values, step=0.5):
    t = start
    for v in values:
        det.heard(t, v)
        t += step
    return t


def test_detector_needs_a_few_readings_before_deciding():
    det = Detector(CFG, started=0.0)
    det.heard(0.1, -50)
    det.heard(0.2, -50)
    assert det.update(0.3) is None and det.state is None
    det.heard(0.4, -50)
    assert det.update(0.5) == "near"


def test_detector_near_and_away_use_the_median():
    det = Detector(CFG, started=0.0)
    t = feed(det, 0, [-60, -80, -62, -61, -59])  # one deep fade doesn't matter
    assert det.update(t) == "near"
    t = feed(det, t + 10, [-75, -60, -74, -72, -76])  # one strong reading doesn't either
    assert det.update(t) == "away"


def test_detector_holds_its_state_between_the_thresholds():
    det = Detector(CFG, started=0.0)
    t = feed(det, 0, [-60, -60, -60])
    assert det.update(t) == "near"
    t = feed(det, t + 10, [-67, -68, -67, -68])  # between -65 and -70
    assert det.update(t) is None and det.state == "near"
    det2 = Detector(CFG, started=0.0)
    t = feed(det2, 0, [-67, -68, -67])
    assert det2.update(t) is None and det2.state is None  # undecided from the start


def test_detector_window_drops_old_readings():
    det = Detector(CFG, started=0.0)
    feed(det, 0, [-50, -50, -50])
    assert det.median(20.0) is None  # all older than 8 s


def test_detector_away_when_not_heard():
    det = Detector(CFG, started=0.0)
    t = feed(det, 0, [-55, -55, -55])
    assert det.update(t) == "near"
    assert det.update(t + 29) is None
    assert det.update(t + 31) == "away"


def test_detector_heard_without_rssi_keeps_the_phone_present():
    det = Detector(CFG, started=0.0)
    t = feed(det, 0, [-55, -55, -55])
    det.update(t)
    det.heard(t + 25)  # e.g. manufacturer data changed, RSSI didn't
    assert det.update(t + 40) is None and det.state == "near"


def test_detector_startup_grace_then_away():
    det = Detector(CFG, started=100.0)
    assert det.update(110.0) is None
    assert det.update(131.0) == "away"


# --- bridge-side state ------------------------------------------------------------


def pset(mode="auto", sleep_after="60s", **extra):
    return parse({"presence": {"mode": mode, "sleep_after": sleep_after, **extra}})


def test_presence_auto_sleeps_after_delay_and_wakes_at_once():
    s, p = pset(), Presence()
    assert p.due(s, 0) is None  # nothing known yet: leave the robot alone
    p.observe("away", 10)
    assert p.due(s, 69) is None and p.next_wakeup(s) == 70
    assert p.due(s, 70) == SLEEP
    p.done(SLEEP)
    assert p.asleep(s) and p.due(s, 1000) is None
    p.observe("near", 1001)
    assert p.due(s, 1001) == WAKE


def test_presence_short_absence_doesnt_sleep():
    s, p = pset(), Presence()
    p.observe("away", 0)
    p.observe("near", 30)
    assert p.due(s, 100) is None and p.next_wakeup(s) is None


def test_presence_repeat_is_not_a_change():
    p = Presence()
    assert p.observe("near", 0) is True
    assert p.observe("near", 60) is False and p.since == 0


def test_presence_forced_modes():
    p = Presence()
    p.observe("near", 0)
    assert p.due(pset("asleep"), 1) == SLEEP
    p.done(SLEEP)
    assert p.due(pset("asleep"), 2) is None
    p.observe("away", 3)
    assert p.due(pset("awake"), 4) == WAKE


def test_presence_off_forgets_sleep_and_missed():
    p = Presence()
    p.done(SLEEP)
    p.note("done", pset())
    assert p.missed and p.asleep(pset())
    assert p.due(pset("off"), 0) is None
    assert not p.asleep(pset("off")) and not p.missed and p.robot == "awake"


def test_presence_missed_only_for_configured_events():
    s = pset(missed_events=["blocked"])
    p = Presence()
    p.note("done", s)
    p.note("started", s)
    assert not p.missed
    p.note("blocked", s)
    assert p.missed
    p.done(SLEEP)
    assert p.done(WAKE) is True and p.missed is False


def test_presence_retries_after_failure():
    s, p = pset("asleep"), Presence()
    p.failed(10)
    assert p.due(s, 20) is None and p.next_wakeup(s) == 40
    assert p.due(s, 40) == SLEEP


def test_presence_summary():
    s, p = pset(), Presence()
    p.observe("away", 0)
    assert p.summary(s, 15) == {
        "mode": "auto",
        "phone": "away",
        "robot": "awake",
        "missed": False,
        "sleep_in": 45.0,
    }


def test_status_lines():
    assert _presence_line({"mode": "off", "phone": None, "robot": "awake", "missed": False}) == (
        "off"
    )
    line = _presence_line({"mode": "auto", "phone": "away", "robot": "asleep", "missed": True})
    assert line == "auto, phone away, robot asleep, missed something"
    robot = {"reachable": True, "motors": "disabled", "lock": "free", "version": "1.14.0"}
    assert "asleep (presence)" in _robot_line(robot, {"mode": "auto", "robot": "asleep"})
    assert "the next reaction wakes it" in _robot_line(robot, {"mode": "off", "robot": "awake"})


# --- reactor end to end (fake daemon) ---------------------------------------------


@pytest.fixture
def make(fake_daemon, tmp_path):
    fake, url = fake_daemon
    reactors = []

    def factory(**presence):
        cfg = {
            "s": parse(
                {
                    "bridge": {"daemon_url": url},
                    "presence": {"mode": "auto", "sleep_after": "0.2s", **presence},
                }
            )
        }
        log = EventLog(tmp_path / "events.jsonl")
        r = Reactor(lambda: cfg["s"], log, rng=random.Random(1))
        r.start()
        reactors.append(r)
        return r, log, cfg

    yield fake, factory
    for r in reactors:
        r.stop()


def until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def phone(r, state):
    return r.on_presence(Event(source="presence", event=state, session="presence"))


def agent(r, event, decision, session="s1"):
    r.submit(Event(source="claude-code", event=event, session=session), decision)


def records(log, kind):
    return [x for x in log.tail(200) if x.get("kind") == kind]


def test_robot_sleeps_when_phone_leaves(make):
    fake, factory = make
    r, log, _ = factory()
    phone(r, "away")
    assert until(lambda: r.asleep())
    assert fake.played == ["goto_sleep"] and fake.motor_mode == "disabled"
    assert records(log, "presence")[-1] | {"received": None} == {
        "received": None,
        "kind": "presence",
        "action": "sleep",
        "outcome": "done",
    }


def test_missed_reaction_after_wake(make):
    fake, factory = make
    r, log, _ = factory()
    phone(r, "away")
    assert until(lambda: r.asleep())
    agent(r, "done", policy.ASLEEP)  # the bridge decides ASLEEP while asleep
    agent(r, "done", policy.ASLEEP, session="s2")
    phone(r, "near")
    assert until(lambda: "inquiring1" in fake.played)
    assert fake.played == ["goto_sleep", "wake_up", "inquiring1"]  # once, not per event
    assert fake.motor_mode == "enabled"
    wake = [x for x in records(log, "presence") if x["action"] == "wake"]
    assert wake[-1]["missed"] is True
    assert until(lambda: records(log, "reaction"))
    assert records(log, "reaction")[-1]["reaction"] == "missed"


def test_nothing_missed_means_just_waking(make):
    fake, factory = make
    r, log, _ = factory()
    phone(r, "away")
    assert until(lambda: r.asleep())
    agent(r, "started", policy.ASLEEP)  # not in missed_events
    phone(r, "near")
    assert until(lambda: not r.asleep())
    time.sleep(0.3)
    assert fake.played == ["goto_sleep", "wake_up"]


def test_blocked_session_at_sleep_is_missed_and_not_escalated(make):
    fake, factory = make
    r, log, cfg = factory()
    cfg["s"] = parse(
        {
            "bridge": {"daemon_url": cfg["s"].bridge.daemon_url},
            "presence": {"mode": "auto", "sleep_after": "0.5s"},
            "escalation": {"blocked_after": "1s"},
        }
    )
    agent(r, "blocked", policy.PASS)
    assert until(lambda: "attentive1" in fake.played)
    phone(r, "away")
    assert until(lambda: r.asleep())
    time.sleep(1.0)  # the escalation would be due by now
    assert "toc-toc-toc" not in fake.played
    phone(r, "near")
    assert until(lambda: "inquiring1" in fake.played)
    time.sleep(0.3)
    assert fake.played == ["attentive1", "goto_sleep", "wake_up", "inquiring1"]


def test_try_while_asleep_plays_then_sleeps_again(make):
    fake, factory = make
    r, log, _ = factory()
    phone(r, "away")
    assert until(lambda: r.asleep())
    result = r.try_reaction("done", timeout=5)
    assert result["outcome"] == "played" and result["slept_again"] is True
    assert fake.played == ["goto_sleep", "wake_up", "cheerful1", "goto_sleep"]
    assert fake.motor_mode == "disabled" and r.asleep()


def test_try_missed(make):
    fake, factory = make
    r, _, _ = factory(mode="off")
    result = r.try_reaction("missed", timeout=5)
    assert (result["outcome"], result["move"], result["reaction"]) == (
        "played",
        "inquiring1",
        "missed",
    )


def test_robot_held_by_an_app_isnt_put_to_sleep(make):
    fake, factory = make
    fake.lock = {"state": "local_app", "holder_name": "conversation"}
    r, log, _ = factory()
    phone(r, "away")
    assert until(lambda: records(log, "presence"))
    assert records(log, "presence")[0]["outcome"] == "locked"
    assert fake.played == [] and not r.asleep()


def test_presence_off_reacts_as_before(make):
    fake, factory = make
    r, _, _ = factory(mode="off")
    phone(r, "away")
    time.sleep(0.4)
    assert not r.asleep() and fake.played == []


def test_missed_reaction_respects_master_switch(make):
    fake, factory = make
    r, log, cfg = factory()
    phone(r, "away")
    assert until(lambda: r.asleep())
    agent(r, "done", policy.ASLEEP)
    cfg["s"] = parse(
        {
            "enabled": False,
            "bridge": {"daemon_url": cfg["s"].bridge.daemon_url},
            "presence": {"mode": "auto", "sleep_after": "0.2s"},
        }
    )
    phone(r, "near")
    assert until(lambda: records(log, "reaction"))
    assert records(log, "reaction")[-1]["outcome"] == policy.DISABLED
    assert fake.played == ["goto_sleep", "wake_up"]


# --- bridge -------------------------------------------------------------------------


@pytest.fixture
def bridge_with_reactor(fake_daemon, tmp_path):
    fake, url = fake_daemon
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[bridge]\ndaemon_url = "{url}"\n[presence]\nmode = "auto"\nsleep_after = "0.2s"\n'
    )
    watcher = SettingsWatcher(cfg)
    log = EventLog(tmp_path / "events.jsonl")
    reactor = Reactor(watcher.current, log, rng=random.Random(1))
    reactor.start()
    yield fake, Bridge(watcher, log, reactor), log
    reactor.stop()


def test_bridge_logs_presence_changes_not_heartbeats(bridge_with_reactor):
    fake, bridge, log = bridge_with_reactor
    for _ in range(3):
        assert bridge.handle(Event(source="presence", event="near")) == policy.PRESENCE
    logged = [x for x in log.tail(50) if x.get("source") == "presence"]
    assert len(logged) == 1 and bridge.health()["events"]["presence"] == 3


def test_bridge_decides_asleep_while_robot_sleeps(bridge_with_reactor):
    fake, bridge, log = bridge_with_reactor
    bridge.handle(Event(source="presence", event="away"))
    assert until(lambda: bridge.reactor.asleep())
    assert bridge.handle(Event(source="claude-code", event="done", session="s")) == policy.ASLEEP
    assert bridge.handle(Event(source="claude-code", event="observe")) == policy.OBSERVE
    assert bridge.status()["presence"]["missed"] is True


def test_bridge_settings_change_presence(bridge_with_reactor):
    fake, bridge, _ = bridge_with_reactor
    summary = bridge.change_settings({"presence": "asleep"})
    assert summary["presence"]["mode"] == "asleep"
    with pytest.raises(SettingsError):
        bridge.change_settings({"presence": "sometimes"})
