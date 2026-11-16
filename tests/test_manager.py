"""The manager: power decisions turn into BMC / guard calls."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.homerunner.bmc import BmcConnectionError, PowerState
from custom_components.homerunner.const import (
    CONF_AUTO_SHUTDOWN,
    CONF_AUTOMATIC,
    CONF_DRY_RUN,
    CONF_WOL_MAC,
    DOMAIN,
)
from custom_components.homerunner.guard import GuardConnectionError, ShutdownResult
from custom_components.homerunner.power_manager import ServerState

from .conftest import make_guard_status, make_job, make_snapshot

LIVE = {CONF_AUTOMATIC: True, CONF_AUTO_SHUTDOWN: True, CONF_DRY_RUN: False}


async def _setup(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.manager


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_queued_job_wakes_server(hass: HomeAssistant, mock_entry, mocks) -> None:
    mocks.snapshot = make_snapshot(queued=[make_job()])
    manager = await _setup(hass, mock_entry)
    await manager.async_step()
    mocks.redfish.power_on.assert_awaited_once()
    mocks.wol.assert_not_awaited()
    assert manager.machine.state is ServerState.STARTING
    assert hass.states.get("sensor.runner_server_octo_org_queued_jobs").state == "1"
    attrs = hass.states.get("sensor.runner_server_octo_org_queued_jobs").attributes
    assert attrs["jobs"][0]["repo"] == "octo-org/app"


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_wol_is_sent_and_alone_counts_as_attempt(hass, mock_entry, mocks) -> None:
    hass.config_entries.async_update_entry(
        mock_entry, data={**mock_entry.data, CONF_WOL_MAC: "AA:BB:CC:DD:EE:FF"}
    )
    mocks.redfish.power_on.side_effect = BmcConnectionError("down")
    mocks.snapshot = make_snapshot(queued=[make_job()])
    manager = await _setup(hass, mock_entry)
    await manager.async_step()
    mocks.wol.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")
    assert manager.machine.state is ServerState.STARTING  # not WAKE_FAILED


@pytest.mark.parametrize(
    "entry_options", [{CONF_AUTOMATIC: True, CONF_AUTO_SHUTDOWN: True, CONF_DRY_RUN: True}]
)
async def test_dry_run_does_not_call_bmc(hass, mock_entry, mocks, caplog) -> None:
    caplog.set_level("INFO")
    mocks.snapshot = make_snapshot(queued=[make_job()])
    manager = await _setup(hass, mock_entry)
    await manager.async_step()
    mocks.redfish.power_on.assert_not_awaited()
    mocks.wol.assert_not_awaited()
    assert "Dry run: would wake" in caplog.text
    assert manager.machine.state is ServerState.STARTING


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_wake_failure_notifies(hass, mock_entry, mocks) -> None:
    events = async_capture_events(hass, f"{DOMAIN}_wake_failed")
    mocks.redfish.power_on.side_effect = BmcConnectionError("nope")
    mocks.snapshot = make_snapshot(queued=[make_job()])
    manager = await _setup(hass, mock_entry)
    with patch(
        "custom_components.homerunner.manager.persistent_notification.async_create"
    ) as create:
        await manager.async_step()
        await hass.async_block_till_done()
    assert manager.machine.state is ServerState.WAKE_FAILED
    assert len(events) == 1
    create.assert_called_once()
    assert "did not start" in create.call_args.kwargs["title"]
    assert create.call_args.args[1].endswith("nope")


async def _up(hass, entry, mocks):
    mocks.redfish.get_power_state.return_value = PowerState.ON
    manager = await _setup(hass, entry)
    assert manager.machine.state is ServerState.READY
    return manager


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_guard_refusal_is_handled(hass, mock_entry, mocks) -> None:
    refused = async_capture_events(hass, f"{DOMAIN}_shutdown_refused")
    manager = await _up(hass, mock_entry, mocks)
    mocks.guard.shutdown.return_value = ShutdownResult(False, "job_running", None)
    await manager.async_shutdown()
    await hass.async_block_till_done()
    mocks.guard.shutdown.assert_awaited_once()
    mocks.redfish.power_off.assert_not_awaited()
    assert manager.machine.state is ServerState.READY
    assert refused[0].data["reason"] == "job_running"


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_guard_unreachable_never_powers_off(hass, mock_entry, mocks) -> None:
    manager = await _up(hass, mock_entry, mocks)
    mocks.guard.shutdown.side_effect = GuardConnectionError("ssh down")
    await manager.async_shutdown()
    mocks.redfish.power_off.assert_not_awaited()
    assert manager.machine.state is ServerState.READY


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_idle_shutdown_goes_through_guard(hass, mock_entry, mocks) -> None:
    manager = await _up(hass, mock_entry, mocks)
    manager.machine.rules.idle_hours = 0.25
    manager.machine.rules.short_idle_minutes = 5
    manager.machine.rules.min_uptime_minutes = 0
    manager.machine.idle_since = dt_util.utcnow() - timedelta(hours=1)
    manager.machine.last_on_at = dt_util.utcnow() - timedelta(hours=1)
    await manager.async_step()
    mocks.guard.shutdown.assert_awaited_once()
    mocks.redfish.power_off.assert_not_awaited()
    assert manager.machine.state is ServerState.SHUTTING_DOWN


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_force_service_uses_bmc_without_guard(hass, mock_entry, mocks) -> None:
    await _up(hass, mock_entry, mocks)
    await hass.services.async_call(DOMAIN, "shutdown", {"force": True}, blocking=True)
    mocks.redfish.power_off.assert_awaited_once_with(force=True)
    mocks.guard.shutdown.assert_not_awaited()


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_force_service_error_is_reported(hass, mock_entry, mocks) -> None:
    await _up(hass, mock_entry, mocks)
    mocks.redfish.power_off.side_effect = BmcConnectionError("x")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "shutdown", {"force": True}, blocking=True)


async def test_dry_run_force_does_not_call_bmc(hass, mock_entry, mocks) -> None:
    await _up(hass, mock_entry, mocks)
    await hass.services.async_call(DOMAIN, "shutdown", {"force": True}, blocking=True)
    mocks.redfish.power_off.assert_not_awaited()


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_services_wake_keep_awake_shutdown(hass, mock_entry, mocks) -> None:
    manager = await _setup(hass, mock_entry)  # power off
    await hass.services.async_call(DOMAIN, "wake", {}, blocking=True)
    mocks.redfish.power_on.assert_awaited_once()
    await hass.services.async_call(DOMAIN, "keep_awake", {"duration": {"hours": 3}}, blocking=True)
    left = manager.machine.keep_awake_until - dt_util.utcnow()
    assert timedelta(hours=2, minutes=59) < left <= timedelta(hours=3)
    await hass.services.async_call(DOMAIN, "shutdown", {}, blocking=True)  # not up -> nothing
    mocks.guard.shutdown.assert_not_awaited()


async def test_entities_change_rules_immediately(hass, mock_entry, mocks) -> None:
    manager = await _setup(hass, mock_entry)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.runner_server_octo_org_idle_time", "value": 3.5},
        blocking=True,
    )
    assert manager.machine.rules.idle_hours == 3.5
    await hass.async_block_till_done()
    assert mock_entry.options["idle_hours"] == 3.5
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.runner_server_octo_org_automatic"}, blocking=True
    )
    assert manager.machine.rules.automatic is True
    assert mock_entry.options[CONF_AUTOMATIC] is True
    assert manager.machine.rules.auto_shutdown is False  # missing key -> False
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.runner_server_octo_org_automatic_shutdown"},
        blocking=True,
    )
    assert manager.machine.rules.auto_shutdown is True
    assert mock_entry.options[CONF_AUTO_SHUTDOWN] is True


async def test_state_is_persisted(hass, mock_entry, mocks, hass_storage) -> None:
    manager = await _setup(hass, mock_entry)
    await hass.config_entries.async_unload(mock_entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.{mock_entry.entry_id}"]["data"]
    assert stored["machine"]["state"] == "off"
    assert "schedules" in stored
    assert manager is not None


async def test_service_without_entry(hass) -> None:
    from homeassistant.exceptions import ServiceValidationError
    from homeassistant.setup import async_setup_component

    await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "wake", {}, blocking=True)


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_guard_status_used_for_readiness(hass, mock_entry, mocks) -> None:
    mocks.redfish.get_power_state.return_value = PowerState.ON
    mocks.guard.status.return_value = make_guard_status(active=False)
    manager = await _setup(hass, mock_entry)
    assert manager.machine.state is ServerState.STARTING


@pytest.mark.parametrize("entry_options", [LIVE])
async def test_failed_queue_refresh_blocks_shutdown(hass, mock_entry, mocks) -> None:
    """A stale 'ok' snapshot must not count as GitHub being reachable."""
    manager = await _up(hass, mock_entry, mocks)
    assert manager.build_observation().github_ok is True
    manager.queue.last_update_success = False
    assert manager.build_observation().github_ok is False
