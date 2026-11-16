"""Tests for power_manager.py with simulated time."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from custom_components.homerunner.const import PowerState, ServerState, WakeReason
from custom_components.homerunner.power_manager import (
    Observation,
    PowerStateMachine,
    Rules,
)

T0 = datetime(2027, 1, 10, 12, 0, tzinfo=UTC)
ON, OFF, UNK = PowerState.ON, PowerState.OFF, PowerState.UNKNOWN


def mins(n: float) -> timedelta:
    return timedelta(minutes=n)


def rules(**kw) -> Rules:
    base = {"automatic": True, "dry_run": False}
    base.update(kw)
    return Rules(**base)


def obs(now, power=ON, *, ready=True, queued=0, running=0, github_ok=True, next_schedule=None,
        prewake=False, oldest=None, local_date=None) -> Observation:
    return Observation(
        now=now,
        local_date=local_date or now.date(),
        power=power,
        runners_ready=ready,
        queued=queued,
        running=running,
        github_ok=github_ok,
        next_schedule_at=next_schedule,
        prewake_due=prewake,
        oldest_queued_at=oldest,
    )


def kinds(actions):
    return [(a.kind, a.reason) for a in actions]


def machine_off(**kw) -> PowerStateMachine:
    m = PowerStateMachine(rules(**kw))
    m.step(obs(T0, OFF))
    assert m.state == ServerState.OFF
    return m


def machine_ready(worked=False, **kw) -> tuple[PowerStateMachine, datetime]:
    """Machine that has been woken by a job and is READY with idle timer running."""
    m = machine_off(**kw)
    t = T0 + mins(1)
    m.step(obs(t, OFF, queued=1))
    assert m.state == ServerState.STARTING
    t += mins(2)
    m.step(obs(t, ON, queued=1, running=0))  # ready, job waiting -> ACTIVE
    assert m.state == ServerState.ACTIVE
    if not worked:
        m.worked_since_wake = False
    t += mins(1)
    m.step(obs(t, ON))
    assert m.state == ServerState.READY
    return m, t


# ---------------------------------------------------------------- wake


def test_wake_on_queued_job_then_active_ready():
    m = machine_off()
    acts = m.step(obs(T0 + mins(1), OFF, queued=2))
    assert kinds(acts) == [("wake", "queued_job")]
    assert m.state == ServerState.STARTING and m.starts_today == 1
    assert m.last_wake_reason == WakeReason.QUEUED_JOB
    # power on but runners not ready yet
    assert m.step(obs(T0 + mins(3), ON, ready=False, queued=2)) == []
    assert m.state == ServerState.STARTING
    acts = m.step(obs(T0 + mins(4), ON, ready=True, queued=2))
    assert kinds(acts) == [("event", "woke")]
    assert m.state == ServerState.ACTIVE and m.worked_since_wake
    acts = m.step(obs(T0 + mins(10), ON, queued=0, running=0))
    assert m.state == ServerState.READY and m.idle_since == T0 + mins(10)


def test_no_automatic_wake_when_automatic_off_but_manual_works():
    m = machine_off(automatic=False)
    assert m.step(obs(T0 + mins(1), OFF, queued=3, prewake=True)) == []
    assert m.state == ServerState.OFF
    acts = m.request_wake(T0 + mins(2))
    assert kinds(acts) == [("wake", "manual")]
    assert m.state == ServerState.STARTING


def test_wake_for_schedule():
    m = machine_off()
    acts = m.step(obs(T0 + mins(1), OFF, prewake=True))
    assert kinds(acts) == [("wake", "schedule")]


def test_prewake_without_job_uses_short_idle():
    m = machine_off(min_uptime_minutes=5)
    t = T0 + mins(1)
    m.step(obs(t, OFF, prewake=True))
    t += mins(2)
    m.step(obs(t, ON))  # ready, idle starts
    ready_at = t
    assert m.state == ServerState.READY and not m.worked_since_wake
    assert m.shutdown_at == ready_at + mins(20)
    # prewake still due -> guard keeps it up
    assert m.step(obs(ready_at + mins(25), ON, next_schedule=ready_at + mins(30))) == []
    acts = m.step(obs(ready_at + mins(61), ON))
    assert kinds(acts) == [("shutdown", "idle")]


# ---------------------------------------------------------------- idle timing


def test_long_vs_short_idle():
    m, t = machine_ready(worked=True)
    assert m.worked_since_wake
    assert m.shutdown_at == t + timedelta(hours=2)
    assert m.step(obs(t + timedelta(hours=1, minutes=59))) == []
    acts = m.step(obs(t + timedelta(hours=2)))
    assert kinds(acts) == [("shutdown", "idle")]
    assert m.state == ServerState.SHUTTING_DOWN

    m2, t2 = machine_ready(worked=False)
    assert m2.shutdown_at == t2 + mins(20)
    assert m2.step(obs(t2 + mins(19))) == []
    assert kinds(m2.step(obs(t2 + mins(20)))) == [("shutdown", "idle")]


def test_new_job_restarts_idle_timer():
    m, t = machine_ready(worked=True)
    m.step(obs(t + mins(100), ON, running=1))
    assert m.state == ServerState.ACTIVE and m.idle_since is None and m.shutdown_at is None
    t2 = t + mins(110)
    m.step(obs(t2, ON))
    assert m.idle_since == t2 and m.shutdown_at == t2 + timedelta(hours=2)


def test_idle_hours_change_at_runtime():
    m, t = machine_ready(worked=True)
    assert m.shutdown_at == t + timedelta(hours=2)
    m.rules.idle_hours = 0.5
    assert m.shutdown_at == t + mins(30)
    assert kinds(m.step(obs(t + mins(30)))) == [("shutdown", "idle")]
    m2, t2 = machine_ready(worked=True)
    m2.rules.idle_hours = 4
    assert m2.step(obs(t2 + timedelta(hours=3))) == []
    assert m2.shutdown_at == t2 + timedelta(hours=4)


# ---------------------------------------------------------------- blockers


def long_idle(**kw):
    m, t = machine_ready(worked=True, **kw)
    return m, t + timedelta(hours=3)


def test_blocker_automatic_off():
    m, t = long_idle()
    m.rules.automatic = False
    assert m.step(obs(t)) == []
    assert "automatic_off" in m.blockers and m.state == ServerState.READY
    m.rules.automatic = True
    assert kinds(m.step(obs(t + mins(1)))) == [("shutdown", "idle")]


def test_blocker_keep_awake():
    m, t = long_idle()
    m.keep_awake(t, t + timedelta(hours=1))
    assert m.shutdown_at == t + timedelta(hours=1)
    assert m.step(obs(t + mins(59))) == []
    assert "keep_awake" in m.blockers
    assert kinds(m.step(obs(t + mins(60)))) == [("shutdown", "idle")]


def test_blocker_schedule_within_guard():
    m, t = long_idle()
    assert m.step(obs(t, next_schedule=t + mins(29))) == []
    assert "schedule_soon" in m.blockers
    assert m.step(obs(t, prewake=True)) == []
    assert kinds(m.step(obs(t, next_schedule=t + mins(31)))) == [("shutdown", "idle")]


def test_blocker_min_uptime():
    m = machine_off(min_uptime_minutes=60, short_idle_minutes=5)
    t = T0 + mins(1)
    m.step(obs(t, OFF, prewake=True))
    m.step(obs(t + mins(1), ON))
    on_at = m.last_on_at
    assert m.shutdown_at == on_at + mins(60)
    assert m.step(obs(t + mins(30), ON)) == []
    assert kinds(m.step(obs(on_at + mins(60), ON))) == [("shutdown", "idle")]


def test_blocker_github_not_ok():
    m, t = long_idle()
    assert m.step(obs(t, github_ok=False)) == []
    assert "github_unavailable" in m.blockers
    assert kinds(m.step(obs(t + mins(1)))) == [("shutdown", "idle")]


def test_blocker_unknown_power():
    m, t = long_idle()
    assert m.step(obs(t, UNK)) == []
    assert "power_unknown" in m.blockers and m.state == ServerState.READY
    assert kinds(m.step(obs(t + mins(1), ON))) == [("shutdown", "idle")]


def test_manual_shutdown_ignores_timers_and_automatic():
    m, t = machine_ready(worked=True)
    m.rules.automatic = False
    acts = m.request_shutdown(t + mins(1))
    assert kinds(acts) == [("shutdown", "manual")]
    assert m.state == ServerState.SHUTTING_DOWN
    assert m.request_shutdown(t + mins(2)) == []


# ---------------------------------------------------------------- shutdown flow


def test_guard_refuses_back_to_ready_and_timer_restarted():
    m, t = long_idle()
    assert kinds(m.step(obs(t))) == [("shutdown", "idle")]
    acts = m.on_shutdown_result(t, False, "job_running")
    assert kinds(acts) == [("event", "shutdown_refused")]
    assert acts[0].detail["reason"] == "job_running"
    assert m.state == ServerState.READY and m.idle_since == t
    # does not hammer the guard
    assert m.step(obs(t + timedelta(hours=1))) == []
    assert kinds(m.step(obs(t + timedelta(hours=2)))) == [("shutdown", "idle")]


def test_guard_refuses_with_jobs_seen_goes_active():
    m, t = long_idle()
    m.step(obs(t))
    m.on_shutdown_result(t, False, "job_running")
    assert m.state == ServerState.READY
    m.step(obs(t + mins(1), running=1))
    assert m.state == ServerState.ACTIVE


def test_shutdown_accepted_then_off():
    m, t = long_idle()
    m.step(obs(t))
    assert m.on_shutdown_result(t, True, None) == []
    assert m.state == ServerState.SHUTTING_DOWN
    assert m.step(obs(t + mins(2), ON)) == []
    acts = m.step(obs(t + mins(3), OFF))
    assert kinds(acts) == [("event", "shut_down")]
    assert m.state == ServerState.OFF and m.last_off_at == t + mins(3)


def test_shutdown_stuck_notified_once():
    m, t = long_idle()
    m.step(obs(t))
    m.on_shutdown_result(t, True, None)
    assert m.step(obs(t + mins(14), ON)) == []
    acts = m.step(obs(t + mins(15), ON))
    assert kinds(acts) == [("notify", "shutdown_stuck")]
    assert all(a.reason != "shutdown_stuck" for a in m.step(obs(t + mins(40), ON)))


def test_unexpected_off():
    m, t = machine_ready(worked=True)
    acts = m.step(obs(t + mins(5), OFF))
    assert kinds(acts) == [("notify", "unexpected_off")]
    assert m.state == ServerState.OFF and m.last_off_at == t + mins(5)
    # also from ACTIVE
    m2, t2 = machine_ready()
    m2.step(obs(t2 + mins(1), ON, running=1))
    assert kinds(m2.step(obs(t2 + mins(2), OFF, running=1))) == [("notify", "unexpected_off")]


# ---------------------------------------------------------------- start limit


def cycle(m, t, queued=1):
    """One full wake -> ready -> job gone cycle; returns time after it."""
    m.state = ServerState.OFF
    acts = m.step(obs(t, OFF, queued=queued))
    return acts


def test_start_limit_blocks_shutdown_and_resets_at_local_midnight():
    m = machine_off(max_starts_per_day=2)
    t = T0
    all_acts = []
    for _ in range(2):
        t += mins(5)
        all_acts += m.step(obs(t, OFF, queued=1))
        t += mins(2)
        m.step(obs(t, ON, queued=1))
        m.state = ServerState.OFF  # simulate it went off between starts
        m.last_off_at = t
    assert m.starts_today == 2
    assert [a.reason for a in all_acts if a.kind == "notify"] == ["start_limit"]
    # a third automatic wake is still allowed (a waiting job must run)
    t += mins(5)
    acts = m.step(obs(t, OFF, queued=1))
    assert kinds(acts) == [("wake", "queued_job")] and m.starts_today == 3
    # no automatic shutdown while limit hit, even after a long idle
    m.step(obs(t + mins(2), ON, queued=1))
    m.step(obs(t + mins(3), ON))
    late = t + timedelta(hours=6)
    assert m.step(obs(late, ON)) == []
    assert "start_limit" in m.blockers and m.state == ServerState.READY
    # local midnight passes (local_date differs although UTC date is the same)
    nxt = date(2027, 1, 11)
    acts = m.step(obs(late + mins(1), ON, local_date=nxt))
    assert m.starts_today == 0
    assert kinds(acts) == [("shutdown", "idle")]


def test_start_limit_notified_once_per_day():
    m = machine_off(max_starts_per_day=1)
    a1 = m.step(obs(T0 + mins(1), OFF, queued=1))
    assert ("notify", "start_limit") in kinds(a1)
    m.step(obs(T0 + mins(3), ON, queued=1))
    m.state = ServerState.OFF
    a2 = m.step(obs(T0 + mins(10), OFF, queued=1))
    assert ("notify", "start_limit") not in kinds(a2)
    m.state = ServerState.OFF
    a3 = m.step(obs(T0 + mins(10), OFF, queued=1, local_date=date(2027, 1, 11)))
    assert ("notify", "start_limit") in kinds(a3)  # next day, one start again hits max 1


# ---------------------------------------------------------------- forced wake


def test_forced_wake_after_days_off_and_hold():
    m = machine_off(forced_wake_days=5, forced_wake_minutes=30, min_uptime_minutes=5)
    assert m.step(obs(T0 + timedelta(days=4, hours=23), OFF)) == []
    t = T0 + timedelta(days=5)
    acts = m.step(obs(t, OFF))
    assert kinds(acts) == [("wake", "forced_wake")]
    assert m.last_wake_reason == WakeReason.FORCED
    t += mins(2)
    m.step(obs(t, ON))
    on_at = m.last_on_at
    assert m.state == ServerState.READY
    # short idle is 20 min but forced hold is 30 min after on
    assert m.shutdown_at == on_at + mins(30)
    assert m.step(obs(on_at + mins(29), ON)) == []
    assert kinds(m.step(obs(on_at + mins(30), ON))) == [("shutdown", "idle")]


def test_forced_wake_not_when_automatic_off():
    m = machine_off(automatic=False)
    assert m.step(obs(T0 + timedelta(days=30), OFF)) == []


# ---------------------------------------------------------------- wake timeout


def test_wake_timeout_failed_retry_and_recovery():
    m = machine_off(wake_timeout_minutes=10)
    t = T0 + mins(1)
    m.step(obs(t, OFF, queued=1))
    assert m.step(obs(t + mins(9), OFF, queued=1)) == []
    acts = m.step(obs(t + mins(10), OFF, queued=1))
    assert kinds(acts) == [("notify", "wake_failed"), ("wake", "queued_job")]
    assert acts[1].detail.get("retry") is True
    assert m.state == ServerState.WAKE_FAILED
    # retry once per timeout period, notification only once
    assert m.step(obs(t + mins(15), OFF, queued=1)) == []
    acts = m.step(obs(t + mins(20), OFF, queued=1))
    assert kinds(acts) == [("wake", "queued_job")]
    # recovery
    acts = m.step(obs(t + mins(23), ON, queued=1, ready=True))
    assert kinds(acts) == [("event", "woke")] and acts[0].detail["recovered"] is True
    assert m.state == ServerState.ACTIVE


def test_wake_failed_gives_up_when_reason_gone():
    m = machine_off(wake_timeout_minutes=10)
    t = T0 + mins(1)
    m.step(obs(t, OFF, queued=1))
    m.step(obs(t + mins(10), OFF, queued=1))
    assert m.state == ServerState.WAKE_FAILED
    assert m.step(obs(t + mins(21), OFF, queued=0)) == []
    assert m.state == ServerState.OFF


def test_on_wake_result_failure():
    m = machine_off()
    m.step(obs(T0 + mins(1), OFF, queued=1))
    acts = m.on_wake_result(T0 + mins(1), False, "bmc down")
    assert kinds(acts) == [("notify", "wake_failed")] and acts[0].detail["error"] == "bmc down"
    assert m.state == ServerState.WAKE_FAILED
    assert m.on_wake_result(T0, True, None) == []


# ---------------------------------------------------------------- queue stuck


def test_queue_stuck_alert_once_per_burst():
    m, t = machine_ready(worked=True, queue_alert_minutes=15)
    o = lambda now, q: obs(now, ON, queued=q, oldest=t + mins(1) if q else None)  # noqa: E731
    assert m.step(o(t + mins(10), 1)) == []
    acts = m.step(o(t + mins(16), 1))
    assert kinds(acts) == [("notify", "queue_stuck")]
    assert m.step(o(t + mins(40), 1)) == []
    m.step(o(t + mins(50), 0))
    acts = m.step(obs(t + mins(100), ON, queued=1, oldest=t + mins(80)))
    assert kinds(acts) == [("notify", "queue_stuck")]


def test_queue_stuck_age_counted_from_ready():
    m = machine_off()
    m.step(obs(T0 + mins(1), OFF, queued=1))
    old = T0 - timedelta(hours=3)
    acts = m.step(obs(T0 + mins(5), ON, queued=1, oldest=old))
    assert kinds(acts) == [("event", "woke")]
    assert m.step(obs(T0 + mins(10), ON, queued=1, oldest=old)) == []
    assert kinds(m.step(obs(T0 + mins(20), ON, queued=1, oldest=old))) == [
        ("notify", "queue_stuck")
    ]


# ---------------------------------------------------------------- manual power on


def test_manual_power_on_detected():
    m = machine_off()
    acts = m.step(obs(T0 + mins(5), ON, ready=False))
    assert acts == []
    assert m.state == ServerState.STARTING and m.last_wake_reason == WakeReason.MANUAL
    assert m.starts_today == 1
    acts = m.step(obs(T0 + mins(8), ON))
    assert kinds(acts) == [("event", "woke")]
    assert m.state == ServerState.READY and not m.worked_since_wake
    assert m.shutdown_at == T0 + mins(8) + mins(20)


def test_first_step_adopts_running_server():
    m = PowerStateMachine(rules())
    m.step(obs(T0, ON, ready=True))
    assert m.state == ServerState.READY
    m = PowerStateMachine(rules())
    m.step(obs(T0, ON, ready=True, running=1))
    assert m.state == ServerState.ACTIVE
    m = PowerStateMachine(rules())
    m.step(obs(T0, OFF))
    assert m.state == ServerState.OFF
    m = PowerStateMachine(rules())
    m.step(obs(T0, UNK))
    assert m.state == ServerState.UNKNOWN


# ---------------------------------------------------------------- dry run


def test_dry_run_simulation():
    m = PowerStateMachine(rules(dry_run=True, min_uptime_minutes=5))
    m.step(obs(T0, OFF))
    assert m.state == ServerState.OFF
    acts = m.step(obs(T0 + mins(1), OFF, queued=1))
    assert kinds(acts) == [("wake", "queued_job")] and acts[0].detail["dry_run"] is True
    assert m.simulated_power == ON
    # real BMC still says OFF and guard not ready: simulated power rules
    acts = m.step(obs(T0 + mins(2), OFF, ready=None, queued=1))
    assert kinds(acts) == [("event", "woke")] and m.state == ServerState.ACTIVE
    m.step(obs(T0 + mins(3), OFF, ready=None, running=0))
    assert m.state == ServerState.READY
    late = T0 + timedelta(hours=3)
    acts = m.step(obs(late, OFF, ready=None))
    assert kinds(acts) == [("shutdown", "idle")] and acts[0].detail["dry_run"] is True
    assert m.state == ServerState.SHUTTING_DOWN and m.simulated_power == OFF
    acts = m.step(obs(late + mins(1), ON))  # real power irrelevant
    assert kinds(acts) == [("event", "shut_down")] and m.state == ServerState.OFF


def test_dry_run_off_with_real_server_on_no_manual_detection():
    m = PowerStateMachine(rules(dry_run=True))
    m.step(obs(T0, OFF))
    m.step(obs(T0 + mins(1), ON))
    assert m.state == ServerState.OFF


def test_live_wake_has_no_dry_flag():
    m = machine_off()
    acts = m.step(obs(T0 + mins(1), OFF, queued=1))
    assert "dry_run" not in acts[0].detail


# ---------------------------------------------------------------- persistence


def test_persistence_round_trip():
    m, t = machine_ready(worked=True)
    m.keep_awake(t, t + timedelta(hours=1))
    m.step(obs(t + mins(1), ON))
    data = m.as_dict()
    data = json.loads(json.dumps(data))  # JSON safe
    r = PowerStateMachine(rules(), data)
    assert r.as_dict() == m.as_dict()
    assert r.state == ServerState.READY and r.idle_since == m.idle_since
    assert r.shutdown_at == m.shutdown_at and r.starts_today == m.starts_today
    assert r.last_wake_reason == WakeReason.QUEUED_JOB and r.keep_awake_until == m.keep_awake_until


def test_restore_robust_to_garbage():
    garbage = {
        "state": "bogus",
        "idle_since": "not a date",
        "last_wake_reason": 42,
        "starts_today": "x",
        "starts_date": 5,
        "keep_awake_until": "2027-01-01T00:00:00",  # naive -> rejected
        "last_on_at": None,
        "simulated_power": "weird",
        "last_jobs": -3,
    }
    m = PowerStateMachine(rules(), garbage)
    assert m.state == ServerState.UNKNOWN and m.idle_since is None
    assert m.starts_today == 0 and m.keep_awake_until is None and m.last_wake_reason is None
    m.step(obs(T0, OFF))
    assert m.state == ServerState.OFF
    for stored in ({}, None, {"state": None}, {"unknown": 1}):
        PowerStateMachine(rules(), stored).as_dict()


def test_restored_starting_without_timestamp_does_not_crash():
    m = PowerStateMachine(rules(), {"state": "starting"})
    m.step(obs(T0, ON, ready=False))
    assert m.starting_since == T0
    m.step(obs(T0 + mins(11), ON, ready=False))
    assert m.state == ServerState.WAKE_FAILED
