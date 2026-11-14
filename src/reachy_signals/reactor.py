"""The one thread that moves the robot (spec → Bridge behaviour, steps 2-7).

Events come in from HTTP handler threads via :meth:`Reactor.submit`; a single
worker takes jobs from the Scheduler and plays them, one gesture at a time.
Every outcome goes to the event log.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import policy
from .daemon import DaemonClient, DaemonError, DaemonUnavailable, MoveNotFound
from .eventlog import EventLog
from .events import Event, now_iso
from .scheduler import Job, Scheduler
from .settings import Settings, quiet_active

log = logging.getLogger(__name__)

POLL = 0.1  # seconds between checks while a gesture plays
MOVE_TIMEOUT = 30.0  # longest library move is ~15 s

# Outcomes written to the event log.
PLAYED = "played"
INTERRUPTED = "interrupted"
NO_REACTION = "no-reaction"  # empty reaction list
LOCKED = "locked"  # an app or remote session holds the robot
DAEMON_DOWN = "daemon-unavailable"
UNKNOWN_MOVE = "unknown-move"
ERROR = "error"


class Reactor:
    def __init__(
        self,
        settings: Callable[[], Settings],
        event_log: EventLog,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        client_factory: Callable[[str], DaemonClient] = DaemonClient,
    ) -> None:
        self._settings = settings
        self._log = event_log
        self._clock = clock
        self._rng = rng or random.Random()
        self._client_factory = client_factory
        self._clients: dict[str, DaemonClient] = {}
        self._sched = Scheduler()
        self._cond = threading.Condition()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.current: dict[str, Any] | None = None  # what's playing, for /health
        self.woke_robot = False

    # --- input --------------------------------------------------------------

    def submit(self, event: Event, decision: str) -> None:
        with self._cond:
            dropped = self._sched.submit(event, decision, self._clock())
            self._cond.notify_all()
        for job, reason in dropped:
            self._record(job, reason)

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="reactor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        with self._cond:
            now = self._clock()
            return {
                "playing": self.current,
                "pending": [j.kind for j in self._sched.pending],
                "blocked_sessions": self._sched.blocked_sessions(now),
            }

    # --- worker -------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
                settings = self._settings()
                job, dropped = self._sched.pop(settings, self._clock())
                if job is None:
                    wake = self._sched.next_wakeup(settings)
                    timeout = 1.0 if wake is None else max(0.0, min(1.0, wake - self._clock()))
                    if not dropped:
                        self._cond.wait(timeout)
            for d, reason in dropped:
                self._record(d, reason)
            if job is not None:
                try:
                    self._execute(job, settings)
                except Exception as e:  # never let the worker die
                    log.exception("reaction failed")
                    self._record(job, ERROR, error=str(e))

    def _execute(self, job: Job, settings: Settings) -> None:
        # Settings may have changed while the job waited: check drop/pass again.
        decision = policy.drop_or_pass(settings, job.event)
        if decision != policy.PASS:
            self._record(job, decision)
            return

        names = self._reaction_names(job, settings)
        if not names:
            self._record(job, NO_REACTION)
            return
        move = self._rng.choice(names)
        client = self._client(settings.bridge.daemon_url)
        try:
            lock = client.lock_status()
            if not lock.free:
                self._record(job, LOCKED, move=move, holder=lock.holder or lock.state)
                return
            woke = client.ensure_ready()
            self.woke_robot |= woke
            if not client.wait_idle(MOVE_TIMEOUT):
                self._record(job, ERROR, move=move, error="robot still busy")
                return
            started = self._clock()
            uuid = client.play(move)
        except DaemonUnavailable as e:
            self._record(job, DAEMON_DOWN, move=move, error=str(e))
            return
        except MoveNotFound as e:
            self._record(job, UNKNOWN_MOVE, move=move, error=str(e))
            return
        except DaemonError as e:
            self._record(job, ERROR, move=move, error=str(e))
            return

        with self._cond:
            self.current = {"move": move, "kind": job.kind, "session": job.event.session}
        try:
            outcome = self._wait(client, uuid, job, settings)
        finally:
            with self._cond:
                self.current = None
        self._record(job, outcome, move=move, woke=woke, seconds=round(self._clock() - started, 2))

    def _wait(self, client: DaemonClient, uuid: str, job: Job, settings: Settings) -> str:
        """Wait for the move to finish; stop it if a higher-priority job arrives."""
        deadline = self._clock() + MOVE_TIMEOUT
        while self._clock() < deadline:
            with self._cond:
                if self._stopping:
                    client.stop(uuid)
                    return INTERRUPTED
                if self._sched.preempts(job.priority, self._settings(), self._clock()):
                    preempt = True
                else:
                    preempt = False
                    self._cond.wait(POLL)
            if preempt:
                client.stop(uuid)
                return INTERRUPTED
            try:
                if uuid not in client.running():
                    return PLAYED
            except DaemonError:
                return DAEMON_DOWN
        return ERROR

    # --- helpers ------------------------------------------------------------

    def _reaction_names(self, job: Job, settings: Settings) -> tuple[str, ...]:
        quiet = quiet_active(settings, datetime.now())
        if job.escalation:
            # In quiet mode, an empty quiet `blocked` list silences the reminder too.
            if quiet and not settings.quiet.reactions.blocked:
                return ()
            return settings.escalation.blocked
        reactions = settings.quiet.reactions if quiet else settings.reactions
        return reactions.for_event(job.event.event)

    def _client(self, url: str) -> DaemonClient:
        if url not in self._clients:
            self._clients[url] = self._client_factory(url)
        return self._clients[url]

    def _record(self, job: Job, outcome: str, **extra: Any) -> None:
        ev = job.event
        self._log.append(
            {
                "received": now_iso(),
                "kind": "reaction",
                "reaction": job.kind,
                "source": ev.source,
                "session": ev.session,
                "cwd": ev.cwd,
                "outcome": outcome,
                **extra,
            }
        )
        level = logging.INFO if outcome in (PLAYED, INTERRUPTED, NO_REACTION) else logging.WARNING
        log.log(
            level,
            "%s %s session=%s -> %s %s",
            ev.source,
            job.kind,
            ev.session[:8],
            outcome,
            " ".join(f"{k}={v}" for k, v in extra.items()),
        )
