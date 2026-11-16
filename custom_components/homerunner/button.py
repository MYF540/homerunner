"""Buttons."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bmc import BmcError
from .const import DEFAULT_KEEP_AWAKE_HOURS, DOMAIN
from .entity import HomerunnerEntity, schedule_subentries
from .models import HomerunnerConfigEntry

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomerunnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            ServerButton(entry, "wake"),
            ServerButton(entry, "safe_shutdown"),
            ServerButton(entry, "keep_awake"),
            ServerButton(entry, "force_off", enabled_default=False),
        ]
    )
    for sub in schedule_subentries(entry):
        async_add_entities(
            [DispatchNowButton(entry, sub.subentry_id, sub.title)],
            config_subentry_id=sub.subentry_id,
        )


class ServerButton(HomerunnerEntity, ButtonEntity):
    def __init__(
        self, entry: HomerunnerConfigEntry, key: str, enabled_default: bool = True
    ) -> None:
        super().__init__(entry, key)
        self._attr_entity_registry_enabled_default = enabled_default

    async def async_press(self) -> None:
        manager = self.hr.manager
        key = self._attr_translation_key
        if key == "wake":
            await manager.async_wake()
        elif key == "safe_shutdown":
            await manager.async_shutdown()
        elif key == "keep_awake":
            await manager.async_keep_awake(timedelta(hours=DEFAULT_KEEP_AWAKE_HOURS))
        elif key == "force_off":
            try:
                await manager.async_force_shutdown()
            except BmcError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="force_off_failed",
                    translation_placeholders={"error": str(err)},
                ) from err


class DispatchNowButton(HomerunnerEntity, ButtonEntity):
    def __init__(self, entry: HomerunnerConfigEntry, sid: str, name: str) -> None:
        super().__init__(entry, "dispatch_now", schedule_id=sid, schedule_name=name)

    async def async_press(self) -> None:
        schedule = self.hr.runner.schedules.get(self.schedule_id)
        if schedule is not None:
            self.hr.runner.dispatch_background(schedule)
