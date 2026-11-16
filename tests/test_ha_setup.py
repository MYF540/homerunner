"""Setup, unload and entities of the HA layer."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.homerunner.bmc import PowerState
from custom_components.homerunner.const import DOMAIN


async def test_setup_and_unload(hass: HomeAssistant, mock_entry, mocks) -> None:
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "wake")
    state = hass.states.get("sensor.runner_server_octo_org_state")
    assert state is not None
    assert state.state == "off"
    assert hass.states.get("binary_sensor.runner_server_octo_org_server_on").state == "off"
    assert hass.states.get("sensor.runner_server_octo_org_power").state == "12.0"
    assert hass.states.get("switch.runner_server_octo_org_dry_run").state == "on"
    assert hass.states.get("switch.runner_server_octo_org_automatic").state == "off"
    assert float(hass.states.get("number.runner_server_octo_org_idle_time").state) == 2.0

    await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.NOT_LOADED


async def test_force_off_button_disabled_by_default(hass, mock_entry, mocks) -> None:
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("button.runner_server_octo_org_force_off") is None
    assert hass.states.get("button.runner_server_octo_org_wake") is not None


async def test_auth_failure_starts_reauth(hass, mock_entry, mocks) -> None:
    from tests.conftest import make_snapshot

    mocks.snapshot = make_snapshot(ok=False, auth_failed=True)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


async def test_partial_failure_keeps_values(hass, mock_entry, mocks) -> None:
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.homerunner.bmc import BmcConnectionError

    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    mocks.redfish.get_power_state.side_effect = BmcConnectionError("down")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert mock_entry.runtime_data.server.data.bmc_ok is False
    assert mock_entry.runtime_data.server.data.power is PowerState.OFF
    assert hass.states.get("binary_sensor.runner_server_octo_org_problem").state == "on"
    assert hass.states.get("sensor.runner_server_octo_org_power").state == "unavailable"
