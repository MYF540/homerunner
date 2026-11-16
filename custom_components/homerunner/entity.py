"""Base entities and device info."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_OWNER, DOMAIN, SUBENTRY_SCHEDULE
from .models import HomerunnerConfigEntry, HomerunnerData


def server_device(entry: HomerunnerConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        translation_key="server",
        translation_placeholders={"owner": str(entry.data.get(CONF_OWNER, entry.title))},
    )


def schedule_device(entry: HomerunnerConfigEntry, subentry_id: str, name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{subentry_id}")},
        translation_key="schedule",
        translation_placeholders={"name": name},
        entry_type=DeviceEntryType.SERVICE,
        via_device=(DOMAIN, entry.entry_id),
    )


class HomerunnerEntity(Entity):
    """Entity that refreshes whenever the manager reports a change."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: HomerunnerConfigEntry,
        key: str,
        *,
        schedule_id: str | None = None,
        schedule_name: str | None = None,
    ) -> None:
        self.entry = entry
        self.hr: HomerunnerData = entry.runtime_data
        self.schedule_id = schedule_id
        self._attr_translation_key = key
        if schedule_id is None:
            self._attr_unique_id = f"{entry.entry_id}_{key}"
            self._attr_device_info = server_device(entry)
        else:
            self._attr_unique_id = f"{entry.entry_id}_{schedule_id}_{key}"
            self._attr_device_info = schedule_device(
                entry, schedule_id, schedule_name or schedule_id
            )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hr.manager.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


def schedule_subentries(entry: HomerunnerConfigEntry):
    """Yield the schedule subentries of an entry."""
    for sub in entry.subentries.values():
        if sub.subentry_type == SUBENTRY_SCHEDULE:
            yield sub
