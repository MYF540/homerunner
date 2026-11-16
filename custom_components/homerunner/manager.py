"""HA glue around the power state machine: observes, decides, acts and persists."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .bmc import BmcError, RedfishClient, send_magic_packet
from .const import (
    CONF_AUTOMATIC,
    CONF_DRY_RUN,
    CONF_FORCED_WAKE_DAYS,
    CONF_FORCED_WAKE_MINUTES,
    CONF_IDLE_HOURS,
    CONF_MAX_STARTS_PER_DAY,
    CONF_MIN_UPTIME_MINUTES,
    CONF_QUEUE_ALERT_MINUTES,
    CONF_SCHEDULE_GUARD_MINUTES,
    CONF_SHORT_IDLE_MINUTES,
    CONF_WAKE_TIMEOUT_MINUTES,
    CONF_WOL_MAC,
    DEFAULT_AUTOMATIC,
    DEFAULT_DRY_RUN,
    DEFAULT_FORCED_WAKE_DAYS,
    DEFAULT_FORCED_WAKE_MINUTES,
    DEFAULT_IDLE_HOURS,
    DEFAULT_MAX_STARTS_PER_DAY,
    DEFAULT_MIN_UPTIME_MINUTES,
    DEFAULT_QUEUE_ALERT_MINUTES,
    DEFAULT_SCHEDULE_GUARD_MINUTES,
    DEFAULT_SHORT_IDLE_MINUTES,
    DEFAULT_WAKE_TIMEOUT_MINUTES,
    DOMAIN,
    PowerState,
    WakeReason,
)
from .coordinator import QueueCoordinator, ServerCoordinator
from .guard import GuardClient, GuardError
from .power_manager import Action, Observation, PowerStateMachine, Rules
from .schedule_runner import ScheduleRunner

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SAVE_DELAY = 10
STEP_INTERVAL = timedelta(minutes=1)

_RULE_DEFAULTS: dict[str, Any] = {
    CONF_IDLE_HOURS: DEFAULT_IDLE_HOURS,
    CONF_SHORT_IDLE_MINUTES: DEFAULT_SHORT_IDLE_MINUTES,
    CONF_MIN_UPTIME_MINUTES: DEFAULT_MIN_UPTIME_MINUTES,
    CONF_MAX_STARTS_PER_DAY: DEFAULT_MAX_STARTS_PER_DAY,
    CONF_FORCED_WAKE_DAYS: DEFAULT_FORCED_WAKE_DAYS,
    CONF_FORCED_WAKE_MINUTES: DEFAULT_FORCED_WAKE_MINUTES,
    CONF_WAKE_TIMEOUT_MINUTES: DEFAULT_WAKE_TIMEOUT_MINUTES,
    CONF_SCHEDULE_GUARD_MINUTES: DEFAULT_SCHEDULE_GUARD_MINUTES,
    CONF_QUEUE_ALERT_MINUTES: DEFAULT_QUEUE_ALERT_MINUTES,
    CONF_AUTOMATIC: DEFAULT_AUTOMATIC,
    CONF_DRY_RUN: DEFAULT_DRY_RUN,
}
_BOOL_RULES = {CONF_AUTOMATIC, CONF_DRY_RUN}
_INT_RULES = {CONF_MAX_STARTS_PER_DAY}

NOTIFY_TEXT: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "wake_failed": (
            "Runner server did not start",
            "The server could not be woken or did not become ready in time. {error}",
        ),
        "start_limit": (
            "Start limit reached",
            "The server was started {starts_today} times today (limit {max_starts_per_day}). "
            "It stays on until midnight.",
        ),
        "unexpected_off": (
            "Runner server is off unexpectedly",
            "The server was switched off although no shutdown was requested.",
        ),
        "queue_stuck": (
            "Job waits although the server is up",
            "{queued} job(s) have been waiting since {waiting_since}.",
        ),
        "shutdown_stuck": (
            "Server did not shut down",
            "The server is still on {minutes} minutes after the shutdown request.",
        ),
    },
    "de": {
        "wake_failed": (
            "Runner-Server startet nicht",
            "Der Server konnte nicht geweckt werden oder wurde nicht rechtzeitig bereit. {error}",
        ),
        "start_limit": (
            "Startgrenze erreicht",
            "Der Server wurde heute {starts_today} Mal gestartet (Grenze {max_starts_per_day}). "
            "Er bleibt bis Mitternacht an.",
        ),
        "unexpected_off": (
            "Runner-Server unerwartet aus",
            "Der Server wurde ausgeschaltet, ohne dass ein Herunterfahren angefordert war.",
        ),
        "queue_stuck": (
            "Job wartet trotz laufendem Server",
            "{queued} Job(s) warten seit {waiting_since}.",
        ),
        "shutdown_stuck": (
            "Server fährt nicht herunter",
            "Der Server ist {minutes} Minuten nach der Anforderung noch an.",
        ),
    },
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def rules_from_entry(entry: ConfigEntry) -> Rules:
    """Build the rules from the entry's options (fallback to the defaults)."""
    values: dict[str, Any] = {}
    for key, default in _RULE_DEFAULTS.items():
        value = entry.options.get(key, default)
        if key in _BOOL_RULES:
            value = bool(value)
        elif key in _INT_RULES:
            value = int(value)
        else:
            value = float(value)
        values[key] = value
    return Rules(**values)


