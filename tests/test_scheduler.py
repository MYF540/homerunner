"""Tests for scheduler.py."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.homerunner.github import (
    GitHubAuthError,
    GitHubForbiddenError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
    GitHubValidationError,
)
from custom_components.homerunner.scheduler import (
    Schedule,
    dispatch_with_retry,
    next_fire,
    prewake_at,
    schedule_from_subentry,
)


def sched(**kw) -> Schedule:
    base = {
        "id": "s1",
        "name": "night",
        "repository": "org/repo",
        "workflow": "night.yml",
        "ref": "dev",
        "inputs": {"planned": "true"},
        "time": time(3, 0),
        "weekdays": frozenset(range(7)),
        "timezone": "Europe/Berlin",
        "prewake": True,
        "prewake_minutes": 15,
        "retries": 5,
        "enabled": True,
        "dispatch_token": None,
    }
    base.update(kw)
    return Schedule(**base)


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_next_fire_other_timezone():
    # 03:00 Berlin in January is 02:00 UTC
    assert next_fire(sched(), utc(2027, 1, 10, 0, 0)) == utc(2027, 1, 10, 2, 0)
    # strictly after: exactly at the fire time -> next day
    assert next_fire(sched(), utc(2027, 1, 10, 2, 0)) == utc(2027, 1, 11, 2, 0)
    # summer time: 01:00 UTC
    assert next_fire(sched(), utc(2027, 7, 1, 0, 0)) == utc(2027, 7, 1, 1, 0)


def test_next_fire_utc_zone_and_new_york():
    assert next_fire(sched(timezone="UTC"), utc(2027, 1, 10, 0, 0)) == utc(2027, 1, 10, 3, 0)
    assert next_fire(sched(timezone="America/New_York"), utc(2027, 1, 10, 0, 0)) == utc(
        2027, 1, 10, 8, 0
    )


def test_dst_spring_forward_nonexistent_time():
    s = sched(time=time(2, 30))
    # 2027-03-28 02:00 -> 03:00 in Berlin; 02:30 does not exist -> fire at the jump (01:00Z)
    assert next_fire(s, utc(2027, 3, 27, 12, 0)) == utc(2027, 3, 28, 1, 0)
    # next day normal again: 02:30 CEST = 00:30Z
    assert next_fire(s, utc(2027, 3, 28, 1, 0)) == utc(2027, 3, 29, 0, 30)


def test_dst_fall_back_ambiguous_fires_once():
    s = sched(time=time(2, 30))
    first = next_fire(s, utc(2026, 10, 24, 12, 0))
    assert first == utc(2026, 10, 25, 0, 30)  # first occurrence (CEST)
    # the second 02:30 (CET = 01:30Z) must not fire again
    assert next_fire(s, first) == utc(2026, 10, 26, 1, 30)


def test_weekdays_filter():
    s = sched(weekdays=frozenset({0, 4}), timezone="UTC")  # Mon, Fri
    # 2027-01-10 is a Sunday
    assert next_fire(s, utc(2027, 1, 10, 0, 0)) == utc(2027, 1, 11, 3, 0)
    assert next_fire(s, utc(2027, 1, 11, 3, 0)) == utc(2027, 1, 15, 3, 0)


def test_disabled_or_no_weekdays():
    assert next_fire(sched(enabled=False), utc(2027, 1, 1)) is None
    assert next_fire(sched(weekdays=frozenset()), utc(2027, 1, 1)) is None


def test_no_catch_up_after_restart():
    # HA was down over 03:00 and starts at 05:00 -> next fire is tomorrow
    s = sched(timezone="UTC")
    assert next_fire(s, utc(2027, 1, 10, 5, 0)) == utc(2027, 1, 11, 3, 0)


def test_prewake():
    s = sched()
    fire = utc(2027, 1, 10, 2, 0)
    assert prewake_at(s, fire) == fire - timedelta(minutes=15)
    assert prewake_at(sched(prewake=False), fire) is None


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def dispatch(self, repo, workflow, ref, inputs, token=None):
        self.calls.append((repo, workflow, ref, inputs, token))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class Sleeper:
    def __init__(self):
        self.delays = []

    async def __call__(self, d):
        self.delays.append(d)


async def test_dispatch_ok_run_id_passthrough():
    c = FakeClient([4711])
    res = await dispatch_with_retry(c, sched(), fallback_token="fb", sleep=Sleeper())
    assert res.ok and res.run_id == 4711 and res.attempts == 1 and not res.auth_failed
    assert c.calls == [("org/repo", "night.yml", "dev", {"planned": "true"}, "fb")]


async def test_dispatch_204_no_run_id_and_own_token():
    c = FakeClient([None])
    res = await dispatch_with_retry(
        c, sched(dispatch_token="own"), fallback_token="fb", sleep=Sleeper()
    )
    assert res.ok and res.run_id is None and res.status == 204
    assert c.calls[0][4] == "own"


async def test_retry_transient_with_backoff():
    err = GitHubTransientError("boom", 502)
    c = FakeClient([err, err, err, 7])
    sl = Sleeper()
    res = await dispatch_with_retry(c, sched(), fallback_token="fb", sleep=sl)
    assert res.ok and res.attempts == 4 and res.run_id == 7
    assert sl.delays == [30, 60, 120]


async def test_retry_exhausted():
    err = GitHubTransientError("boom", 503)
    c = FakeClient([err] * 10)
    sl = Sleeper()
    res = await dispatch_with_retry(c, sched(retries=3), fallback_token="fb", sleep=sl)
    assert not res.ok and res.attempts == 4 and res.status == 503
    assert sl.delays == [30, 60, 120]


async def test_backoff_sequence_full():
    err = GitHubTransientError("x")
    c = FakeClient([err] * 7)
    sl = Sleeper()
    res = await dispatch_with_retry(c, sched(retries=6), fallback_token="fb", sleep=sl)
    assert not res.ok and res.attempts == 7
    assert sl.delays == [30, 60, 120, 240, 480, 480]


async def test_auth_failed_no_retry():
    c = FakeClient([GitHubAuthError("bad", 401)])
    sl = Sleeper()
    res = await dispatch_with_retry(c, sched(), fallback_token="fb", sleep=sl)
    assert not res.ok and res.auth_failed and res.status == 401 and res.attempts == 1
    assert sl.delays == []


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (GitHubValidationError("no", 422), 422),
        (GitHubNotFoundError("no", 404), 404),
        (GitHubForbiddenError("no", 403), 403),
    ],
)
async def test_no_retry_on_permanent_errors(exc, status):
    c = FakeClient([exc, 1])
    sl = Sleeper()
    res = await dispatch_with_retry(c, sched(), fallback_token="fb", sleep=sl)
    assert not res.ok and res.status == status and res.attempts == 1 and not res.auth_failed
    assert sl.delays == [] and len(c.calls) == 1


async def test_rate_limit_short_reset_is_retried_long_is_not():
    now = utc(2027, 1, 1, 12, 0)
    short = GitHubRateLimitError("rl", 403, now + timedelta(minutes=2))
    c = FakeClient([short, 9])
    sl = Sleeper()
    res = await dispatch_with_retry(c, sched(), fallback_token="fb", sleep=sl, now=lambda: now)
    assert res.ok and sl.delays == [121]

    long = GitHubRateLimitError("rl", 403, now + timedelta(minutes=30))
    c = FakeClient([long, 9])
    res = await dispatch_with_retry(c, sched(), fallback_token="fb", sleep=Sleeper(), now=lambda: now)
    assert not res.ok and res.attempts == 1

    c = FakeClient([GitHubRateLimitError("rl", 429, None), 9])
    res = await dispatch_with_retry(c, sched(), fallback_token="fb", sleep=Sleeper(), now=lambda: now)
    assert not res.ok and res.attempts == 1


def test_schedule_from_subentry_defaults_and_json_inputs():
    s = schedule_from_subentry(
        "abc",
        {
            "repository": "o/r",
            "workflow": "w.yml",
            "inputs": '{"a": "b"}',
            "time": "03:00",
        },
        "Europe/Berlin",
    )
    assert s.id == "abc" and s.ref == "main" and s.inputs == {"a": "b"}
    assert s.time == time(3, 0) and s.weekdays == frozenset(range(7))
    assert s.timezone == "Europe/Berlin" and s.prewake and s.prewake_minutes == 15
    assert s.retries == 5 and s.enabled and s.dispatch_token is None


def test_schedule_from_subentry_full():
    s = schedule_from_subentry(
        "x",
        {
            "name": "N",
            "repository": "o/r",
            "workflow": "w.yml",
            "ref": "dev",
            "inputs": {"k": 1},
            "time": "04:15:00",
            "weekdays": [0, 2],
            "timezone": "UTC",
            "prewake": False,
            "prewake_minutes": 5,
            "retries": 2,
            "dispatch_token": "tok",
            "enabled": False,
        },
        "Europe/Berlin",
    )
    assert s.name == "N" and s.inputs == {"k": 1} and s.time == time(4, 15)
    assert s.weekdays == frozenset({0, 2}) and s.timezone == "UTC" and not s.prewake
    assert s.retries == 2 and s.dispatch_token == "tok" and not s.enabled


@pytest.mark.parametrize(
    "bad",
    [
        {"inputs": "{not json", "time": "03:00"},
        {"inputs": "[1]", "time": "03:00"},
        {"time": "25:99"},
        {"time": "03:00", "timezone": "Mars/Base"},
        {"time": "03:00", "weekdays": [9]},
    ],
)
def test_schedule_from_subentry_invalid(bad):
    data = {"repository": "o/r", "workflow": "w.yml", **bad}
    with pytest.raises(ValueError):
        schedule_from_subentry("x", data, "UTC")
