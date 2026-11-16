"""Schedule subentries: timers, pre-wake, dispatch results, repairs."""

from datetime import UTC, datetime

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.homerunner.const import (
    CONF_AUTOMATIC,
    CONF_DRY_RUN,
    DOMAIN,
    SUBENTRY_SCHEDULE,
)
from custom_components.homerunner.github import GitHubAuthError, GitHubForbiddenError

from .conftest import ENTRY_DATA

SUB_DATA = {
    "name": "Nightly",
    "repository": "octo-org/app",
    "workflow": "nightly.yml",
    "ref": "dev",
    "inputs": '{"planned": "true"}',
    "time": "03:00:00",
    "weekdays": [0, 1, 2, 3, 4, 5, 6],
    "timezone": "Europe/Berlin",
    "prewake": True,
    "prewake_minutes": 15,
    "retries": 0,
    "enabled": True,
}


@pytest.fixture
def sched_entry(hass: HomeAssistant, entry_options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Octo-Org",
        unique_id="octo-org",
        data=dict(ENTRY_DATA),
        options=dict(entry_options),
        subentries_data=[
            ConfigSubentryData(
                data=dict(SUB_DATA),
                subentry_type=SUBENTRY_SCHEDULE,
                title="Nightly",
                unique_id=None,
                subentry_id="sub1",
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def _fire(hass, freezer, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_fires_at_the_right_time(hass, sched_entry, mocks, freezer) -> None:
    freezer.move_to("2026-11-16 01:00:00+00:00")  # 02:00 in Berlin
    events = async_capture_events(hass, f"{DOMAIN}_dispatched")
    mocks.github.dispatch.return_value = 4242
    data = await _setup(hass, sched_entry)
    assert data.runner.fire_time("sub1") == datetime(2026, 11, 16, 2, 0, tzinfo=UTC)

    await _fire(hass, freezer, datetime(2026, 11, 16, 1, 59, 59, tzinfo=UTC))
    mocks.github.dispatch.assert_not_awaited()

    await _fire(hass, freezer, datetime(2026, 11, 16, 2, 0, 1, tzinfo=UTC))
    mocks.github.dispatch.assert_awaited_once_with(
        "octo-org/app", "nightly.yml", "dev", {"planned": "true"}, token="ghp_secret_token"
    )
    assert events[0].data["run_id"] == 4242
    # next fire is tomorrow, no double dispatch
    assert data.runner.fire_time("sub1") == datetime(2026, 11, 17, 2, 0, tzinfo=UTC)
    state = data.runner.state_of("sub1")
    assert state.ok and state.run_id == 4242 and state.status == 200

    attrs = hass.states.get("sensor.schedule_nightly_last_dispatch").attributes
    assert attrs["run_id"] == 4242 and attrs["result"] == "ok"
    assert (
        hass.states.get("sensor.schedule_nightly_next_dispatch").state
        == "2026-11-17T02:00:00+00:00"
    )

    # the last run is looked up two minutes later
    await _fire(hass, freezer, datetime(2026, 11, 16, 2, 2, 5, tzinfo=UTC))
    mocks.github.get_run.assert_awaited_once_with("octo-org/app", 4242)
    run = hass.states.get("sensor.schedule_nightly_last_run")
    assert run.state == "success" and run.attributes["url"] == "https://x/run"


async def test_no_catch_up_after_restart(hass, sched_entry, mocks, freezer) -> None:
    freezer.move_to("2026-11-16 02:30:00+00:00")  # 30 min after the fire time
    data = await _setup(hass, sched_entry)
    mocks.github.dispatch.assert_not_awaited()
    assert data.runner.fire_time("sub1") == datetime(2026, 11, 17, 2, 0, tzinfo=UTC)
    await _fire(hass, freezer, datetime(2026, 11, 16, 2, 31, tzinfo=UTC))
    mocks.github.dispatch.assert_not_awaited()
    # reload: still nothing
    await hass.config_entries.async_reload(sched_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    mocks.github.dispatch.assert_not_awaited()


async def test_disabled_schedule_does_not_fire(hass, sched_entry, mocks, freezer) -> None:
    freezer.move_to("2026-11-16 01:00:00+00:00")
    data = await _setup(hass, sched_entry)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.schedule_nightly_enabled"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.schedule_nightly_enabled").state == "off"
    assert data.runner.fire_time("sub1") is None
    assert hass.states.get("sensor.schedule_nightly_next_dispatch").state == "unknown"
    await _fire(hass, freezer, datetime(2026, 11, 16, 2, 0, 5, tzinfo=UTC))
    mocks.github.dispatch.assert_not_awaited()
    # the entry was not reloaded just for that
    assert sched_entry.runtime_data is data


async def test_unauthorized_dispatch_creates_repair(hass, sched_entry, mocks, freezer) -> None:
    freezer.move_to("2026-11-16 01:00:00+00:00")
    failed = async_capture_events(hass, f"{DOMAIN}_dispatch_failed")
    mocks.github.dispatch.side_effect = GitHubAuthError("Bad credentials", 401)
    data = await _setup(hass, sched_entry)
    await _fire(hass, freezer, datetime(2026, 11, 16, 2, 0, 1, tzinfo=UTC))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"token_invalid_{sched_entry.entry_id}")
    assert issue is not None
    assert issue.translation_key == "token_invalid" and not issue.is_fixable
    assert failed and failed[0].data["status"] == 401
    st = data.runner.state_of("sub1")
    assert st.ok is False and "Bad credentials" in st.error
    # a later success clears the issue
    mocks.github.dispatch.side_effect = None
    mocks.github.dispatch.return_value = None
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.schedule_nightly_dispatch_now"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"token_invalid_{sched_entry.entry_id}") is None
    )


async def test_forbidden_is_not_a_repair(hass, sched_entry, mocks, freezer) -> None:
    freezer.move_to("2026-11-16 01:00:00+00:00")
    mocks.github.dispatch.side_effect = GitHubForbiddenError("no", 403)
    await _setup(hass, sched_entry)
    await _fire(hass, freezer, datetime(2026, 11, 16, 2, 0, 1, tzinfo=UTC))
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"token_invalid_{sched_entry.entry_id}") is None
    )
    assert mocks.github.dispatch.await_count == 1


