"""Fires workflow dispatches at the configured times and tracks their outcome."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_DISPATCH_FAILED,
    EVENT_DISPATCHED,
    SUBENTRY_SCHEDULE,
)
from .github import GitHubClient, GitHubError
from .scheduler import (
    DispatchResult,
    Schedule,
    dispatch_with_retry,
    next_fire,
    prewake_at,
    schedule_from_subentry,
)

_LOGGER = logging.getLogger(__name__)

RUN_FETCH_DELAY = 120
RUN_REFETCH_DELAY = 600
RUN_FETCH_MAX_ATTEMPTS = 36

ISSUE_TOKEN_INVALID = "token_invalid"


@dataclass
class ScheduleState:
    """What is known about the last dispatch of a schedule (persisted)."""

    last_dispatch_at: datetime | None = None
    ok: bool | None = None
    status: int | None = None
    run_id: int | None = None
    error: str | None = None
    attempts: int = 0
    run_status: str | None = None
    run_conclusion: str | None = None
    run_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_dispatch_at": self.last_dispatch_at.isoformat()
            if self.last_dispatch_at
            else None,
            "ok": self.ok,
            "status": self.status,
            "run_id": self.run_id,
            "error": self.error,
            "attempts": self.attempts,
            "run_status": self.run_status,
            "run_conclusion": self.run_conclusion,
            "run_url": self.run_url,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ScheduleState:
        if not isinstance(raw, dict):
            return cls()
        at = None
        if isinstance(raw.get("last_dispatch_at"), str):
            at = dt_util.parse_datetime(raw["last_dispatch_at"])
        return cls(
            last_dispatch_at=at,
            ok=raw.get("ok"),
            status=raw.get("status"),
            run_id=raw.get("run_id"),
            error=raw.get("error"),
            attempts=raw.get("attempts") or 0,
            run_status=raw.get("run_status"),
            run_conclusion=raw.get("run_conclusion"),
            run_url=raw.get("run_url"),
        )


class ScheduleRunner:
    """Owns the timers of all schedule subentries."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        github: GitHubClient,
        fallback_token: str,
        on_change: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._github = github
        self._token = fallback_token
        self._on_change = on_change
        self.schedules: dict[str, Schedule] = {}
        self.states: dict[str, ScheduleState] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._fire_at: dict[str, datetime] = {}
        self._run_timers: dict[str, CALLBACK_TYPE] = {}
        self._stopped = False

    # ------------------------------------------------------------------ state

    def load(self, stored: Any) -> None:
        """Restore persisted per-schedule results."""
        if isinstance(stored, dict):
            self.states = {sid: ScheduleState.from_dict(raw) for sid, raw in stored.items()}

    def dump(self) -> dict[str, Any]:
        return {sid: st.as_dict() for sid, st in self.states.items()}

    def state_of(self, schedule_id: str) -> ScheduleState:
        return self.states.setdefault(schedule_id, ScheduleState())

    def fire_time(self, schedule_id: str) -> datetime | None:
        return self._fire_at.get(schedule_id)

    # ------------------------------------------------------------------ timers

    @callback
    def sync(self) -> None:
        """(Re)build the schedules from the subentries and arm the timers.

        There is no catch-up: the next fire is always computed from "now".
        """
        schedules: dict[str, Schedule] = {}
        for sub in self._entry.subentries.values():
            if sub.subentry_type != SUBENTRY_SCHEDULE:
                continue
            try:
                schedules[sub.subentry_id] = schedule_from_subentry(
                    sub.subentry_id,
                    {**sub.data, "name": sub.data.get("name") or sub.title},
                    self._hass.config.time_zone,
                )
            except (ValueError, KeyError, TypeError) as err:
                _LOGGER.warning("Ignoring invalid schedule '%s': %s", sub.title, err)
        old = self.schedules
        self.schedules = schedules
        for sid in list(self._timers):
            if sid not in schedules or old.get(sid) != schedules[sid]:
                self._disarm(sid)
        for sid in list(self._run_timers):
            if sid not in schedules:
                self._run_timers.pop(sid)()
        for sid in schedules:
            if sid not in self._timers:
                self._arm(sid, dt_util.utcnow())
        for sid in list(self._fire_at):
            if sid not in self._timers:
                self._fire_at.pop(sid, None)
        self._on_change()

    def _disarm(self, sid: str) -> None:
        unsub = self._timers.pop(sid, None)
        if unsub:
            unsub()
        self._fire_at.pop(sid, None)

    def _arm(self, sid: str, after: datetime) -> None:
        self._disarm(sid)
        schedule = self.schedules.get(sid)
        if schedule is None or self._stopped:
            return
        fire = next_fire(schedule, after)
        if fire is None:
            return
        self._fire_at[sid] = fire

        @callback
        def _due(now: datetime) -> None:
            self._fire(sid, fire, now)

        self._timers[sid] = async_track_point_in_utc_time(self._hass, _due, fire)

    @callback
    def _fire(self, sid: str, fire: datetime, now: datetime) -> None:
        self._timers.pop(sid, None)
        schedule = self.schedules.get(sid)
        # Never recompute from before the fire time, otherwise it would fire twice.
        self._arm(sid, max(now, fire))
        if schedule is not None and schedule.enabled:
            self.dispatch_background(schedule)
        self._on_change()

    def prewake_info(self, now: datetime) -> tuple[bool, datetime | None]:
        """(a pre-wake window is open, next fire of any enabled schedule)."""
        due = False
        nxt: datetime | None = None
        for schedule in self.schedules.values():
            if not schedule.enabled:
                continue
            fire = next_fire(schedule, now)
            if fire is None:
                continue
            if nxt is None or fire < nxt:
                nxt = fire
            wake = prewake_at(schedule, fire)
            if wake is not None and wake <= now:
                due = True
        return due, nxt

    @callback
    def stop(self) -> None:
        self._stopped = True
        for unsub in [*self._timers.values(), *self._run_timers.values()]:
            unsub()
        self._timers.clear()
        self._run_timers.clear()
        self._fire_at.clear()

    # ------------------------------------------------------------------ dispatch

    def build_adhoc(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict | None,
        retries: int = 0,
        token: str | None = None,
    ) -> Schedule:
        return Schedule(
            id="",
            name=f"{repository}/{workflow}",
            repository=repository,
            workflow=workflow,
            ref=ref,
            inputs=dict(inputs or {}),
            retries=retries,
            dispatch_token=token,
        )

    def with_overrides(self, schedule: Schedule, ref: str | None, inputs: dict | None) -> Schedule:
        return replace(
            schedule,
            ref=ref or schedule.ref,
            inputs=dict(inputs) if inputs is not None else schedule.inputs,
        )

    def dispatch_background(self, schedule: Schedule) -> None:
        """Run the dispatch (with retries) in a background task."""
        self._entry.async_create_background_task(
            self._hass,
            self.async_dispatch(schedule),
            name=f"{DOMAIN} dispatch {schedule.name}",
        )

    async def async_dispatch(self, schedule: Schedule) -> DispatchResult:
        """Dispatch and record the result (awaits the retries)."""
        result = await dispatch_with_retry(self._github, schedule, fallback_token=self._token)
        tracked = schedule.id in self.schedules
        if tracked:
            self.states[schedule.id] = ScheduleState(
                last_dispatch_at=dt_util.utcnow(),
                ok=result.ok,
                status=result.status,
                run_id=result.run_id,
                error=result.error,
                attempts=result.attempts,
            )
        issue_id = f"{ISSUE_TOKEN_INVALID}_{self._entry.entry_id}"
        if result.auth_failed:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_TOKEN_INVALID,
                translation_placeholders={
                    "schedule": schedule.name,
                    "repository": schedule.repository,
                },
            )
        elif result.ok:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)

        payload = {
            "entry_id": self._entry.entry_id,
            "schedule": schedule.name,
            "repository": schedule.repository,
            "workflow": schedule.workflow,
            "ref": schedule.ref,
            "run_id": result.run_id,
            "status": result.status,
            "attempts": result.attempts,
        }
        if result.ok:
            self._hass.bus.async_fire(EVENT_DISPATCHED, payload)
            _LOGGER.info(
                "Dispatched %s/%s on %s (run %s)",
                schedule.repository,
                schedule.workflow,
                schedule.ref,
                result.run_id,
            )
            if tracked and result.run_id:
                self._plan_run_fetch(schedule, result.run_id, RUN_FETCH_DELAY, 1)
        else:
            self._hass.bus.async_fire(EVENT_DISPATCH_FAILED, {**payload, "error": result.error})
            _LOGGER.warning(
                "Dispatch of %s/%s failed: %s",
                schedule.repository,
                schedule.workflow,
                result.error,
            )
        self._on_change()
        return result

    # ------------------------------------------------------------------ last run

    def resume_run_fetches(self) -> None:
        """After a restart: look again at runs that were not finished."""
        for sid, state in self.states.items():
            schedule = self.schedules.get(sid)
            if schedule is not None and state.run_id and state.run_status != "completed":
                self._plan_run_fetch(schedule, state.run_id, 30, 1)

    def _plan_run_fetch(self, schedule: Schedule, run_id: int, delay: float, attempt: int) -> None:
        sid = schedule.id
        if sid in self._run_timers:
            self._run_timers.pop(sid)()

        @callback
        def _due(_now: datetime) -> None:
            self._run_timers.pop(sid, None)
            self._entry.async_create_background_task(
                self._hass,
                self._fetch_run(schedule, run_id, attempt),
                name=f"{DOMAIN} fetch run {run_id}",
            )

        self._run_timers[sid] = async_call_later(self._hass, delay, _due)

    async def _fetch_run(self, schedule: Schedule, run_id: int, attempt: int) -> None:
        state = self.states.get(schedule.id)
        if state is None or state.run_id != run_id:
            return  # superseded by a newer dispatch
        try:
            run = await self._github.get_run(schedule.repository, run_id)
        except GitHubError as err:
            _LOGGER.debug("Fetching run %s failed: %s", run_id, err)
            run = None
        if run is not None:
            state.run_status = run.get("status")
            state.run_conclusion = run.get("conclusion")
            state.run_url = run.get("html_url")
            self._on_change()
        if (run is None or state.run_status != "completed") and attempt < RUN_FETCH_MAX_ATTEMPTS:
            self._plan_run_fetch(schedule, run_id, RUN_REFETCH_DELAY, attempt + 1)
