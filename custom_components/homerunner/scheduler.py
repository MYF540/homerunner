"""Pure schedule maths and workflow dispatch with retry (no Home Assistant imports)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .const import (
    CONF_DISPATCH_TOKEN,
    CONF_ENABLED,
    CONF_INPUTS,
    CONF_NAME,
    CONF_PREWAKE,
    CONF_PREWAKE_MINUTES,
    CONF_REF,
    CONF_REPOSITORY,
    CONF_RETRIES,
    CONF_TIME,
    CONF_TIMEZONE,
    CONF_WEEKDAYS,
    CONF_WORKFLOW,
    DEFAULT_PREWAKE,
    DEFAULT_PREWAKE_MINUTES,
    DEFAULT_REF,
    DEFAULT_RETRIES,
    DEFAULT_WEEKDAYS,
)
from .github import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
    GitHubTransientError,
)

DEFAULT_BACKOFF = (30, 60, 120, 240, 480)
MAX_RATE_LIMIT_WAIT = timedelta(minutes=10)


@dataclass(frozen=True)
class Schedule:
    """One workflow dispatch schedule."""

    id: str
    name: str
    repository: str
    workflow: str
    ref: str
    inputs: dict = field(default_factory=dict)
    time: time = time(3, 0)
    weekdays: frozenset[int] = frozenset(range(7))
    timezone: str = "UTC"
    prewake: bool = True
    prewake_minutes: int = DEFAULT_PREWAKE_MINUTES
    retries: int = DEFAULT_RETRIES
    enabled: bool = True
    dispatch_token: str | None = None


def _resolve_local(day: date, at: time, tz: ZoneInfo) -> datetime:
    """Local wall time -> aware UTC instant.

    Ambiguous times (fall back) use the first occurrence (fold=0), so they fire once.
    Nonexistent times (spring forward) fire at the first valid instant after the gap,
    i.e. at the moment the clocks jump.
    """
    naive = datetime.combine(day, at)
    first = naive.replace(tzinfo=tz, fold=0)
    first_utc = first.astimezone(UTC)
    if first_utc.astimezone(tz).replace(tzinfo=None) == naive:
        return first_utc
    # Nonexistent: fold=1 gives an instant before the transition, fold=0 one after it.
    lo = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    hi = first_utc
    if lo > hi:
        lo, hi = hi, lo
    pre = lo.astimezone(tz).utcoffset()
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == pre:
            lo = mid
        else:
            hi = mid
    return hi.replace(microsecond=0)


def next_fire(schedule: Schedule, after: datetime) -> datetime | None:
    """First fire time strictly after ``after`` (aware UTC), or None if never."""
    if not schedule.enabled or not schedule.weekdays:
        return None
    tz = ZoneInfo(schedule.timezone)
    after_utc = after.astimezone(UTC)
    start = after_utc.astimezone(tz).date() - timedelta(days=1)
    for offset in range(0, 16):
        day = start + timedelta(days=offset)
        if day.weekday() not in schedule.weekdays:
            continue
        candidate = _resolve_local(day, schedule.time, tz)
        if candidate > after_utc:
            return candidate
    return None


def prewake_at(schedule: Schedule, fire: datetime) -> datetime | None:
    """When the server should be woken for a fire time (None if pre-wake is off)."""
    if not schedule.prewake:
        return None
    return fire - timedelta(minutes=schedule.prewake_minutes)


@dataclass
class DispatchResult:
    """Outcome of a dispatch attempt series."""

    ok: bool
    status: int | None = None
    run_id: int | None = None
    error: str | None = None
    attempts: int = 0
    auth_failed: bool = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def dispatch_with_retry(
    client: Any,
    schedule: Schedule,
    *,
    fallback_token: str,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    backoff: tuple[float, ...] = DEFAULT_BACKOFF,
    now: Callable[[], datetime] = _utcnow,
) -> DispatchResult:
    """Dispatch the workflow; retry only transient errors (and short rate limits).

    ``schedule.retries`` is the number of retries after the first attempt. 401 sets
    ``auth_failed``; 403/404/422 and every other error fail immediately.
    """
    token = schedule.dispatch_token or fallback_token
    inputs = dict(schedule.inputs) if schedule.inputs else None
    attempts = 0
    while True:
        attempts += 1
        try:
            run_id = await client.dispatch(
                schedule.repository, schedule.workflow, schedule.ref, inputs, token=token
            )
            return DispatchResult(ok=True, status=200 if run_id else 204, run_id=run_id,
                                  attempts=attempts)
        except GitHubAuthError as err:
            return DispatchResult(False, err.status or 401, None, str(err), attempts, True)
        except GitHubRateLimitError as err:
            delay = None
            if err.reset_at is not None:
                wait = err.reset_at - now()
                if wait <= MAX_RATE_LIMIT_WAIT:
                    delay = max(wait.total_seconds(), 0.0) + 1
            if delay is None or attempts > schedule.retries:
                return DispatchResult(False, err.status, None, str(err), attempts)
        except GitHubTransientError as err:
            if attempts > schedule.retries:
                return DispatchResult(False, err.status, None, str(err), attempts)
            delay = backoff[min(attempts - 1, len(backoff) - 1)] if backoff else 0
        except GitHubError as err:
            return DispatchResult(False, err.status, None, str(err), attempts)
        await sleep(delay)


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return time.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ValueError(f"invalid time: {value!r}")


def schedule_from_subentry(subentry_id: str, data: Mapping, default_tz: str) -> Schedule:
    """Build a :class:`Schedule` from a config subentry's data (raises ValueError if invalid)."""
    raw_inputs = data.get(CONF_INPUTS)
    if isinstance(raw_inputs, str):
        raw_inputs = json.loads(raw_inputs) if raw_inputs.strip() else {}
    if raw_inputs is None:
        raw_inputs = {}
    if not isinstance(raw_inputs, Mapping):
        raise ValueError("inputs must be a JSON object")
    weekdays = frozenset(int(d) for d in (data.get(CONF_WEEKDAYS) or DEFAULT_WEEKDAYS))
    if not weekdays <= set(range(7)):
        raise ValueError("weekdays must be 0..6")
    tz_name = data.get(CONF_TIMEZONE) or default_tz or "UTC"
    try:
        ZoneInfo(tz_name)
    except (KeyError, ValueError, OSError) as err:
        raise ValueError(f"unknown timezone: {tz_name}") from err
    at = _parse_time(data.get(CONF_TIME))
    workflow = str(data[CONF_WORKFLOW])
    token = data.get(CONF_DISPATCH_TOKEN) or None
    return Schedule(
        id=subentry_id,
        name=data.get(CONF_NAME) or f"{workflow} {at.strftime('%H:%M')}",
        repository=str(data[CONF_REPOSITORY]),
        workflow=workflow,
        ref=str(data.get(CONF_REF) or DEFAULT_REF),
        inputs=dict(raw_inputs),
        time=at,
        weekdays=weekdays,
        timezone=tz_name,
        prewake=bool(data.get(CONF_PREWAKE, DEFAULT_PREWAKE)),
        prewake_minutes=int(data.get(CONF_PREWAKE_MINUTES, DEFAULT_PREWAKE_MINUTES)),
        retries=int(data.get(CONF_RETRIES, DEFAULT_RETRIES)),
        enabled=bool(data.get(CONF_ENABLED, True)),
        dispatch_token=token,
    )
