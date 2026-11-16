"""Optional GitHub ``workflow_job`` webhook: refreshes the queue as soon as a job is queued."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .const import (
    CONF_RUNNER_LABELS,
    CONF_USE_WEBHOOK,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    DEFAULT_RUNNER_LABELS,
    DOMAIN,
)
from .models import get_conf
from .queue_watcher import job_matches

_LOGGER = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"


def generate_credentials() -> tuple[str, str]:
    """A fresh (webhook id, secret) pair."""
    return secrets.token_hex(16), secrets.token_urlsafe(32)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check ``X-Hub-Signature-256`` (HMAC-SHA256 over the raw body)."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def webhook_url(hass: HomeAssistant, webhook_id: str) -> str:
    """The URL to enter at GitHub (falls back to the path if no external URL is known)."""
    try:
        return webhook.async_generate_url(hass, webhook_id, allow_internal=False)
    except Exception:
        return webhook.async_generate_path(webhook_id)


async def async_register_webhook(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Register the webhook if enabled in the options; returns its id."""
    if not entry.options.get(CONF_USE_WEBHOOK):
        return None
    webhook_id = entry.options.get(CONF_WEBHOOK_ID)
    secret = entry.options.get(CONF_WEBHOOK_SECRET)
    if not webhook_id or not secret:
        _LOGGER.warning("Webhook enabled without id/secret, not registering")
        return None
    await async_setup_component(hass, "webhook", {})
    labels = list(get_conf(entry, CONF_RUNNER_LABELS, DEFAULT_RUNNER_LABELS))

    async def _handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        body = await request.read()
        if not verify_signature(secret, body, request.headers.get(SIGNATURE_HEADER)):
            _LOGGER.warning("Rejected webhook call with an invalid signature")
            return web.Response(status=401)
        event = request.headers.get(EVENT_HEADER)
        if event == "ping":
            return web.Response(status=200, text="pong")
        if event != "workflow_job":
            return web.Response(status=200)
        try:
            job = json.loads(body)["workflow_job"]
            job_labels = [str(label) for label in job.get("labels") or []]
        except (ValueError, KeyError, TypeError, AttributeError):
            return web.Response(status=400)
        if job_matches(job_labels, labels):
            hass.async_create_task(entry.runtime_data.queue.async_request_refresh())
        return web.Response(status=200)

    webhook.async_register(
        hass,
        DOMAIN,
        f"Homerunner {entry.title}",
        webhook_id,
        _handle,
        local_only=False,
        allowed_methods=["POST"],
    )
    return webhook_id


def async_unregister_webhook(hass: HomeAssistant, webhook_id: str | None) -> None:
    """Remove the webhook registered by :func:`async_register_webhook`."""
    if webhook_id:
        webhook.async_unregister(hass, webhook_id)
