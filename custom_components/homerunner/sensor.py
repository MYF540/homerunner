"""Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ServerState, WakeReason
from .entity import HomerunnerEntity, schedule_subentries
from .github import Job
from .models import HomerunnerConfigEntry

PARALLEL_UPDATES = 0


def _jobs(jobs: list[Job]) -> list[dict[str, Any]]:
    return [{"repo": j.repo, "name": j.name, "url": j.html_url} for j in jobs]


@dataclass(frozen=True, kw_only=True)
class HomerunnerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Any], Any]
    attrs_fn: Callable[[Any], dict[str, Any]] | None = None
    available_fn: Callable[[Any], bool] = lambda _hr: True


SERVER_SENSORS: tuple[HomerunnerSensorDescription, ...] = (
    HomerunnerSensorDescription(
        key="state",
        translation_key="state",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in ServerState],
        value_fn=lambda hr: hr.manager.machine.state.value,
        attrs_fn=lambda hr: {
            "blockers": list(hr.manager.machine.blockers),
            "dry_run": hr.manager.machine.rules.dry_run,
        },
    ),
    HomerunnerSensorDescription(
        key="queued_jobs",
        translation_key="queued_jobs",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="jobs",
        value_fn=lambda hr: len(hr.queue.data.queued) if hr.queue.data else None,
        attrs_fn=lambda hr: {"jobs": _jobs(hr.queue.data.queued) if hr.queue.data else []},
    ),
    HomerunnerSensorDescription(
        key="running_jobs",
        translation_key="running_jobs",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="jobs",
        value_fn=lambda hr: len(hr.queue.data.running) if hr.queue.data else None,
        attrs_fn=lambda hr: {"jobs": _jobs(hr.queue.data.running) if hr.queue.data else []},
    ),
    HomerunnerSensorDescription(
        key="shutdown_at",
        translation_key="shutdown_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda hr: hr.manager.machine.shutdown_at,
    ),
    HomerunnerSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda hr: hr.server.data.watts if hr.server.data else None,
        available_fn=lambda hr: bool(hr.server.data and hr.server.data.bmc_ok),
    ),
    HomerunnerSensorDescription(
        key="last_wake_reason",
        translation_key="last_wake_reason",
        device_class=SensorDeviceClass.ENUM,
        options=[r.value for r in WakeReason],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hr: (
            hr.manager.machine.last_wake_reason.value
            if hr.manager.machine.last_wake_reason
            else None
        ),
    ),
    HomerunnerSensorDescription(
        key="starts_today",
        translation_key="starts_today",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda hr: hr.manager.machine.starts_today,
    ),
    HomerunnerSensorDescription(
        key="rate_limit_remaining",
        translation_key="rate_limit_remaining",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda hr: hr.queue.data.rate_limit.remaining if hr.queue.data else None,
        attrs_fn=lambda hr: {
            "limit": hr.queue.data.rate_limit.limit if hr.queue.data else None,
            "reset_at": hr.queue.data.rate_limit.reset_at if hr.queue.data else None,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomerunnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(ServerSensor(entry, desc) for desc in SERVER_SENSORS)
    for sub in schedule_subentries(entry):
        async_add_entities(
            [
                ScheduleNextFireSensor(entry, sub.subentry_id, sub.title),
                ScheduleLastDispatchSensor(entry, sub.subentry_id, sub.title),
                ScheduleLastRunSensor(entry, sub.subentry_id, sub.title),
            ],
            config_subentry_id=sub.subentry_id,
        )


class ServerSensor(HomerunnerEntity, SensorEntity):
    entity_description: HomerunnerSensorDescription

    def __init__(
        self, entry: HomerunnerConfigEntry, description: HomerunnerSensorDescription
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.hr)

    @property
    def available(self) -> bool:
        return self.entity_description.available_fn(self.hr)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.hr) if fn else None


class _ScheduleSensor(HomerunnerEntity, SensorEntity):
    def __init__(self, entry: HomerunnerConfigEntry, key: str, sid: str, name: str) -> None:
        super().__init__(entry, key, schedule_id=sid, schedule_name=name)

    @property
    def _state(self):
        return self.hr.runner.state_of(self.schedule_id)


class ScheduleNextFireSensor(_ScheduleSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: HomerunnerConfigEntry, sid: str, name: str) -> None:
        super().__init__(entry, "next_fire", sid, name)

    @property
    def native_value(self) -> datetime | None:
        return self.hr.runner.fire_time(self.schedule_id)


class ScheduleLastDispatchSensor(_ScheduleSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: HomerunnerConfigEntry, sid: str, name: str) -> None:
        super().__init__(entry, "last_dispatch", sid, name)

    @property
    def native_value(self) -> datetime | None:
        return self._state.last_dispatch_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._state
        return {
            "result": None if st.ok is None else ("ok" if st.ok else "failed"),
            "status": st.status,
            "run_id": st.run_id,
            "error": st.error,
            "attempts": st.attempts,
        }


class ScheduleLastRunSensor(_ScheduleSensor):
    def __init__(self, entry: HomerunnerConfigEntry, sid: str, name: str) -> None:
        super().__init__(entry, "last_run", sid, name)

    @property
    def native_value(self) -> str | None:
        st = self._state
        return st.run_conclusion or st.run_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._state
        return {"url": st.run_url, "run_id": st.run_id}