@pytest.mark.parametrize("entry_options", [{CONF_AUTOMATIC: True, CONF_DRY_RUN: False}])
async def test_prewake_wakes_server_before_fire(hass, sched_entry, mocks, freezer) -> None:
    freezer.move_to("2026-11-16 01:30:00+00:00")  # outside the window (fire 02:00, 15 min)
    data = await _setup(hass, sched_entry)
    await data.manager.async_step()
    mocks.redfish.power_on.assert_not_awaited()
    due, nxt = data.runner.prewake_info(datetime(2026, 11, 16, 1, 30, tzinfo=UTC))
    assert due is False and nxt == datetime(2026, 11, 16, 2, 0, tzinfo=UTC)

    freezer.move_to("2026-11-16 01:46:00+00:00")
    await data.manager.async_step()
    mocks.redfish.power_on.assert_awaited_once()
    assert data.manager.machine.last_wake_reason.value == "schedule"


async def test_dispatch_service_with_schedule_and_overrides(
    hass, sched_entry, mocks, freezer
) -> None:
    freezer.move_to("2026-11-16 01:00:00+00:00")
    await _setup(hass, sched_entry)
    await hass.services.async_call(
        DOMAIN,
        "dispatch",
        {
            "schedule": "button.schedule_nightly_dispatch_now",
            "ref": "release",
            "inputs": {"x": "1"},
        },
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    mocks.github.dispatch.assert_awaited_once_with(
        "octo-org/app", "nightly.yml", "release", {"x": "1"}, token="ghp_secret_token"
    )


async def test_dispatch_service_adhoc_and_errors(hass, sched_entry, mocks, freezer) -> None:
    from homeassistant.exceptions import ServiceValidationError

    freezer.move_to("2026-11-16 01:00:00+00:00")
    await _setup(hass, sched_entry)
    await hass.services.async_call(
        DOMAIN,
        "dispatch",
        {"repository": "o/r", "workflow": "w.yml", "inputs": '{"a": "b"}'},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    mocks.github.dispatch.assert_awaited_once_with(
        "o/r", "w.yml", "main", {"a": "b"}, token="ghp_secret_token"
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "dispatch", {"repository": "o/r"}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "dispatch", {"schedule": "nope"}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "dispatch", {"schedule": "sub1", "inputs": "[1]"}, blocking=True
        )


async def test_last_dispatch_survives_restart(
    hass, sched_entry, mocks, freezer, hass_storage
) -> None:
    freezer.move_to("2026-11-16 01:00:00+00:00")
    mocks.github.dispatch.return_value = 7
    await _setup(hass, sched_entry)
    await _fire(hass, freezer, datetime(2026, 11, 16, 2, 0, 1, tzinfo=UTC))
    await hass.config_entries.async_unload(sched_entry.entry_id)
    await hass.config_entries.async_setup(sched_entry.entry_id)
    await hass.async_block_till_done()
    assert sched_entry.runtime_data.runner.state_of("sub1").run_id == 7


async def test_schedule_device_and_entities(hass, sched_entry, mocks) -> None:
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    await _setup(hass, sched_entry)
    ent = er.async_get(hass).async_get("switch.schedule_nightly_enabled")
    assert ent.config_subentry_id == "sub1"
    dev = dr.async_get(hass).async_get(ent.device_id)
    assert dev.entry_type is dr.DeviceEntryType.SERVICE
    assert dev.name == "Schedule Nightly"
