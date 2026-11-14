"""Bridge rules (docs/spec.md → Bridge behaviour) on a fake clock."""

from reachy_signals import policy
from reachy_signals.events import Event
from reachy_signals.scheduler import CLEARED, COLLAPSED, STALE, SUPERSEDED, Scheduler
from reachy_signals.settings import parse

S = parse({"bridge": {"max_event_age": "10s"}, "escalation": {"blocked_after": "3m"}})


def ev(event, session="s1", **hook):
    return Event(source="claude-code", event=event, session=session, hook=hook)


def tool(session="s1", name="PreToolUse", **extra):
    return ev("observe", session, hook_event_name=name, tool_name="Bash", **extra)


def submit(sched, event, now, decision=None):
    if decision is None:
        decision = policy.OBSERVE if event.event == "observe" else policy.PASS
    return sched.submit(event, decision, now)


def drain(sched, now=0.0, settings=S):
    out = []
    while True:
        job, _ = sched.pop(settings, now)
        if job is None:
            return out
        out.append((job.kind, job.event.session))


def test_priority_blocked_then_done_then_started():
    s = Scheduler()
    submit(s, ev("started", "a"), 0)
    submit(s, ev("done", "b"), 0)
    submit(s, ev("blocked", "c"), 0)
    assert drain(s) == [("blocked", "c"), ("done", "b"), ("started", "a")]


def test_same_priority_oldest_first():
    s = Scheduler()
    submit(s, ev("done", "a"), 0)
    submit(s, ev("done", "b"), 1)
    assert drain(s, 1) == [("done", "a"), ("done", "b")]


def test_started_burst_collapses_to_newest():
    s = Scheduler()
    submit(s, ev("started", "a"), 0)
    submit(s, ev("started", "b"), 0.1)
    dropped = submit(s, ev("started", "c"), 0.2)
    assert [(j.event.session, r) for j, r in dropped] == [("b", COLLAPSED)]
    assert drain(s, 0.2) == [("started", "c")]


def test_newer_event_from_same_session_supersedes():
    s = Scheduler()
    submit(s, ev("started", "a"), 0)
    dropped = submit(s, ev("done", "a"), 1)
    assert [(j.kind, r) for j, r in dropped] == [("started", SUPERSEDED)]
    assert drain(s, 1) == [("done", "a")]


def test_stale_events_are_dropped_not_replayed():
    s = Scheduler()
    submit(s, ev("done", "a"), 0)
    job, dropped = s.pop(S, 10.5)
    assert job is None and [(j.kind, r) for j, r in dropped] == [("done", STALE)]


def test_non_pass_decisions_do_not_queue():
    s = Scheduler()
    for decision in (policy.SHADOW, policy.SOURCE_OFF, policy.DISABLED):
        submit(s, ev("blocked"), 0, decision)
    assert drain(s) == [] and s.blocked == {}


def test_escalation_once_after_blocked_after():
    s = Scheduler()
    submit(s, ev("blocked"), 0)
    assert drain(s, 0) == [("blocked", "s1")]
    assert s.next_wakeup(S) == 180
    assert drain(s, 179) == []
    assert drain(s, 180) == [("escalation", "s1")]
    assert drain(s, 500) == []  # only once per blocked episode
    assert s.next_wakeup(S) is None


def test_new_blocked_rearms_escalation():
    s = Scheduler()
    submit(s, ev("blocked"), 0)
    drain(s, 200)
    submit(s, ev("blocked"), 300)
    assert drain(s, 300) == [("blocked", "s1")]
    assert drain(s, 480) == [("escalation", "s1")]


def test_next_event_from_session_clears_blocked():
    for clearing in (ev("started"), ev("done"), tool(), tool(name="PostToolUse")):
        s = Scheduler()
        submit(s, ev("blocked"), 0)
        drain(s)
        submit(s, clearing, 10)
        assert s.blocked == {}, clearing
        assert all(kind != "escalation" for kind, _ in drain(s, 200))


def test_tool_use_drops_a_pending_blocked_job():
    s = Scheduler()
    submit(s, ev("blocked"), 0)
    dropped = submit(s, tool(), 1)
    assert [(j.kind, r) for j, r in dropped] == [("blocked", CLEARED)]


def test_events_around_the_block_do_not_clear_it():
    s = Scheduler()
    submit(s, ev("observe", hook_event_name="PermissionRequest", tool_name="Bash"), 0)
    submit(s, ev("blocked"), 6)
    for e in (
        ev("observe", hook_event_name="Notification", notification_type="permission_prompt"),
        tool(subagent=True),  # a subagent working doesn't answer the main agent's prompt
        tool(session="other"),  # another session
    ):
        submit(s, e, 7)
    assert list(s.blocked) and len(s.blocked) == 1
    assert drain(s, 7) == [("blocked", "s1")]


def test_preempts():
    s = Scheduler()
    submit(s, ev("done", "a"), 0)
    drain(s)
    assert not s.preempts(2, S, 0)
    submit(s, ev("started", "b"), 0)
    assert not s.preempts(2, S, 0)  # started doesn't interrupt done
    submit(s, ev("blocked", "c"), 0)
    assert s.preempts(2, S, 0)


def test_due_escalation_preempts_a_done():
    s = Scheduler()
    submit(s, ev("blocked", "a"), 0)
    drain(s, 0)
    assert s.preempts(2, S, 180)


def test_blocked_sessions_report():
    s = Scheduler()
    submit(
        s,
        Event(
            source="claude-code", event="blocked", session="a", cwd="/p", detail="permission_prompt"
        ),
        0,
    )
    (info,) = s.blocked_sessions(42)
    assert info["session"] == "a" and info["seconds"] == 42 and info["escalated"] is False


def test_manual_job_skips_session_rules():
    s = Scheduler()
    s.submit_manual(ev("blocked", "try"), 0)
    assert s.blocked == {}  # a `try` never starts a blocked episode
    submit(s, ev("started", "a"), 0.1)  # doesn't collapse the manual job
    s.submit_manual(ev("started", "try"), 0.2)
    submit(s, ev("started", "b"), 0.3)
    assert drain(s, 0.3) == [("blocked", "try"), ("started", "try"), ("started", "b")]


def test_manual_job_waits_longer_than_max_event_age():
    s = Scheduler()
    s.submit_manual(ev("done", "try"), 0)
    job, dropped = s.pop(S, 25)
    assert job is not None and job.manual and dropped == []
    s.submit_manual(ev("done", "try"), 0)
    job, dropped = s.pop(S, 31)
    assert job is None and [r for _, r in dropped] == [STALE]


def test_manual_escalation_kind():
    s = Scheduler()
    s.submit_manual(ev("blocked", "try"), 0, escalation=True)
    assert drain(s) == [("escalation", "try")]
