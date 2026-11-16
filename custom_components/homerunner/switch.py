"""Switches: rule toggles and per-schedule enable."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_AUTO_SHUTDOWN, CONF_AUTOMATIC, CONF_DRY_RUN, CONF_ENABLED
from .entity import HomerunnerEntity, schedule_subentries
from .models import HomerunnerConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomerunnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            RuleSwitch(entry, CONF_AUTOMATIC),
            RuleSwitch(entry, CONF_AUTO_SHUTDOWN),
            RuleSwitch(entry, CONF_DRY_RUN),
        ]
    )
    for sub in schedule_subentries(entry):
        async_add_entities(
            [ScheduleEnabledSwitch(entry, sub.subentry_id, sub.title)],
            config_subentry_id=sub.subentry_id,
        )


class RuleSwitch(HomerunnerEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: HomerunnerConfigEntry, rule: str) -> None:
        super().__init__(entry, rule)
        self._rule = rule

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.hr.manager.machine.rules, self._rule))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hr.manager.async_set_rule(self._rule, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hr.manager.async_set_rule(self._rule, False)


class ScheduleEnabledSwitch(HomerunnerEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: HomerunnerConfigEntry, sid: str, name: str) -> None:
        super().__init__(entry, "enabled", schedule_id=sid, schedule_name=name)

    @property
    def is_on(self) -> bool | None:
        schedule = self.hr.runner.schedules.get(self.schedule_id)
        return None if schedule is None else schedule.enabled

    async def _set(self, value: bool) -> None:
        sub = self.entry.subentries[self.schedule_id]
        self.hass.config_entries.async_update_subentry(
            self.entry, sub, data={**sub.data, CONF_ENABLED: value}
        )
        self.hr.runner.sync()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