class HomerunnerManager:
    """Runs the state machine and executes what it decides."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        queue: QueueCoordinator,
        server: ServerCoordinator,
        redfish: RedfishClient,
        guard: GuardClient,
        runner: ScheduleRunner,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.queue = queue
        self.server = server
        self.redfish = redfish
        self.guard = guard
        self.runner = runner
        self.machine = PowerStateMachine(rules_from_entry(entry))
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Load the persisted state and start stepping."""
        stored = await self._store.async_load() or {}
        self.machine = PowerStateMachine(rules_from_entry(self.entry), stored.get("machine"))
        self.runner.load(stored.get("schedules"))
        self.runner.sync()
        self.runner.resume_run_fetches()
        self._unsubs.append(self.queue.async_add_listener(self._on_coordinator))
        self._unsubs.append(self.server.async_add_listener(self._on_coordinator))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_interval, STEP_INTERVAL)
        )
        await self.async_step()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.runner.stop()
        await self._store.async_save(self._payload())

    # ------------------------------------------------------------------ listeners

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def async_notify(self) -> None:
        """Tell the entities that something changed and persist."""
        for listener in list(self._listeners):
            listener()
        self._store.async_delay_save(self._payload, SAVE_DELAY)

    def _payload(self) -> dict[str, Any]:
        return {"machine": self.machine.as_dict(), "schedules": self.runner.dump()}

    @callback
    def _on_coordinator(self) -> None:
        self.entry.async_create_background_task(
            self.hass, self.async_step(), name=f"{DOMAIN} step"
        )

    @callback
    def _on_interval(self, _now: datetime) -> None:
        self._on_coordinator()

    # ------------------------------------------------------------------ rules

    @callback
    def async_apply_options(self) -> None:
        """Copy the entry's options into the machine's (mutable) rules."""
        new = rules_from_entry(self.entry)
        for key in _RULE_DEFAULTS:
            setattr(self.machine.rules, key, getattr(new, key))
        self.async_notify()

    async def async_set_rule(self, key: str, value: Any) -> None:
        """Change one rule (number/switch entities): effective immediately, persisted in options."""
        if key not in _RULE_DEFAULTS:
            raise ValueError(f"unknown rule {key}")
        if key in _BOOL_RULES:
            value = bool(value)
        elif key in _INT_RULES:
            value = int(value)
        else:
            value = float(value)
        setattr(self.machine.rules, key, value)
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, key: value}
        )
        self.async_notify()
        await self.async_step()

    # ------------------------------------------------------------------ observation

    def build_observation(self, now: datetime | None = None) -> Observation:
        now = now or dt_util.utcnow()
        queue = self.queue.data
        server = self.server.data
        queued = queue.queued if queue else []
        created = [j.created_at for j in queued if j.created_at]
        power = server.power if server else PowerState.UNKNOWN
        runners_ready: bool | None = None
        if server and server.guard is not None and server.guard_ok and power == PowerState.ON:
            runners_ready = server.guard.all_active
        prewake_due, next_at = self.runner.prewake_info(now)
        return Observation(
            now=now,
            local_date=dt_util.as_local(now).date(),
            power=power,
            runners_ready=runners_ready,
            queued=len(queued),
            running=len(queue.running) if queue else 0,
            github_ok=bool(queue and queue.ok),
            next_schedule_at=next_at,
            prewake_due=prewake_due,
            oldest_queued_at=min(created) if created else None,
        )

    # ------------------------------------------------------------------ stepping

    async def async_step(self) -> None:
        """One state machine step on the current observation."""
        async with self._lock:
            obs = self.build_observation()
            actions = self.machine.step(obs)
            await self._execute(actions)
            self.async_notify()

    async def _execute(self, actions: list[Action]) -> None:
        pending = list(actions)
        while pending:
            action = pending.pop(0)
            try:
                if action.kind == "wake":
                    pending += await self._do_wake(action)
                elif action.kind == "shutdown":
                    pending += await self._do_shutdown(action)
                elif action.kind == "notify":
                    self._do_notify(action)
                elif action.kind == "event":
                    self.hass.bus.async_fire(f"{DOMAIN}_{action.reason}", action.detail)
            except Exception:
                _LOGGER.exception("Executing %s/%s failed", action.kind, action.reason)

    async def _do_wake(self, action: Action) -> list[Action]:
        if action.detail.get("dry_run"):
            _LOGGER.info("Dry run: would wake the server (%s)", action.reason)
            return []
        _LOGGER.info("Waking the server (%s)", action.reason)
        error: str | None = None
        try:
            await self.redfish.power_on()
        except BmcError as err:
            error = str(err) or type(err).__name__
            _LOGGER.warning("Redfish power on failed: %s", error)
        wol_sent = False
        mac = self.entry.data.get(CONF_WOL_MAC)
        if mac:
            try:
                await send_magic_packet(mac)
                wol_sent = True
            except (OSError, ValueError) as err:
                _LOGGER.warning("Wake-on-LAN failed: %s", err)
                error = error or str(err)
        ok = error is None or wol_sent
        self.last_error = None if ok else error
        return self.machine.on_wake_result(dt_util.utcnow(), ok, None if ok else error)

    async def _do_shutdown(self, action: Action) -> list[Action]:
        if action.detail.get("dry_run"):
            _LOGGER.info("Dry run: would shut the server down (%s)", action.reason)
            return []
        _LOGGER.info("Asking the guard to shut the server down (%s)", action.reason)
        try:
            result = await self.guard.shutdown()
        except GuardError as err:
            _LOGGER.warning("Guard shutdown failed: %s", err)
            self.last_error = str(err)
            return self.machine.on_shutdown_result(dt_util.utcnow(), False, "error")
        return self.machine.on_shutdown_result(
            dt_util.utcnow(), result.accepted, result.reason
        )

    def _do_notify(self, action: Action) -> None:
        language = "de" if (self.hass.config.language or "").startswith("de") else "en"
        texts = NOTIFY_TEXT[language].get(action.reason) or NOTIFY_TEXT["en"].get(action.reason)
        if texts is not None:
            title, message = texts
            message = message.format_map(_SafeDict(action.detail))
            if self.machine.rules.dry_run:
                message = f"[dry run] {message}"
            persistent_notification.async_create(
                self.hass,
                message,
                title=title,
                notification_id=f"{DOMAIN}_{self.entry.entry_id}_{action.reason}",
            )
        _LOGGER.info("Notification %s: %s", action.reason, action.detail)
        self.hass.bus.async_fire(
            f"{DOMAIN}_{action.reason}", {"entry_id": self.entry.entry_id, **action.detail}
        )

    # ------------------------------------------------------------------ manual commands

    async def async_wake(self, reason: WakeReason = WakeReason.MANUAL) -> None:
        async with self._lock:
            actions = self.machine.request_wake(dt_util.utcnow(), reason)
            await self._execute(actions)
            self.async_notify()

    async def async_shutdown(self) -> None:
        """Safe shutdown through the guard."""
        async with self._lock:
            actions = self.machine.request_shutdown(dt_util.utcnow())
            await self._execute(actions)
            self.async_notify()

    async def async_force_shutdown(self) -> None:
        """Switch the server off through the BMC without asking the guard."""
        async with self._lock:
            now = dt_util.utcnow()
            self.machine.request_shutdown(now)  # moves the machine to SHUTTING_DOWN
            if self.machine.rules.dry_run:
                _LOGGER.info("Dry run: would force the server off")
            else:
                _LOGGER.warning("Forcing the server off without the guard")
                try:
                    await self.redfish.power_off(force=True)
                except BmcError as err:
                    self.last_error = str(err)
                    self.machine.on_shutdown_result(now, False, "error")
                    self.async_notify()
                    raise
            self.async_notify()

    async def async_keep_awake(self, duration: timedelta) -> None:
        now = dt_util.utcnow()
        self.machine.keep_awake(now, now + duration)
        self.async_notify()
        await self.async_step()
