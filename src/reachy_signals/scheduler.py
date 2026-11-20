"""Which gesture plays next: the bridge rules that don't touch the robot.

Pure and clock-driven (callers pass ``now``, a monotonic time in seconds), so every
rule in docs/spec.md → Bridge behaviour can be tested exactly. The worker thread
in reactor.py owns one Scheduler and does the daemon calls.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from . import policy
from .events import REACTION_EVENTS, Event
from .settings import Settings

PRIORITY = {"started": 1, "done": 2, "blocked": 3}
MISSED_PRIORITY = 2  # the `missed` reaction after presence wakes the robot: like `done`

# Observation-only hooks that prove the agent is running again, so a blocked session
# isn't blocked any more. Provisional answer to the spec's open question "what
# clears blocked": a tool can't run while the agent waits for the user.
# Not PermissionRequest or Notification: those arrive around the block itself.
CLEARING_HOOKS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "StopFailure")

# Why a job never played (logged by the caller).
SUPERSEDED = "superseded"  # the session sent a newer event first
COLLAPSED = "collapsed"  # burst of `started`: only the newest plays
STALE = "stale"  # older than bridge.max_event_age when its turn came
CLEARED = "cleared"  # blocked job dropped because the session is running again
ASLEEP = "asleep"  # dropped because presence put the robot to sleep

# `reachy-signals try` waits for its gesture, so it may wait behind a long one.
MANUAL_MAX_AGE = 30.0


@dataclass(frozen=True)
class Job:
    event: Event
    received: float  # monotonic
    seq: int
    escalation: bool = False
    manual: bool = False  # `reachy-signals try`: no drop/pass, no blocked tracking
    missed: bool = False  # the `missed` reaction (presence), not an agent event
    on_done: Callable[[dict[str, Any]], None] | None = field(default=None, compare=False)

    @property
    def priority(self) -> int:
        return MISSED_PRIORITY if self.missed else PRIORITY[self.event.event]

    @property
    def kind(self) -> str:
        if self.missed:
            return "missed"
        return "escalation" if self.escalation else self.event.event


@dataclass
class _Blocked:
    since: float
    event: Event
    escalated: bool = False


@dataclass
class Scheduler:
    pending: list[Job] = field(default_factory=list)
    blocked: dict[str, _Blocked] = field(default_factory=dict)  # by session key
    _seq: Iterator[int] = field(default_factory=itertools.count)

    # --- input --------------------------------------------------------------

    def submit(self, event: Event, decision: str, now: float) -> list[tuple[Job, str]]:
        """Take one event as the bridge decided it. Returns jobs dropped as a result."""
        key = _session_key(event)
        dropped: list[tuple[Job, str]] = []
        clears = event.event in REACTION_EVENTS or _is_clearing(event)
        # ASLEEP clears too: a session that finished while the robot slept isn't blocked.
        if clears and decision in (policy.PASS, policy.OBSERVE, policy.ASLEEP):
            if key in self.blocked and event.event != "blocked":
                del self.blocked[key]
            # anything still queued for this session is out of date now
            reason = SUPERSEDED if event.event in REACTION_EVENTS else CLEARED
            dropped += self._remove(lambda j: _session_key(j.event) == key, reason)

        if decision != policy.PASS or event.event not in REACTION_EVENTS:
            return dropped
        if event.event == "started":
            dropped += self._remove(
                lambda j: j.event.event == "started" and not (j.escalation or j.manual), COLLAPSED
            )
        if event.event == "blocked":
            self.blocked[key] = _Blocked(since=now, event=event)
        self.pending.append(Job(event=event, received=now, seq=next(self._seq)))
        return dropped

    def submit_manual(
        self,
        event: Event,
        now: float,
        escalation: bool = False,
        on_done: Callable[[dict[str, Any]], None] | None = None,
        missed: bool = False,
    ) -> Job:
        """Queue a `try`: plays like a real event but leaves session state alone."""
        job = Job(
            event=event,
            received=now,
            seq=next(self._seq),
            escalation=escalation,
            manual=True,
            missed=missed,
            on_done=on_done,
        )
        self.pending.append(job)
        return job

    def submit_missed(self, event: Event, now: float) -> Job:
        """Queue the `missed` reaction (presence just woke the robot)."""
        job = Job(event=event, received=now, seq=next(self._seq), missed=True)
        self.pending.append(job)
        return job

    def drop_all(self, reason: str) -> list[tuple[Job, str]]:
        """Drop every queued job except `try` requests (presence put the robot to sleep)."""
        return self._remove(lambda j: not j.manual, reason)

    def silence_blocked(self) -> bool:
        """No reminders for sessions blocked now. True if any session is blocked.

        Used when the robot goes to sleep: the `missed` reaction on waking says it
        instead, and a reminder for an old block right after waking would repeat it.
        """
        for b in self.blocked.values():
            b.escalated = True
        return bool(self.blocked)

    # --- output -------------------------------------------------------------

    def pop(self, settings: Settings, now: float) -> tuple[Job | None, list[tuple[Job, str]]]:
        """Next job to play (highest priority, then oldest), plus jobs dropped as stale."""
        self._queue_due_escalations(settings, now)
        dropped = self._remove(lambda j: now - j.received > _max_age(j, settings), STALE)
        if not self.pending:
            return None, dropped
        job = max(self.pending, key=lambda j: (j.priority, -j.seq))
        self.pending.remove(job)
        return job, dropped

    def preempts(self, priority: int, settings: Settings, now: float) -> bool:
        """True if something pending (or an escalation now due) outranks ``priority``."""
        self._queue_due_escalations(settings, now)
        return any(j.priority > priority for j in self.pending)

    def next_wakeup(self, settings: Settings) -> float | None:
        """Monotonic time of the earliest escalation still to come."""
        due = [
            b.since + settings.escalation.blocked_after
            for b in self.blocked.values()
            if not b.escalated
        ]
        return min(due, default=None)

    def blocked_sessions(self, now: float) -> list[dict[str, object]]:
        return [
            {
                "session": b.event.session,
                "cwd": b.event.cwd,
                "source": b.event.source,
                "detail": b.event.detail,
                "seconds": round(now - b.since, 1),
                "escalated": b.escalated,
            }
            for b in self.blocked.values()
        ]

    # --- internals ----------------------------------------------------------

    def _queue_due_escalations(self, settings: Settings, now: float) -> None:
        for b in self.blocked.values():
            if not b.escalated and now - b.since >= settings.escalation.blocked_after:
                b.escalated = True  # once per blocked episode
                self.pending.append(
                    Job(event=b.event, received=now, seq=next(self._seq), escalation=True)
                )

    def _remove(self, pred, reason: str) -> list[tuple[Job, str]]:
        gone = [j for j in self.pending if pred(j)]
        self.pending = [j for j in self.pending if not pred(j)]
        return [(j, reason) for j in gone]


def _max_age(job: Job, settings: Settings) -> float:
    return MANUAL_MAX_AGE if job.manual else settings.bridge.max_event_age


def _session_key(event: Event) -> str:
    # sessions are per source; an event without a session id is its own session
    return f"{event.source}\x00{event.session or event.cwd}"


def _is_clearing(event: Event) -> bool:
    if event.event != "observe" or event.hook.get("subagent"):
        return False  # a subagent's tool use doesn't mean the main agent was answered
    return event.hook.get("hook_event_name") in CLEARING_HOOKS
