"""Data update coordinators: GitHub queue and server state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .bmc import BmcError, RedfishClient
from .const import DOMAIN, PowerState
from .guard import GuardClient, GuardError, GuardStatus
from .queue_watcher import QueueSnapshot, QueueWatcher

_LOGGER = logging.getLogger(__name__)

QUEUE_INTERVAL = timedelta(seconds=60)
QUEUE_INTERVAL_THROTTLED = timedelta(minutes=5)
SERVER_INTERVAL = timedelta(seconds=30)


class QueueCoordinator(DataUpdateCoordinator[QueueSnapshot]):
    """Scans GitHub for queued and running jobs."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, watcher: QueueWatcher) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} queue",
            update_interval=QUEUE_INTERVAL,
        )
        self._watcher = watcher

    async def _async_update_data(self) -> QueueSnapshot:
        snapshot = await self._watcher.scan()
        if snapshot.auth_failed:
            raise ConfigEntryAuthFailed("GitHub token rejected")
        self.update_interval = QUEUE_INTERVAL_THROTTLED if snapshot.throttled else QUEUE_INTERVAL
        return snapshot


@dataclass
class ServerData:
    """State of the server as far as it could be read."""

    power: PowerState = PowerState.UNKNOWN
    watts: float | None = None
    guard: GuardStatus | None = None
    bmc_ok: bool = False
    guard_ok: bool = True
    error: str | None = None


class ServerCoordinator(DataUpdateCoordinator[ServerData]):
    """Reads BMC power state / watts and (while on) the guard status."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        redfish: RedfishClient,
        guard: GuardClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} server",
            update_interval=SERVER_INTERVAL,
        )
        self._redfish = redfish
        self._guard = guard
        self._last = ServerData()

    async def _async_update_data(self) -> ServerData:
        last = self._last
        errors: list[str] = []
        power = last.power
        watts = last.watts
        bmc_ok = True
        try:
            power = await self._redfish.get_power_state()
        except BmcError as err:
            bmc_ok = False
            errors.append(f"BMC: {err}")
            _LOGGER.debug("Reading power state failed: %s", err)
        if bmc_ok:
            try:
                watts = await self._redfish.get_power_watts()
            except BmcError as err:
                errors.append(f"BMC power: {err}")
                _LOGGER.debug("Reading power draw failed: %s", err)

        guard = last.guard
        guard_ok = True
        if power == PowerState.ON:
            try:
                guard = await self._guard.status()
            except GuardError as err:
                guard_ok = False
                errors.append(f"Guard: {err}")
                _LOGGER.debug("Guard status failed: %s", err)
        else:
            guard = None

        self._last = ServerData(
            power=power,
            watts=watts,
            guard=guard,
            bmc_ok=bmc_ok,
            guard_ok=guard_ok,
            error="; ".join(errors) or None,
        )
        return self._last
