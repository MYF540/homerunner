"""Pure power state machine of Homerunner (no Home Assistant imports).

The machine never looks at the wall clock: every method receives ``now`` (aware, UTC).
It decides what to do (wake, shut down, notify) and returns :class:`Action` objects; the
HA glue (``manager.py``) executes them and reports results back via ``on_wake_result`` /
``on_shutdown_result``.

Dry run
-------
With ``rules.dry_run`` the machine still emits ``wake``/``shutdown`` actions (flagged with
``detail["dry_run"] = True``) but nothing is switched, so ``obs.power`` is meaningless for
the machine's own decisions. While dry run is on the *simulated* power state
(``simulated_power``) is authoritative instead of ``obs.power`` and the guard's
``runners_ready`` is replaced by "simulated power is ON". A wake action flips the simulated
power to ON, a shutdown action flips it to OFF; everything else (state transitions, timers,
limits) then behaves exactly as in live mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from .const import PowerState, ServerState, WakeReason

__all__ = [
    "Action",
    "Observation",
    "PowerState",
    "PowerStateMachine",
    "Rules",
    "ServerState",
    "WakeReason",
]

SHUTDOWN_STUCK_MINUTES = 15


@dataclass
class Rules:
    """Tunable rules (mutable at runtime; changes apply on the next step)."""

    idle_hours: float = 2.0
    short_idle_minutes: float = 20
    min_uptime_minutes: float = 15
    max_starts_per_day: int = 8
    forced_wake_days: float = 5
    forced_wake_minutes: float = 30
    wake_timeout_minutes: float = 10
    schedule_guard_minutes: float = 30
    queue_alert_minutes: float = 15
    automatic: bool = False
    dry_run: bool = True


@dataclass
class Observation:
    """Everything the machine needs to know about the world for one step."""

    now: datetime
    local_date: date
    power: PowerState
    runners_ready: bool | None = None
    queued: int = 0
    running: int = 0
    github_ok: bool = True
    next_schedule_at: datetime | None = None
    prewake_due: bool = False
    oldest_queued_at: datetime | None = None


@dataclass
class Action:
    """Something the manager should do or tell the user."""

    kind: Literal["wake", "shutdown", "notify", "event"]
    reason: str
    detail: dict[str, Any]


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else None
    return None


def _date(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _enum(cls: type, value: Any, default: Any) -> Any:
    try:
        return cls(value)
    except ValueError:
        return default


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


class PowerStateMachine:
    """Decides when to wake and when to shut down the runner host."""

    def __init__(self, rules: Rules, stored: dict | None = None) -> None:
        self.rules = rules
        self.state: ServerState = ServerState.UNKNOWN
        self.idle_since: datetime | None = None
        self.last_wake_reason: WakeReason | None = None
        self.starts_today: int = 0
        self.starts_date: date | None = None
        self.worked_since_wake: bool = False
        self.keep_awake_until: datetime | None = None
        self.last_on_at: datetime | None = None
        self.last_off_at: datetime | None = None
        self.starting_since: datetime | None = None
        self.ready_at: datetime | None = None
        self.wake_retry_at: datetime | None = None
        self.shutdown_requested_at: datetime | None = None
        self.shutdown_accepted: bool = False
        self.simulated_power: PowerState | None = None
        self.blockers: list[str] = []
        self._wake_failed_notified = False
        self._stuck_notified = False
        self._limit_notified_date: date | None = None
        self._queue_alert_sent = False
        self._last_jobs = 0
        if stored:
            self._restore(stored)

    # ------------------------------------------------------------------ persistence

    def _restore(self, s: dict) -> None:
        self.state = _enum(ServerState, s.get("state"), ServerState.UNKNOWN)
        self.idle_since = _dt(s.get("idle_since"))
        self.last_wake_reason = _enum(WakeReason, s.get("last_wake_reason"), None)
        starts = s.get("starts_today")
        self.starts_today = starts if isinstance(starts, int) and starts >= 0 else 0
        self.starts_date = _date(s.get("starts_date"))
        self.worked_since_wake = bool(s.get("worked_since_wake", False))
        self.keep_awake_until = _dt(s.get("keep_awake_until"))
        self.last_on_at = _dt(s.get("last_on_at"))
        self.last_off_at = _dt(s.get("last_off_at"))
        self.starting_since = _dt(s.get("starting_since"))
        self.ready_at = _dt(s.get("ready_at"))
        self.wake_retry_at = _dt(s.get("wake_retry_at"))
        self.shutdown_requested_at = _dt(s.get("shutdown_requested_at"))
        self.shutdown_accepted = bool(s.get("shutdown_accepted", False))
        self.simulated_power = _enum(PowerState, s.get("simulated_power"), None)
        self._wake_failed_notified = bool(s.get("wake_failed_notified", False))
        self._stuck_notified = bool(s.get("shutdown_stuck_notified", False))
        self._limit_notified_date = _date(s.get("limit_notified_date"))
        self._queue_alert_sent = bool(s.get("queue_alert_sent", False))
        jobs = s.get("last_jobs")
        self._last_jobs = jobs if isinstance(jobs, int) and jobs >= 0 else 0

    def as_dict(self) -> dict:
        """JSON-serialisable snapshot for the Store."""
        return {
            "state": self.state.value,
            "idle_since": _iso(self.idle_since),
            "last_wake_reason": self.last_wake_reason.value if self.last_wake_reason else None,
            "starts_today": self.starts_today,
            "starts_date": _iso(self.starts_date),
            "worked_since_wake": self.worked_since_wake,
            "keep_awake_until": _iso(self.keep_awake_until),
            "last_on_at": _iso(self.last_on_at),
            "last_off_at": _iso(self.last_off_at),
            "starting_since": _iso(self.starting_since),
            "ready_at": _iso(self.ready_at),
            "wake_retry_at": _iso(self.wake_retry_at),
            "shutdown_requested_at": _iso(self.shutdown_requested_at),
            "shutdown_accepted": self.shutdown_accepted,
            "simulated_power": self.simulated_power.value if self.simulated_power else None,
            "wake_failed_notified": self._wake_failed_notified,
            "shutdown_stuck_notified": self._stuck_notified,
            "limit_notified_date": _iso(self._limit_notified_date),
            "queue_alert_sent": self._queue_alert_sent,
            "last_jobs": self._last_jobs,
        }

    # ------------------------------------------------------------------ derived values

    @property
    def shutdown_at(self) -> datetime | None:
        """Earliest instant the idle rules allow an automatic shutdown (None while busy).

        This covers the timing rules (idle timer, min uptime, forced-wake hold, keep-awake);
        the remaining blockers (automatic off, schedule guard, GitHub, power unknown, start
        limit) are evaluated in ``step`` and listed in ``blockers``.
        """
        if self.idle_since is None or self.state != ServerState.READY:
            return None
        if self.worked_since_wake:
            base = self.idle_since + timedelta(hours=self.rules.idle_hours)
        else:
            base = self.idle_since + timedelta(minutes=self.rules.short_idle_minutes)
        candidates = [base]
        if self.last_on_at is not None:
            candidates.append(self.last_on_at + timedelta(minutes=self.rules.min_uptime_minutes))
            if self.last_wake_reason == WakeReason.FORCED:
                candidates.append(
                    self.last_on_at + timedelta(minutes=self.rules.forced_wake_minutes)
                )
        if self.keep_awake_until is not None:
            candidates.append(self.keep_awake_until)
        return max(candidates)

    # ------------------------------------------------------------------ helpers

    def _dry(self) -> dict[str, Any]:
        return {"dry_run": True} if self.rules.dry_run else {}

    def _roll_day(self, local_date: date) -> None:
        if self.starts_date != local_date:
            self.starts_date = local_date
            self.starts_today = 0
            self._limit_notified_date = None

    def _limit_actions(self) -> list[Action]:
        if (
            self.starts_today >= self.rules.max_starts_per_day
            and self.starts_date is not None
            and self._limit_notified_date != self.starts_date
        ):
            self._limit_notified_date = self.starts_date
            return [
                Action(
                    "notify",
                    "start_limit",
                    {
                        "starts_today": self.starts_today,
                        "max_starts_per_day": self.rules.max_starts_per_day,
                    },
                )
            ]
        return []

    def _start(
        self, now: datetime, reason: WakeReason, *, count: bool = True, send_wake: bool = True
    ) -> list[Action]:
        self.state = ServerState.STARTING
        self.starting_since = now
        self.last_wake_reason = reason
        self.worked_since_wake = False
        self.idle_since = None
        self.ready_at = None
        self.wake_retry_at = None
        self.shutdown_requested_at = None
        self.shutdown_accepted = False
        self._wake_failed_notified = False
        self._stuck_notified = False
        self._queue_alert_sent = False
        if count:
            self.starts_today += 1
        actions: list[Action] = []
        if send_wake:
            if self.rules.dry_run:
                self.simulated_power = PowerState.ON
            actions.append(Action("wake", reason.value, self._dry()))
        actions.extend(self._limit_actions())
        return actions

    def _power(self, obs: Observation) -> PowerState:
        if self.rules.dry_run:
            if self.simulated_power is None:
                self.simulated_power = obs.power
            return self.simulated_power
        self.simulated_power = None
        return obs.power

    def _reason_persists(self, obs: Observation) -> bool:
        reason = self.last_wake_reason
        if reason == WakeReason.QUEUED_JOB:
            return obs.queued > 0
        if reason == WakeReason.SCHEDULE:
            return obs.prewake_due or obs.queued > 0
        return True

    def _become_up(self, obs: Observation, *, recovered: bool) -> list[Action]:
        now = obs.now
        jobs = obs.queued + obs.running
        self.ready_at = now
        self._wake_failed_notified = False
        self.wake_retry_at = None
        waited = (now - self.starting_since).total_seconds() if self.starting_since else None
        if jobs > 0:
            self.state = ServerState.ACTIVE
            self.worked_since_wake = True
            self.idle_since = None
        else:
            self.state = ServerState.READY
            self.idle_since = now
        return [
            Action(
                "event",
                "woke",
                {
                    "wake_reason": self.last_wake_reason.value if self.last_wake_reason else None,
                    "seconds": waited,
                    "recovered": recovered,
                },
            )
        ]

    # ------------------------------------------------------------------ public API

    def step(self, obs: Observation) -> list[Action]:
        """Advance the machine with a fresh observation."""
        now = obs.now
        self._roll_day(obs.local_date)
        power = self._power(obs)
        runners_ready = (power == PowerState.ON) if self.rules.dry_run else obs.runners_ready
        self._last_jobs = obs.queued + obs.running
        self.blockers = []
        actions: list[Action] = self._limit_actions()

        if self.state == ServerState.UNKNOWN:
            actions += self._first_step(obs, power, runners_ready)
        elif self.state == ServerState.OFF:
            actions += self._step_off(obs, power)
        elif self.state in (ServerState.STARTING, ServerState.WAKE_FAILED):
            actions += self._step_starting(obs, power, runners_ready)
        elif self.state in (ServerState.READY, ServerState.ACTIVE):
            actions += self._step_up(obs, power)
        elif self.state == ServerState.SHUTTING_DOWN:
            actions += self._step_shutting_down(obs, power)
        return actions

    def _first_step(
        self, obs: Observation, power: PowerState, runners_ready: bool | None
    ) -> list[Action]:
        now = obs.now
        if power == PowerState.OFF:
            self.state = ServerState.OFF
            if self.last_off_at is None:
                self.last_off_at = now
            return []
        if power == PowerState.ON:
            self.last_on_at = now
            self.starting_since = now
            if self.last_wake_reason is None:
                self.last_wake_reason = WakeReason.MANUAL
            if runners_ready:
                self.worked_since_wake = False
                return self._become_up(obs, recovered=False)
            self.state = ServerState.STARTING
        return []

    def _step_off(self, obs: Observation, power: PowerState) -> list[Action]:
        now = obs.now
        if power == PowerState.UNKNOWN:
            return []
        if power == PowerState.ON:
            # Somebody switched the machine on (BMC button, WoL from elsewhere ...).
            self.last_on_at = now
            return self._start(now, WakeReason.MANUAL, send_wake=False)
        if self.last_off_at is None:
            self.last_off_at = now
        if not self.rules.automatic:
            return []
        reason: WakeReason | None = None
        if obs.queued > 0:
            reason = WakeReason.QUEUED_JOB
        elif obs.prewake_due:
            reason = WakeReason.SCHEDULE
        elif now - self.last_off_at >= timedelta(days=self.rules.forced_wake_days):
            reason = WakeReason.FORCED
        if reason is None:
            return []
        return self._start(now, reason)

    def _step_starting(
        self, obs: Observation, power: PowerState, runners_ready: bool | None
    ) -> list[Action]:
        now = obs.now
        if self.starting_since is None:
            self.starting_since = now
        if power == PowerState.ON and (
            self.last_on_at is None or self.last_on_at < self.starting_since
        ):
            self.last_on_at = now
        if power == PowerState.ON and runners_ready:
            return self._become_up(obs, recovered=self.state == ServerState.WAKE_FAILED)

        timeout = timedelta(minutes=self.rules.wake_timeout_minutes)
        if self.state == ServerState.STARTING:
            if now - self.starting_since >= timeout:
                self.state = ServerState.WAKE_FAILED
                self.wake_retry_at = now
                actions = []
                if not self._wake_failed_notified:
                    self._wake_failed_notified = True
                    actions.append(
                        Action(
                            "notify",
                            "wake_failed",
                            {"error": "timeout", "minutes": self.rules.wake_timeout_minutes},
                        )
                    )
                actions.append(Action("wake", self._reason_value(), {**self._dry(), "retry": True}))
                return actions
            return []

        # WAKE_FAILED
        if not self._reason_persists(obs):
            if power == PowerState.OFF:
                self.state = ServerState.OFF
                self.last_off_at = self.last_off_at or now
            return []
        if self.wake_retry_at is None or now - self.wake_retry_at >= timeout:
            self.wake_retry_at = now
            return [Action("wake", self._reason_value(), {**self._dry(), "retry": True})]
        return []

    def _reason_value(self) -> str:
        return (self.last_wake_reason or WakeReason.MANUAL).value

    def _step_up(self, obs: Observation, power: PowerState) -> list[Action]:
        now = obs.now
        actions: list[Action] = []
        if power == PowerState.OFF:
            self.state = ServerState.OFF
            self.last_off_at = now
            self.idle_since = None
            return [Action("notify", "unexpected_off", {"previous_state": "up"})]

        jobs = obs.queued + obs.running
        if jobs > 0:
            self.state = ServerState.ACTIVE
            self.worked_since_wake = True
            self.idle_since = None
        else:
            self.state = ServerState.READY
            self._queue_alert_sent = False
            if self.idle_since is None:
                self.idle_since = now

        # queue_stuck: a matching job waits although the server is up and ready
        if obs.queued == 0:
            self._queue_alert_sent = False
        elif obs.oldest_queued_at is not None and not self._queue_alert_sent:
            since = max(obs.oldest_queued_at, self.ready_at or obs.oldest_queued_at)
            if now - since >= timedelta(minutes=self.rules.queue_alert_minutes):
                self._queue_alert_sent = True
                actions.append(
                    Action(
                        "notify",
                        "queue_stuck",
                        {"queued": obs.queued, "waiting_since": since.isoformat()},
                    )
                )

        if self.state == ServerState.READY:
            actions += self._maybe_shutdown(obs, power)
        return actions

    def _maybe_shutdown(self, obs: Observation, power: PowerState) -> list[Action]:
        now = obs.now
        at = self.shutdown_at
        blockers: list[str] = []
        if not self.rules.automatic:
            blockers.append("automatic_off")
        if self.keep_awake_until is not None and self.keep_awake_until > now:
            blockers.append("keep_awake")
        guard = timedelta(minutes=self.rules.schedule_guard_minutes)
        if obs.prewake_due or (
            obs.next_schedule_at is not None and obs.next_schedule_at - now < guard
        ):
            blockers.append("schedule_soon")
        if self.last_on_at is not None and now - self.last_on_at < timedelta(
            minutes=self.rules.min_uptime_minutes
        ):
            blockers.append("min_uptime")
        if not obs.github_ok:
            blockers.append("github_unavailable")
        if power == PowerState.UNKNOWN:
            blockers.append("power_unknown")
        if self.starts_today >= self.rules.max_starts_per_day:
            blockers.append("start_limit")
        if at is not None and now < at:
            blockers.append("idle_timer")
        self.blockers = blockers
        if blockers:
            return []
        return self._begin_shutdown(now, "idle")

    def _begin_shutdown(self, now: datetime, reason: str) -> list[Action]:
        self.state = ServerState.SHUTTING_DOWN
        self.shutdown_requested_at = now
        self.shutdown_accepted = False
        self._stuck_notified = False
        self.idle_since = None
        if self.rules.dry_run:
            self.shutdown_accepted = True
            self.simulated_power = PowerState.OFF
        return [Action("shutdown", reason, self._dry())]

    def _step_shutting_down(self, obs: Observation, power: PowerState) -> list[Action]:
        now = obs.now
        if power == PowerState.OFF:
            self.state = ServerState.OFF
            self.last_off_at = now
            self.shutdown_requested_at = None
            return [Action("event", "shut_down", {})]
        if power != PowerState.ON or self.shutdown_requested_at is None:
            return []
        if now - self.shutdown_requested_at >= timedelta(minutes=SHUTDOWN_STUCK_MINUTES):
            actions: list[Action] = []
            if not self._stuck_notified:
                self._stuck_notified = True
                actions.append(
                    Action(
                        "notify",
                        "shutdown_stuck",
                        {"minutes": SHUTDOWN_STUCK_MINUTES, "accepted": self.shutdown_accepted},
                    )
                )
            # Not going down: fall back so the machine does not dead-lock in SHUTTING_DOWN.
            self._resume_after_refusal(now)
            return actions
        return []

    def _resume_after_refusal(self, now: datetime) -> None:
        self.shutdown_requested_at = None
        self.shutdown_accepted = False
        if self._last_jobs > 0:
            self.state = ServerState.ACTIVE
            self.idle_since = None
        else:
            self.state = ServerState.READY
            self.idle_since = now

    def request_wake(self, now: datetime, reason: WakeReason = WakeReason.MANUAL) -> list[Action]:
        """Wake the server regardless of ``automatic`` (e.g. the "wake" button)."""
        if self.state in (ServerState.OFF, ServerState.UNKNOWN):
            return self._start(now, reason)
        if self.state == ServerState.WAKE_FAILED:
            return self._start(now, reason, count=False)
        return []

    def request_shutdown(self, now: datetime) -> list[Action]:
        """Safe shutdown via the guard; ignores idle timers and ``automatic``, not the guard."""
        if self.state not in (ServerState.READY, ServerState.ACTIVE):
            return []
        return self._begin_shutdown(now, "manual")

    def keep_awake(self, now: datetime, until: datetime) -> None:
        """Keep the server on until ``until`` (a time in the past clears the hold)."""
        self.keep_awake_until = until if until > now else None

    def on_shutdown_result(self, now: datetime, accepted: bool, reason: str | None) -> list[Action]:
        """Report the guard's answer to a ``shutdown`` action."""
        if self.state != ServerState.SHUTTING_DOWN:
            return []
        if accepted:
            self.shutdown_accepted = True
            return []
        self._resume_after_refusal(now)
        return [Action("event", "shutdown_refused", {"reason": reason})]

    def on_wake_result(self, now: datetime, ok: bool, error: str | None) -> list[Action]:
        """Report whether the BMC accepted the power-on command."""
        if ok or self.state != ServerState.STARTING:
            return []
        self.state = ServerState.WAKE_FAILED
        self.wake_retry_at = now
        if self._wake_failed_notified:
            return []
        self._wake_failed_notified = True
        return [Action("notify", "wake_failed", {"error": error})]
