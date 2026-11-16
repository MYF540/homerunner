"""The Homerunner integration: wake a CI runner host on demand and shut it down when idle."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .bmc import BmcError, RedfishClient
from .const import (
    ATTR_CONFIG_ENTRY,
    ATTR_DURATION,
    ATTR_FORCE,
    ATTR_SCHEDULE,
    CONF_API_BASE,
    CONF_BMC_HOST,
    CONF_BMC_PASSWORD,
    CONF_BMC_USERNAME,
    CONF_BMC_VERIFY_SSL,
    CONF_EXCLUDED_REPOS,
    CONF_GITHUB_TOKEN,
    CONF_INPUTS,
    CONF_OWNER,
    CONF_REF,
    CONF_REPOSITORY,
    CONF_RUNNER_LABELS,
    CONF_SSH_HOST,
    CONF_SSH_KEY,
    CONF_SSH_KNOWN_HOST_KEY,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_USE_WEBHOOK,
    CONF_WEBHOOK_ID,
    CONF_WORKFLOW,
    DEFAULT_API_BASE,
    DEFAULT_BMC_VERIFY_SSL,
    DEFAULT_REF,
    DEFAULT_RUNNER_LABELS,
    DEFAULT_SSH_PORT,
    DOMAIN,
    SERVICE_DISPATCH,
    SERVICE_KEEP_AWAKE,
    SERVICE_SHUTDOWN,
    SERVICE_WAKE,
)
from .coordinator import QueueCoordinator, ServerCoordinator
from .github import GitHubClient
from .guard import GuardClient
from .manager import HomerunnerManager
from .models import HomerunnerConfigEntry, HomerunnerData, get_conf
from .queue_watcher import QueueWatcher
from .schedule_runner import ScheduleRunner
from .webhook import async_register_webhook, async_unregister_webhook

_LOGGER = logging.getLogger(__name__)

__all__ = ["DOMAIN"]

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_ENTRY_FIELD = {vol.Optional(ATTR_CONFIG_ENTRY): cv.string}
SERVICE_WAKE_SCHEMA = vol.Schema(_ENTRY_FIELD)
SERVICE_SHUTDOWN_SCHEMA = vol.Schema(
    {**_ENTRY_FIELD, vol.Optional(ATTR_FORCE, default=False): cv.boolean}
)
SERVICE_KEEP_AWAKE_SCHEMA = vol.Schema(
    {**_ENTRY_FIELD, vol.Optional(ATTR_DURATION, default={"hours": 2}): cv.time_period}
)
SERVICE_DISPATCH_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Optional(ATTR_SCHEDULE): cv.string,
        vol.Optional(CONF_REPOSITORY): cv.string,
        vol.Optional(CONF_WORKFLOW): cv.string,
        vol.Optional(CONF_REF): cv.string,
        vol.Optional(CONF_INPUTS): vol.Any(dict, cv.string),
    }
)


def _signature(entry: HomerunnerConfigEntry) -> tuple:
    """Settings that need a reload when they change (rules and `enabled` do not)."""
    subs = tuple(
        sorted(
            (
                sub.subentry_id,
                sub.title,
                json.dumps(
                    {k: v for k, v in sub.data.items() if k != "enabled"},
                    sort_keys=True,
                    default=str,
                ),
            )
            for sub in entry.subentries.values()
        )
    )
    return (
        tuple(get_conf(entry, CONF_EXCLUDED_REPOS, [])),
        tuple(get_conf(entry, CONF_RUNNER_LABELS, DEFAULT_RUNNER_LABELS)),
        bool(entry.options.get(CONF_USE_WEBHOOK)),
        entry.options.get(CONF_WEBHOOK_ID),
        subs,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (once)."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomerunnerConfigEntry) -> bool:
    """Set up a Homerunner config entry."""
    session = async_get_clientsession(hass)
    github = GitHubClient(
        session,
        entry.data[CONF_GITHUB_TOKEN],
        entry.data.get(CONF_API_BASE, DEFAULT_API_BASE),
    )
    verify = entry.data.get(CONF_BMC_VERIFY_SSL, DEFAULT_BMC_VERIFY_SSL)
    redfish = RedfishClient(
        async_get_clientsession(hass, verify_ssl=verify),
        entry.data[CONF_BMC_HOST],
        entry.data[CONF_BMC_USERNAME],
        entry.data[CONF_BMC_PASSWORD],
        verify,
    )
    guard = GuardClient(
        entry.data[CONF_SSH_HOST],
        entry.data.get(CONF_SSH_PORT, DEFAULT_SSH_PORT),
        entry.data[CONF_SSH_USERNAME],
        entry.data[CONF_SSH_KEY],
        entry.data.get(CONF_SSH_KNOWN_HOST_KEY),
    )
    watcher = QueueWatcher(
        github,
        entry.data[CONF_OWNER],
        list(get_conf(entry, CONF_RUNNER_LABELS, DEFAULT_RUNNER_LABELS)),
        list(get_conf(entry, CONF_EXCLUDED_REPOS, [])),
    )
    queue = QueueCoordinator(hass, entry, watcher)
    server = ServerCoordinator(hass, entry, redfish, guard)

    # first refreshes raise ConfigEntryAuthFailed / ConfigEntryNotReady as appropriate
    await queue.async_config_entry_first_refresh()
    await server.async_refresh()

    manager_ref: list[HomerunnerManager] = []

    def _changed() -> None:
        if manager_ref:
            manager_ref[0].async_notify()

    runner = ScheduleRunner(hass, entry, github, entry.data[CONF_GITHUB_TOKEN], _changed)
    manager = HomerunnerManager(hass, entry, queue, server, redfish, guard, runner)
    manager_ref.append(manager)

    entry.runtime_data = HomerunnerData(
        github=github,
        redfish=redfish,
        guard=guard,
        queue=queue,
        server=server,
        manager=manager,
        runner=runner,
        signature=_signature(entry),
    )

    await manager.async_start()
    entry.runtime_data.webhook_id = await async_register_webhook(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: HomerunnerConfigEntry) -> None:
    """Options or subentries changed: reload only if the structure changed."""
    data = entry.runtime_data
    if _signature(entry) != data.signature:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    data.runner.sync()
    data.manager.async_apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: HomerunnerConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_unregister_webhook(hass, entry.runtime_data.webhook_id)
        await entry.runtime_data.manager.async_stop()
    return unloaded


# ---------------------------------------------------------------------- services


def _get_entry(hass: HomeAssistant, call: ServiceCall) -> HomerunnerConfigEntry:
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_CONFIG_ENTRY)
    if wanted:
        for entry in entries:
            if entry.entry_id == wanted or entry.title == wanted:
                return entry
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_found")
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entry")
    if len(entries) > 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="multiple_entries")
    return entries[0]


def _resolve_schedule_id(hass: HomeAssistant, entry: HomerunnerConfigEntry, ref: str) -> str:
    """A schedule is addressed by subentry id or by the id of one of its entities."""
    if ref in entry.subentries:
        return ref
    registry_entry = er.async_get(hass).async_get(ref)
    if (
        registry_entry is not None
        and registry_entry.config_entry_id == entry.entry_id
        and registry_entry.config_subentry_id in entry.subentries
    ):
        return registry_entry.config_subentry_id
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_schedule",
        translation_placeholders={"schedule": ref},
    )


def _parse_inputs(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_inputs"
            ) from err
    if not isinstance(raw, dict):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_inputs")
    return raw


def _async_register_services(hass: HomeAssistant) -> None:
    async def wake(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        await entry.runtime_data.manager.async_wake()

    async def shutdown(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        manager = entry.runtime_data.manager
        if call.data[ATTR_FORCE]:
            try:
                await manager.async_force_shutdown()
            except BmcError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="force_off_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
        else:
            await manager.async_shutdown()

    async def keep_awake(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        duration: timedelta = call.data[ATTR_DURATION]
        await entry.runtime_data.manager.async_keep_awake(duration)

    async def dispatch(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        runner = entry.runtime_data.runner
        inputs = _parse_inputs(call.data.get(CONF_INPUTS))
        ref = call.data.get(CONF_REF)
        if call.data.get(ATTR_SCHEDULE):
            sid = _resolve_schedule_id(hass, entry, call.data[ATTR_SCHEDULE])
            schedule = runner.schedules.get(sid)
            if schedule is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_schedule",
                    translation_placeholders={"schedule": call.data[ATTR_SCHEDULE]},
                )
            schedule = runner.with_overrides(schedule, ref, inputs)
        else:
            if not (call.data.get(CONF_REPOSITORY) and call.data.get(CONF_WORKFLOW)):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="dispatch_target_missing"
                )
            schedule = runner.build_adhoc(
                call.data[CONF_REPOSITORY],
                call.data[CONF_WORKFLOW],
                ref or DEFAULT_REF,
                inputs,
            )
        runner.dispatch_background(schedule)

    hass.services.async_register(DOMAIN, SERVICE_WAKE, wake, schema=SERVICE_WAKE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SHUTDOWN, shutdown, schema=SERVICE_SHUTDOWN_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_KEEP_AWAKE, keep_awake, schema=SERVICE_KEEP_AWAKE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_DISPATCH, dispatch, schema=SERVICE_DISPATCH_SCHEMA)
