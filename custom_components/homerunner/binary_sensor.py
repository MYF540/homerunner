"""Binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import PowerState, ServerState
from .entity import HomerunnerEntity
from .models import HomerunnerConfigEntry, HomerunnerData

PARALLEL_UPDATES = 0


def _problem(hr: HomerunnerData) -> bool:
    server = hr.server.data
    queue = hr.queue.data
    return bool(
        hr.manager.machine.state == ServerState.WAKE_FAILED
        or (queue is not None and not queue.ok)
        or (server is not None and not server.bmc_ok)
        or (server is not None and server.power == PowerState.ON and not server.guard_ok)
    )


@dataclass(frozen=True, kw_only=True)
class HomerunnerBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[HomerunnerData], bool | None]


DESCRIPTIONS = (
    HomerunnerBinaryDescription(
        key="server_on",
        translation_key="server_on",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda hr: (
            None
            if not hr.server.data or hr.server.data.power == PowerState.UNKNOWN
            else hr.server.data.power == PowerState.ON
        ),
    ),
    HomerunnerBinaryDescription(
        key="github_reachable",
        translation_key="github_reachable",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hr: bool(hr.queue.data and hr.queue.data.ok),
    ),
    HomerunnerBinaryDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_problem,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomerunnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(ServerBinarySensor(entry, d) for d in DESCRIPTIONS)


class ServerBinarySensor(HomerunnerEntity, BinarySensorEntity):
    entity_description: HomerunnerBinaryDescription

    def __init__(
        self, entry: HomerunnerConfigEntry, description: HomerunnerBinaryDescription
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.hr)
