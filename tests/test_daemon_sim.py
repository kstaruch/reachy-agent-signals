"""Daemon client against the real daemon software in mockup-sim mode.

Start it first (no robot needed, never touches USB):

    uvx --python 3.12 --from "reachy-mini==1.14.0" reachy-mini-daemon \
        --mockup-sim --headless --no-media --fastapi-port 8001

Skipped when it isn't running. As a safety guard these tests also skip unless the
daemon reports a simulation mode, so they can never move the real robot.
"""

import os
import time

import pytest

from reachy_signals.daemon import DANCES, EMOTIONS, DaemonClient, DaemonError, MoveNotFound

SIM_URL = os.environ.get("REACHY_SIGNALS_SIM_URL", "http://localhost:8001/api")

pytestmark = pytest.mark.sim


@pytest.fixture(scope="module")
def sim():
    c = DaemonClient(SIM_URL)
    try:
        status = c.status()
    except DaemonError:
        pytest.skip(f"no mockup-sim daemon at {SIM_URL}")
    if not (status.get("mockup_sim_enabled") or status.get("simulation_enabled")):
        pytest.skip(f"daemon at {SIM_URL} is not a simulation: refusing to move a real robot")
    c.stop_all()
    if c.motor_mode() != "enabled":
        c.ensure_ready()
    yield c
    c.stop_all()
    c._request("POST", "/motors/set_mode/enabled")


def test_status_and_lock(sim):
    assert sim.status()["state"] == "running"
    assert sim.lock_status().free


def test_libraries_contain_the_configured_moves(sim):
    assert sim.resolve("yeah_nod") == DANCES
    assert sim.resolve("cheerful1") == EMOTIONS
    assert sim.unknown_moves(["yeah_nod", "cheerful1", "attentive1", "toc-toc-toc"]) == []


def test_play_waits_about_as_long_as_the_move(sim):
    start = time.monotonic()
    uid = sim.play_when_idle("yeah_nod")
    assert sim.wait_move(uid, timeout=10)
    assert 1.0 < time.monotonic() - start < 5  # real robot: 2.1 s


def test_busy_play_is_dropped_by_the_daemon(sim):
    """The quirk play_when_idle exists for."""
    first = sim.play_when_idle("yeah_nod")
    second = sim.play("simple_nod")
    assert sim.running() == [first]
    assert sim.wait_move(first, timeout=10)
    time.sleep(0.3)
    assert second not in sim.running()


def test_stop_interrupts(sim):
    uid = sim.play_when_idle("toc-toc-toc")  # ~15 s
    time.sleep(0.5)
    assert sim.stop(uid) is True
    assert sim.running() == []
    assert sim.stop(uid) is False


def test_unknown_move(sim):
    with pytest.raises(MoveNotFound):
        sim.play("no_such_move", dataset=DANCES)


def test_ensure_ready_from_disabled(sim):
    sim._request("POST", "/motors/set_mode/disabled")
    assert sim.motor_mode() == "disabled"
    assert sim.ensure_ready() is True
    assert sim.motor_mode() == "enabled"
    assert sim.running() == []
