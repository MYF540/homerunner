"""Config flow, reauth, options flow and schedule subentries."""

from homeassistant.data_entry_flow import FlowResultType

from custom_components.homerunner.bmc import BmcAuthError, BmcConnectionError
from custom_components.homerunner.const import (
    CONF_AUTOMATIC,
    CONF_DRY_RUN,
    CONF_GITHUB_TOKEN,
    CONF_SSH_KNOWN_HOST_KEY,
    DOMAIN,
    SUBENTRY_SCHEDULE,
)
from custom_components.homerunner.github import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubTransientError,
)
from custom_components.homerunner.guard import GuardConnectionError, GuardProtocolError

GITHUB_INPUT = {
    "github_token": "ghp_new",
    "api_base": "https://api.github.com",
    "owner": "Octo-Org",
    "excluded_repos": ["old-repo"],
    "runner_labels": ["self-hosted", "Linux"],
}
SERVER_INPUT = {
    "bmc_host": "bmc.example",
    "bmc_username": "u",
    "bmc_password": "p",
    "bmc_verify_ssl": False,
    "ssh_host": "runner.example",
    "ssh_port": 22,
    "ssh_username": "guard",
    "ssh_private_key": "PRIVATE KEY TEXT",
}


async def _to_server_step(hass, mocks):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], GITHUB_INPUT)
    assert result["step_id"] == "server"
    return result


async def test_happy_path_pins_fetched_host_key(hass, mocks) -> None:
    result = await _to_server_step(hass, mocks)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SERVER_INPUT)
    assert result["step_id"] == "host_key"
    assert "AAAAFETCHED" in result["description_placeholders"]["host_key"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Octo-Org"
    assert result["data"][CONF_SSH_KNOWN_HOST_KEY] == "ssh-ed25519 AAAAFETCHED"
    assert result["data"]["excluded_repos"] == ["old-repo"]
    assert result["result"].options == {CONF_AUTOMATIC: False, CONF_DRY_RUN: True}
    assert result["result"].unique_id == "octo-org"
    mocks.github.validate.assert_awaited_once_with("Octo-Org")
    await hass.async_block_till_done()


async def test_happy_path_with_given_host_key(hass, mocks) -> None:
    result = await _to_server_step(hass, mocks)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**SERVER_INPUT, "ssh_host_key": "ssh-ed25519 AAAAGIVEN", "wol_mac": "aa:bb:cc:dd:ee:ff"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["wol_mac"] == "aa:bb:cc:dd:ee:ff"
    mocks.guard.fetch_host_key.assert_not_awaited()
    await hass.async_block_till_done()


async def test_github_errors(hass, mocks) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    for exc, error in (
        (GitHubAuthError("bad", 401), "invalid_auth"),
        (GitHubNotFoundError("nf", 404), "owner_not_found"),
        (GitHubTransientError("net"), "cannot_connect"),
    ):
        mocks.github.validate.side_effect = exc
        result = await hass.config_entries.flow.async_configure(result["flow_id"], GITHUB_INPUT)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}
    # and the user can recover
    mocks.github.validate.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], GITHUB_INPUT)
    assert result["step_id"] == "server"


async def test_bmc_errors(hass, mocks) -> None:
    result = await _to_server_step(hass, mocks)
    mocks.redfish.get_power_state.side_effect = BmcAuthError("401")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SERVER_INPUT)
    assert result["errors"] == {"base": "invalid_auth_bmc"}
    mocks.redfish.get_power_state.side_effect = BmcConnectionError("x")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SERVER_INPUT)
    assert result["errors"] == {"base": "cannot_connect_bmc"}


async def test_invalid_mac(hass, mocks) -> None:
    result = await _to_server_step(hass, mocks)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVER_INPUT, "wol_mac": "nope"}
    )
    assert result["errors"] == {"wol_mac": "invalid_mac"}


async def test_guard_errors_let_the_user_retry(hass, mocks) -> None:
    result = await _to_server_step(hass, mocks)
    mocks.guard.status.side_effect = GuardConnectionError("ssh")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SERVER_INPUT)
    assert result["step_id"] == "host_key"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "server" and result["errors"] == {"base": "cannot_connect_guard"}

    mocks.guard.status.side_effect = GuardProtocolError("odd")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVER_INPUT, "ssh_host_key": "ssh-ed25519 AAAA"}
    )
    assert result["errors"] == {"base": "guard_protocol_error"}

    mocks.guard.status.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVER_INPUT, "ssh_host_key": "ssh-ed25519 AAAA"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_host_key_fetch_failure(hass, mocks) -> None:
    result = await _to_server_step(hass, mocks)
    mocks.guard.fetch_host_key.side_effect = GuardConnectionError("no route")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SERVER_INPUT)
    assert result["errors"] == {"base": "cannot_connect_guard"}


