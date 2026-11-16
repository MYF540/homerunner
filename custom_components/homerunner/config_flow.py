"""Config flow, reauth, options flow and schedule subentries."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo

import asyncssh
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .bmc import BmcAuthError, BmcError, RedfishClient, build_magic_packet
from .const import (
    CONF_API_BASE,
    CONF_AUTOMATIC,
    CONF_BMC_HOST,
    CONF_BMC_PASSWORD,
    CONF_BMC_USERNAME,
    CONF_BMC_VERIFY_SSL,
    CONF_DISPATCH_TOKEN,
    CONF_DRY_RUN,
    CONF_ENABLED,
    CONF_EXCLUDED_REPOS,
    CONF_FORCED_WAKE_DAYS,
    CONF_FORCED_WAKE_MINUTES,
    CONF_GITHUB_TOKEN,
    CONF_IDLE_HOURS,
    CONF_INPUTS,
    CONF_MAX_STARTS_PER_DAY,
    CONF_MIN_UPTIME_MINUTES,
    CONF_NAME,
    CONF_OWNER,
    CONF_PREWAKE,
    CONF_PREWAKE_MINUTES,
    CONF_QUEUE_ALERT_MINUTES,
    CONF_REF,
    CONF_REPOSITORY,
    CONF_RETRIES,
    CONF_RUNNER_LABELS,
    CONF_SCHEDULE_GUARD_MINUTES,
    CONF_SHORT_IDLE_MINUTES,
    CONF_SSH_HOST,
    CONF_SSH_KEY,
    CONF_SSH_KNOWN_HOST_KEY,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_TIME,
    CONF_TIMEZONE,
    CONF_USE_WEBHOOK,
    CONF_WAKE_TIMEOUT_MINUTES,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    CONF_WEEKDAYS,
    CONF_WOL_MAC,
    CONF_WORKFLOW,
    DEFAULT_API_BASE,
    DEFAULT_AUTOMATIC,
    DEFAULT_BMC_VERIFY_SSL,
    DEFAULT_DRY_RUN,
    DEFAULT_FORCED_WAKE_DAYS,
    DEFAULT_FORCED_WAKE_MINUTES,
    DEFAULT_IDLE_HOURS,
    DEFAULT_MAX_STARTS_PER_DAY,
    DEFAULT_MIN_UPTIME_MINUTES,
    DEFAULT_PREWAKE,
    DEFAULT_PREWAKE_MINUTES,
    DEFAULT_QUEUE_ALERT_MINUTES,
    DEFAULT_REF,
    DEFAULT_RETRIES,
    DEFAULT_RUNNER_LABELS,
    DEFAULT_SCHEDULE_GUARD_MINUTES,
    DEFAULT_SHORT_IDLE_MINUTES,
    DEFAULT_SSH_PORT,
    DEFAULT_WAKE_TIMEOUT_MINUTES,
    DEFAULT_WEEKDAYS,
    DOMAIN,
    SUBENTRY_SCHEDULE,
)
from .github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubForbiddenError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from .guard import GuardClient, GuardConnectionError, GuardError
from .models import get_conf
from .webhook import generate_credentials, webhook_url

_LOGGER = logging.getLogger(__name__)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _password() -> TextSelector:
    return TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _tags() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=[], custom_value=True, multiple=True)
    )


def _number(minimum: float, maximum: float, step: float = 1) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    )


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    return [str(v).strip() for v in (value or []) if str(v).strip()]


async def _validate_github(
    hass: HomeAssistant, token: str, api_base: str, owner: str
) -> str | None:
    """Return an error key, or None if GitHub accepts token and owner."""
    client = GitHubClient(async_get_clientsession(hass), token, api_base)
    try:
        await client.validate(owner)
    except GitHubAuthError:
        return "invalid_auth"
    except (GitHubForbiddenError, GitHubRateLimitError):
        return "forbidden"
    except GitHubNotFoundError:
        return "owner_not_found"
    except GitHubError:
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error while validating GitHub access")
        return "unknown"
    return None


class HomerunnerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Homerunner: GitHub, then the server, then pin the SSH host key."""

    VERSION = 1

    def __init__(self) -> None:
        self._github: dict[str, Any] = {}
        self._server: dict[str, Any] = {}
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomerunnerOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_SCHEDULE: ScheduleSubentryFlow}

    # ---------------------------------------------------------------- GitHub

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            owner = user_input[CONF_OWNER].strip()
            api_base = (user_input.get(CONF_API_BASE) or DEFAULT_API_BASE).strip()
            token = user_input[CONF_GITHUB_TOKEN].strip()
            await self.async_set_unique_id(owner.lower())
            self._abort_if_unique_id_configured()
            error = await _validate_github(self.hass, token, api_base, owner)
            if error is None:
                self._github = {
                    CONF_GITHUB_TOKEN: token,
                    CONF_API_BASE: api_base,
                    CONF_OWNER: owner,
                    CONF_EXCLUDED_REPOS: _clean_list(user_input.get(CONF_EXCLUDED_REPOS)),
                    CONF_RUNNER_LABELS: _clean_list(user_input.get(CONF_RUNNER_LABELS))
                    or list(DEFAULT_RUNNER_LABELS),
                }
                return await self.async_step_server()
            errors["base"] = error
        schema = vol.Schema(
            {
                vol.Required(CONF_GITHUB_TOKEN): _password(),
                vol.Required(CONF_API_BASE, default=DEFAULT_API_BASE): str,
                vol.Required(CONF_OWNER): str,
                vol.Optional(CONF_EXCLUDED_REPOS, default=[]): _tags(),
                vol.Required(CONF_RUNNER_LABELS, default=list(DEFAULT_RUNNER_LABELS)): _tags(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    # ---------------------------------------------------------------- server

    def _server_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_BMC_HOST): str,
                vol.Required(CONF_BMC_USERNAME): str,
                vol.Required(CONF_BMC_PASSWORD): _password(),
                vol.Required(CONF_BMC_VERIFY_SSL, default=DEFAULT_BMC_VERIFY_SSL): bool,
                vol.Optional(CONF_WOL_MAC): str,
                vol.Required(CONF_SSH_HOST): str,
                vol.Required(CONF_SSH_PORT, default=DEFAULT_SSH_PORT): _number(1, 65535),
                vol.Required(CONF_SSH_USERNAME): str,
                vol.Required(CONF_SSH_KEY): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(CONF_SSH_KNOWN_HOST_KEY): str,
            }
        )

    def _show_server(self, errors: dict[str, str], values: dict[str, Any] | None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="server",
            data_schema=self.add_suggested_values_to_schema(self._server_schema(), values),
            errors=errors,
        )

    def _guard_client(self, data: Mapping[str, Any]) -> GuardClient:
        return GuardClient(
            data[CONF_SSH_HOST],
            int(data[CONF_SSH_PORT]),
            data[CONF_SSH_USERNAME],
            data[CONF_SSH_KEY],
            data.get(CONF_SSH_KNOWN_HOST_KEY),
        )

    async def async_step_server(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self._show_server({}, self._server or None)
        data = dict(user_input)
        data[CONF_SSH_PORT] = int(data.get(CONF_SSH_PORT, DEFAULT_SSH_PORT))
        for key in (CONF_WOL_MAC, CONF_SSH_KNOWN_HOST_KEY):
            if not (data.get(key) or "").strip():
                data.pop(key, None)
            else:
                data[key] = data[key].strip()
        self._server = data

        if CONF_WOL_MAC in data:
            try:
                build_magic_packet(data[CONF_WOL_MAC])
            except ValueError:
                return self._show_server({CONF_WOL_MAC: "invalid_mac"}, data)
        try:
            asyncssh.import_private_key(data[CONF_SSH_KEY])
        except (asyncssh.Error, ValueError, TypeError):
            return self._show_server({CONF_SSH_KEY: "invalid_ssh_key"}, data)

        verify = bool(data[CONF_BMC_VERIFY_SSL])
        redfish = RedfishClient(
            async_get_clientsession(self.hass, verify_ssl=verify),
            data[CONF_BMC_HOST],
            data[CONF_BMC_USERNAME],
            data[CONF_BMC_PASSWORD],
            verify,
        )
        try:
            await redfish.get_power_state()
        except BmcAuthError:
            return self._show_server({"base": "invalid_auth_bmc"}, data)
        except BmcError:
            return self._show_server({"base": "cannot_connect_bmc"}, data)
        except Exception:
            _LOGGER.exception("Unexpected error while checking the BMC")
            return self._show_server({"base": "unknown"}, data)

        if CONF_SSH_KNOWN_HOST_KEY not in data:
            try:
                key = await self._guard_client(data).fetch_host_key()
            except GuardError:
                return self._show_server({"base": "cannot_connect_guard"}, data)
            self._server[CONF_SSH_KNOWN_HOST_KEY] = key
            return await self.async_step_host_key()
        return await self._check_guard_and_create()

    async def async_step_host_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the fetched SSH host key; confirming pins it (trust on first use)."""
        if user_input is not None:
            return await self._check_guard_and_create()
        return self.async_show_form(
            step_id="host_key",
            data_schema=vol.Schema({}),
            description_placeholders={
                "host": self._server[CONF_SSH_HOST],
                "host_key": self._server[CONF_SSH_KNOWN_HOST_KEY],
            },
        )

    async def _check_guard_and_create(self) -> ConfigFlowResult:
        try:
            await self._guard_client(self._server).status()
        except GuardConnectionError:
            return self._show_server({"base": "cannot_connect_guard"}, self._server)
        except GuardError:
            return self._show_server({"base": "guard_protocol_error"}, self._server)
        except Exception:
            _LOGGER.exception("Unexpected error while checking the guard")
            return self._show_server({"base": "unknown"}, self._server)
        owner = self._github[CONF_OWNER]
        return self.async_create_entry(
            title=owner,
            data={**self._github, **self._server},
            options={CONF_AUTOMATIC: DEFAULT_AUTOMATIC, CONF_DRY_RUN: DEFAULT_DRY_RUN},
        )

    # ---------------------------------------------------------------- reauth

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_GITHUB_TOKEN].strip()
            error = await _validate_github(
                self.hass,
                token,
                entry.data.get(CONF_API_BASE, DEFAULT_API_BASE),
                entry.data[CONF_OWNER],
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_GITHUB_TOKEN: token}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_GITHUB_TOKEN): _password()}),
            description_placeholders={"owner": entry.data[CONF_OWNER]},
            errors=errors,
        )


# ---------------------------------------------------------------------- options


class HomerunnerOptionsFlow(OptionsFlow):
    """Rules, GitHub filters and the optional webhook."""

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    def _webhook_placeholders(self) -> dict[str, str]:
        wid = self.config_entry.options.get(CONF_WEBHOOK_ID)
        secret = self.config_entry.options.get(CONF_WEBHOOK_SECRET)
        if wid and secret:
            return {"webhook_url": webhook_url(self.hass, wid), "webhook_secret": secret}
        return {"webhook_url": "-", "webhook_secret": "-"}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        entry = self.config_entry
        if user_input is not None:
            options = {**entry.options, **user_input}
            options[CONF_EXCLUDED_REPOS] = _clean_list(user_input.get(CONF_EXCLUDED_REPOS))
            options[CONF_RUNNER_LABELS] = _clean_list(
                user_input.get(CONF_RUNNER_LABELS)
            ) or list(DEFAULT_RUNNER_LABELS)
            if options.get(CONF_USE_WEBHOOK) and not (
                options.get(CONF_WEBHOOK_ID) and options.get(CONF_WEBHOOK_SECRET)
            ):
                options[CONF_WEBHOOK_ID], options[CONF_WEBHOOK_SECRET] = generate_credentials()
            self._pending = options
            if options.get(CONF_USE_WEBHOOK):
                return await self.async_step_webhook_info()
            return self.async_create_entry(data=options)

        opt = entry.options

        def _o(key: str, default: Any) -> Any:
            return opt.get(key, default)

        schema = vol.Schema(
            {
                vol.Required(CONF_IDLE_HOURS, default=_o(CONF_IDLE_HOURS, DEFAULT_IDLE_HOURS)): _number(0.25, 24, 0.25),
                vol.Required(CONF_SHORT_IDLE_MINUTES, default=_o(CONF_SHORT_IDLE_MINUTES, DEFAULT_SHORT_IDLE_MINUTES)): _number(5, 240),
                vol.Required(CONF_MIN_UPTIME_MINUTES, default=_o(CONF_MIN_UPTIME_MINUTES, DEFAULT_MIN_UPTIME_MINUTES)): _number(0, 240),
                vol.Required(CONF_MAX_STARTS_PER_DAY, default=_o(CONF_MAX_STARTS_PER_DAY, DEFAULT_MAX_STARTS_PER_DAY)): _number(1, 48),
                vol.Required(CONF_FORCED_WAKE_DAYS, default=_o(CONF_FORCED_WAKE_DAYS, DEFAULT_FORCED_WAKE_DAYS)): _number(1, 13),
                vol.Required(CONF_FORCED_WAKE_MINUTES, default=_o(CONF_FORCED_WAKE_MINUTES, DEFAULT_FORCED_WAKE_MINUTES)): _number(5, 240),
                vol.Required(CONF_WAKE_TIMEOUT_MINUTES, default=_o(CONF_WAKE_TIMEOUT_MINUTES, DEFAULT_WAKE_TIMEOUT_MINUTES)): _number(1, 60),
                vol.Required(CONF_SCHEDULE_GUARD_MINUTES, default=_o(CONF_SCHEDULE_GUARD_MINUTES, DEFAULT_SCHEDULE_GUARD_MINUTES)): _number(0, 240),
                vol.Required(CONF_QUEUE_ALERT_MINUTES, default=_o(CONF_QUEUE_ALERT_MINUTES, DEFAULT_QUEUE_ALERT_MINUTES)): _number(1, 240),
                vol.Required(
                    CONF_EXCLUDED_REPOS,
                    default=list(get_conf(entry, CONF_EXCLUDED_REPOS, [])),
                ): _tags(),
                vol.Required(
                    CONF_RUNNER_LABELS,
                    default=list(get_conf(entry, CONF_RUNNER_LABELS, DEFAULT_RUNNER_LABELS)),
                ): _tags(),
                vol.Required(CONF_USE_WEBHOOK, default=bool(_o(CONF_USE_WEBHOOK, False))): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders=self._webhook_placeholders(),
        )

    async def async_step_webhook_info(self, user_input: dict[str, Any] | None = None) -> Any:
        """Show URL and secret to enter in the GitHub webhook settings."""
        if user_input is not None:
            return self.async_create_entry(data=self._pending)
        return self.async_show_form(
            step_id="webhook_info",
            data_schema=vol.Schema({}),
            description_placeholders={
                "webhook_url": webhook_url(self.hass, self._pending[CONF_WEBHOOK_ID]),
                "webhook_secret": self._pending[CONF_WEBHOOK_SECRET],
            },
        )


# ---------------------------------------------------------------------- schedules


class ScheduleSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a schedule (a workflow dispatch at fixed times)."""

    def _schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_REPOSITORY): str,
                vol.Required(CONF_WORKFLOW): str,
                vol.Required(CONF_REF, default=DEFAULT_REF): str,
                vol.Optional(CONF_INPUTS, default="{}"): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Required(CONF_TIME, default="03:00:00"): TimeSelector(),
                vol.Required(CONF_WEEKDAYS, default=[str(d) for d in DEFAULT_WEEKDAYS]): SelectSelector(
                    SelectSelectorConfig(
                        options=[str(d) for d in range(7)],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key="weekdays",
                    )
                ),
                vol.Required(CONF_TIMEZONE, default=self.hass.config.time_zone): str,
                vol.Required(CONF_PREWAKE, default=DEFAULT_PREWAKE): bool,
                vol.Required(CONF_PREWAKE_MINUTES, default=DEFAULT_PREWAKE_MINUTES): _number(0, 240),
                vol.Required(CONF_RETRIES, default=DEFAULT_RETRIES): _number(0, 10),
                vol.Required(CONF_ENABLED, default=True): bool,
                vol.Optional(CONF_DISPATCH_TOKEN): _password(),
            }
        )

    def _validate(self, user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        errors: dict[str, str] = {}
        data = dict(user_input)
        data[CONF_NAME] = data[CONF_NAME].strip()
        data[CONF_REPOSITORY] = data[CONF_REPOSITORY].strip()
        data[CONF_WORKFLOW] = data[CONF_WORKFLOW].strip()
        data[CONF_REF] = data[CONF_REF].strip()
        if not _REPO_RE.match(data[CONF_REPOSITORY]):
            errors[CONF_REPOSITORY] = "invalid_repository"
        raw = (data.get(CONF_INPUTS) or "").strip() or "{}"
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError
            data[CONF_INPUTS] = json.dumps(parsed)
        except ValueError:
            errors[CONF_INPUTS] = "invalid_inputs"
        try:
            ZoneInfo(data[CONF_TIMEZONE].strip())
            data[CONF_TIMEZONE] = data[CONF_TIMEZONE].strip()
        except (KeyError, ValueError, OSError):
            errors[CONF_TIMEZONE] = "invalid_timezone"
        weekdays = sorted({int(d) for d in data.get(CONF_WEEKDAYS) or []})
        if not weekdays:
            errors[CONF_WEEKDAYS] = "no_weekdays"
        data[CONF_WEEKDAYS] = weekdays
        data[CONF_PREWAKE_MINUTES] = int(data[CONF_PREWAKE_MINUTES])
        data[CONF_RETRIES] = int(data[CONF_RETRIES])
        if not (data.get(CONF_DISPATCH_TOKEN) or "").strip():
            data.pop(CONF_DISPATCH_TOKEN, None)
        else:
            data[CONF_DISPATCH_TOKEN] = data[CONF_DISPATCH_TOKEN].strip()
        return data, errors

    def _show(
        self, step_id: str, values: dict[str, Any] | None, errors: dict[str, str]
    ) -> SubentryFlowResult:
        if values and CONF_WEEKDAYS in values:
            values = {**values, CONF_WEEKDAYS: [str(d) for d in values[CONF_WEEKDAYS]]}
        if values and isinstance(values.get(CONF_INPUTS), dict):
            values = {**values, CONF_INPUTS: json.dumps(values[CONF_INPUTS])}
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(self._schema(), values),
            errors=errors,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = self._validate(user_input)
            if not errors:
                return self.async_create_entry(title=data[CONF_NAME], data=data)
            user_input = data
        return self._show("user", user_input, errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = self._validate(user_input)
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=data[CONF_NAME], data=data
                )
            user_input = data
        return self._show("reconfigure", user_input or dict(subentry.data), errors)
