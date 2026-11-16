"""Number entities for the two idle timers."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_IDLE_HOURS, CONF_SHORT_IDLE_MINUTES
from .entity import HomerunnerEntity
from .models import HomerunnerConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomerunnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            RuleNumber(entry, CONF_IDLE_HOURS, 0.25, 24, 0.25, UnitOfTime.HOURS),
            RuleNumber(entry, CONF_SHORT_IDLE_MINUTES, 5, 240, 1, UnitOfTime.MINUTES),
        ]
    )


class RuleNumber(HomerunnerEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        entry: HomerunnerConfigEntry,
        rule: str,
        minimum: float,
        maximum: float,
        step: float,
        unit: str,
    ) -> None:
        super().__init__(entry, rule)
        self._rule = rule
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float:
        return float(getattr(self.hr.manager.machine.rules, self._rule))

    async def async_set_native_value(self, value: float) -> None:
        await self.hr.manager.async_set_rule(self._rule, value)