async def test_already_configured(hass, mock_entry, mocks) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], GITHUB_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass, mock_entry, mocks) -> None:
    result = await mock_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mocks.github.validate.side_effect = GitHubAuthError("bad", 401)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    mocks.github.validate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "ghp_renewed"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert mock_entry.data[CONF_GITHUB_TOKEN] == "ghp_renewed"
    await hass.async_block_till_done()


# ---------------------------------------------------------------------- options


async def test_options_flow_rules_without_reload(hass, mock_entry, mocks) -> None:
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    data = mock_entry.runtime_data
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["step_id"] == "init"
    values = {
        "idle_hours": 4,
        "short_idle_minutes": 30,
        "min_uptime_minutes": 10,
        "max_starts_per_day": 5,
        "forced_wake_days": 6,
        "forced_wake_minutes": 40,
        "wake_timeout_minutes": 12,
        "schedule_guard_minutes": 20,
        "queue_alert_minutes": 25,
        "excluded_repos": [],
        "runner_labels": ["self-hosted"],
        "use_webhook": False,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert mock_entry.runtime_data is data  # no reload
    assert data.manager.machine.rules.idle_hours == 4
    assert data.manager.machine.rules.max_starts_per_day == 5
    assert mock_entry.options[CONF_DRY_RUN] is True  # untouched keys survive


async def test_options_flow_filters_reload_and_webhook(hass, mock_entry, mocks) -> None:
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    old = mock_entry.runtime_data
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert "use_webhook" in schema_keys
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "idle_hours": 2,
            "short_idle_minutes": 20,
            "min_uptime_minutes": 15,
            "max_starts_per_day": 8,
            "forced_wake_days": 5,
            "forced_wake_minutes": 30,
            "wake_timeout_minutes": 10,
            "schedule_guard_minutes": 30,
            "queue_alert_minutes": 15,
            "excluded_repos": ["x"],
            "runner_labels": ["self-hosted", "gpu"],
            "use_webhook": True,
        },
    )
    assert result["step_id"] == "webhook_info"
    assert result["description_placeholders"]["webhook_secret"]
    assert "/api/webhook/" in result["description_placeholders"]["webhook_url"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert mock_entry.options["excluded_repos"] == ["x"]
    assert mock_entry.options["webhook_id"] and mock_entry.options["webhook_secret"]
    assert mock_entry.runtime_data is not old  # reloaded
    assert mock_entry.runtime_data.webhook_id == mock_entry.options["webhook_id"]


# ---------------------------------------------------------------------- schedules

SCHEDULE_INPUT = {
    "name": "Nightly",
    "repository": "octo-org/app",
    "workflow": "nightly.yml",
    "ref": "dev",
    "inputs": '{"planned": "true"}',
    "time": "03:00:00",
    "weekdays": ["0", "1", "2"],
    "timezone": "Europe/Berlin",
    "prewake": True,
    "prewake_minutes": 15,
    "retries": 5,
    "enabled": True,
    "dispatch_token": "ghp_dispatch",
}


async def test_subentry_add_and_reconfigure(hass, mock_entry, mocks) -> None:
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (mock_entry.entry_id, SUBENTRY_SCHEDULE), context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**SCHEDULE_INPUT, "inputs": "{not json"}
    )
    assert result["errors"] == {"inputs": "invalid_inputs"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**SCHEDULE_INPUT, "inputs": "[1]", "repository": "nope", "timezone": "Mars/Base"}
    )
    assert result["errors"] == {
        "repository": "invalid_repository",
        "inputs": "invalid_inputs",
        "timezone": "invalid_timezone",
    }
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], SCHEDULE_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    (subentry,) = mock_entry.subentries.values()
    assert subentry.title == "Nightly" and subentry.subentry_type == SUBENTRY_SCHEDULE
    assert subentry.data["weekdays"] == [0, 1, 2]
    assert subentry.data["inputs"] == '{"planned": "true"}'
    assert subentry.data["dispatch_token"] == "ghp_dispatch"
    runner = mock_entry.runtime_data.runner
    assert subentry.subentry_id in runner.schedules  # reload picked it up
    assert hass.states.get("switch.schedule_nightly_enabled") is not None

    result = await hass.config_entries.subentries.async_init(
        (mock_entry.entry_id, SUBENTRY_SCHEDULE),
        context={"source": "reconfigure", "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**SCHEDULE_INPUT, "name": "Nightly 2", "ref": "main", "weekdays": ["6"], "dispatch_token": ""}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    sub = mock_entry.subentries[subentry.subentry_id]
    assert sub.title == "Nightly 2" and sub.data["ref"] == "main" and sub.data["weekdays"] == [6]
    assert "dispatch_token" not in sub.data
    assert mock_entry.runtime_data.runner.schedules[sub.subentry_id].ref == "main"


async def test_subentry_needs_a_weekday(hass, mock_entry, mocks) -> None:
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_init(
        (mock_entry.entry_id, SUBENTRY_SCHEDULE), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**SCHEDULE_INPUT, "weekdays": []}
    )
    assert result["errors"] == {"weekdays": "no_weekdays"}
