"""Diagnostics with secrets redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BMC_HOST,
    CONF_BMC_PASSWORD,
    CONF_BMC_USERNAME,
    CONF_DISPATCH_TOKEN,
    CONF_GITHUB_TOKEN,
    CONF_SSH_HOST,
    CONF_SSH_KEY,
    CONF_SSH_USERNAME,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    CONF_WOL_MAC,
)
from .models import HomerunnerConfigEntry

TO_REDACT = {
    CONF_GITHUB_TOKEN,
    CONF_BMC_PASSWORD,
    CONF_SSH_KEY,
    CONF_DISPATCH_TOKEN,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_ID,
    CONF_BMC_HOST,
    CONF_BMC_USERNAME,
    CONF_SSH_HOST,
    CONF_SSH_USERNAME,
    CONF_WOL_MAC,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HomerunnerConfigEntry
) -> dict[str, Any]:
    hr = entry.runtime_data
    queue = hr.queue.data
    server = hr.server.data
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
            "subentries": [
                {
                    "subentry_id": sub.subentry_id,
                    "type": sub.subentry_type,
                    "title": sub.title,
                    "data": async_redact_data(dict(sub.data), TO_REDACT),
                }
                for sub in entry.subentries.values()
            ],
        },
        "machine": hr.manager.machine.as_dict(),
        "blockers": list(hr.manager.machine.blockers),
        "queue": None
        if queue is None
        else {
            "ok": queue.ok,
            "error": queue.error,
            "queued": len(queue.queued),
            "running": len(queue.running),
            "repos_watched": queue.repos_watched,
            "throttled": queue.throttled,
            "rate_limit": {
                "limit": queue.rate_limit.limit,
                "remaining": queue.rate_limit.remaining,
                "reset_at": queue.rate_limit.reset_at,
            },
        },
        "server": None
        if server is None
        else {
            "power": server.power.value,
            "watts": server.watts,
            "bmc_ok": server.bmc_ok,
            "guard_ok": server.guard_ok,
            "error": server.error,
            "runners": [
                {"name": r.name, "active": r.active, "job": r.job}
                for r in (server.guard.runners if server.guard else [])
            ],
        },
        "schedules": hr.runner.dump(),
    }
