"""Webhook signature check and diagnostics redaction."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentryData

from custom_components.homerunner.const import (
    CONF_USE_WEBHOOK,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    SUBENTRY_SCHEDULE,
)
from custom_components.homerunner.diagnostics import async_get_config_entry_diagnostics
from custom_components.homerunner.webhook import verify_signature

from .conftest import ENTRY_DATA

SECRET = "hook-secret"
HOOK_ID = "abcdef0123456789"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature() -> None:
    body = b'{"a": 1}'
    assert verify_signature(SECRET, body, _sign(body))
    assert not verify_signature(SECRET, body, _sign(body, "other"))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=abc")
    assert not verify_signature("", body, _sign(body, ""))


@pytest.mark.parametrize(
    "entry_options",
    [{CONF_USE_WEBHOOK: True, CONF_WEBHOOK_ID: HOOK_ID, CONF_WEBHOOK_SECRET: SECRET}],
)
async def test_webhook_triggers_refresh_for_matching_jobs(
    hass, mock_entry, mocks, hass_client_no_auth
) -> None:
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    queue = mock_entry.runtime_data.queue
    queue.async_request_refresh = AsyncMock()
    client = await hass_client_no_auth()
    url = f"/api/webhook/{HOOK_ID}"

    def payload(labels):
        return json.dumps({"action": "queued", "workflow_job": {"labels": labels}}).encode()

    body = payload(["self-hosted"])
    resp = await client.post(
        url,
        data=body,
        headers={"X-GitHub-Event": "workflow_job", "X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status == 200
    await hass.async_block_till_done()
    queue.async_request_refresh.assert_awaited_once()

    # a bad signature is rejected and does nothing
    queue.async_request_refresh.reset_mock()
    resp = await client.post(
        url,
        data=body,
        headers={"X-GitHub-Event": "workflow_job", "X-Hub-Signature-256": _sign(body, "wrong")},
    )
    assert resp.status == 401
    resp = await client.post(url, data=body, headers={"X-GitHub-Event": "workflow_job"})
    assert resp.status == 401

    # a job for other runners is ignored
    other = payload(["ubuntu-latest"])
    resp = await client.post(
        url,
        data=other,
        headers={"X-GitHub-Event": "workflow_job", "X-Hub-Signature-256": _sign(other)},
    )
    assert resp.status == 200
    # ping and unrelated events are acknowledged
    ping = b"{}"
    resp = await client.post(
        url, data=ping, headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _sign(ping)}
    )
    assert resp.status == 200
    await hass.async_block_till_done()
    queue.async_request_refresh.assert_not_awaited()

    # unloading removes the webhook
    await hass.config_entries.async_unload(mock_entry.entry_id)
    resp = await client.post(
        url,
        data=body,
        headers={"X-GitHub-Event": "workflow_job", "X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status == 200  # unknown ids answer 200 by design of HA
    queue.async_request_refresh.assert_not_awaited()


async def test_diagnostics_redacts_secrets(hass, mocks, entry_options) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain="homerunner",
        title="Octo-Org",
        data=dict(ENTRY_DATA),
        options={**entry_options, CONF_WEBHOOK_ID: HOOK_ID, CONF_WEBHOOK_SECRET: SECRET},
        subentries_data=[
            ConfigSubentryData(
                data={
                    "name": "n",
                    "repository": "o/r",
                    "workflow": "w.yml",
                    "ref": "main",
                    "time": "03:00:00",
                    "dispatch_token": "ghp_dispatch_secret",
                },
                subentry_type=SUBENTRY_SCHEDULE,
                title="n",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag, default=str)
    for secret in (
        "ghp_secret_token",
        "bmc_secret_pw",
        "BEGIN OPENSSH PRIVATE KEY",
        "ghp_dispatch_secret",
        SECRET,
        HOOK_ID,
        "bmc.example",
        "runner.example",
    ):
        assert secret not in text
    assert diag["entry"]["data"]["github_token"] == "**REDACTED**"
    assert diag["machine"]["state"] == "off"
    assert diag["queue"]["ok"] is True
