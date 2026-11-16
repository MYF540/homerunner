"""Shared pytest configuration."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading the custom integration in every test."""
    yield


# ---------------------------------------------------------------------- Home Assistant layer

from collections.abc import Generator  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.homerunner.bmc import PowerState  # noqa: E402
from custom_components.homerunner.const import (  # noqa: E402
    CONF_API_BASE,
    CONF_AUTOMATIC,
    CONF_BMC_HOST,
    CONF_BMC_PASSWORD,
    CONF_BMC_USERNAME,
    CONF_BMC_VERIFY_SSL,
    CONF_DRY_RUN,
    CONF_EXCLUDED_REPOS,
    CONF_GITHUB_TOKEN,
    CONF_OWNER,
    CONF_RUNNER_LABELS,
    CONF_SSH_HOST,
    CONF_SSH_KEY,
    CONF_SSH_KNOWN_HOST_KEY,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    DOMAIN,
)
from custom_components.homerunner.github import Job, RateLimit  # noqa: E402
from custom_components.homerunner.guard import (  # noqa: E402
    GuardStatus,
    RunnerStatus,
    ShutdownResult,
)
from custom_components.homerunner.queue_watcher import QueueSnapshot  # noqa: E402

ENTRY_DATA = {
    CONF_GITHUB_TOKEN: "ghp_secret_token",
    CONF_API_BASE: "https://api.github.com",
    CONF_OWNER: "Octo-Org",
    CONF_EXCLUDED_REPOS: [],
    CONF_RUNNER_LABELS: ["self-hosted"],
    CONF_BMC_HOST: "bmc.example",
    CONF_BMC_USERNAME: "bmcuser",
    CONF_BMC_PASSWORD: "bmc_secret_pw",
    CONF_BMC_VERIFY_SSL: False,
    CONF_SSH_HOST: "runner.example",
    CONF_SSH_PORT: 22,
    CONF_SSH_USERNAME: "guard",
    CONF_SSH_KEY: "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----",
    CONF_SSH_KNOWN_HOST_KEY: "ssh-ed25519 AAAAFAKE",
}


def make_job(job_id: int = 1, status: str = "queued", repo: str = "octo-org/app") -> Job:
    return Job(
        id=job_id,
        run_id=100 + job_id,
        repo=repo,
        name=f"job {job_id}",
        status=status,
        labels=("self-hosted",),
        html_url=f"https://github.example/{repo}/runs/{job_id}",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=None,
    )


def make_snapshot(queued=(), running=(), ok=True, auth_failed=False) -> QueueSnapshot:
    return QueueSnapshot(
        queued=list(queued),
        running=list(running),
        ok=ok,
        error=None if ok else "boom",
        rate_limit=RateLimit(5000, 4900, None),
        repos_watched=3,
        scanned_at=datetime.now(UTC),
        auth_failed=auth_failed,
    )


def make_guard_status(active: bool = True) -> GuardStatus:
    return GuardStatus(
        hostname="runner",
        runners=[RunnerStatus("r1", "svc", active, False)],
        users=[],
        inhibit=False,
        uptime_seconds=100,
    )


class Mocks:
    """The mocked outside world of the integration."""

    def __init__(self) -> None:
        self.snapshot = make_snapshot()
        self.watcher = MagicMock()
        self.watcher.scan = AsyncMock(side_effect=lambda: self.snapshot)
        self.github = MagicMock()
        self.github.rate_limit = RateLimit(5000, 4900, None)
        self.github.dispatch = AsyncMock(return_value=None)
        self.github.get_run = AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "html_url": "https://x/run"}
        )
        self.redfish = MagicMock()
        self.redfish.get_power_state = AsyncMock(return_value=PowerState.OFF)
        self.redfish.get_power_watts = AsyncMock(return_value=12.0)
        self.redfish.power_on = AsyncMock()
        self.redfish.power_off = AsyncMock()
        self.guard = MagicMock()
        self.guard.status = AsyncMock(return_value=make_guard_status())
        self.guard.shutdown = AsyncMock(return_value=ShutdownResult(True, None, None))
        self.guard.fetch_host_key = AsyncMock(return_value="ssh-ed25519 AAAAFETCHED")


@pytest.fixture
def mocks() -> Generator[Mocks]:
    m = Mocks()
    base = "custom_components.homerunner"
    with (
        patch(f"{base}.GitHubClient", return_value=m.github),
        patch(f"{base}.QueueWatcher", return_value=m.watcher),
        patch(f"{base}.RedfishClient", return_value=m.redfish),
        patch(f"{base}.GuardClient", return_value=m.guard),
        patch(f"{base}.config_flow.GitHubClient", return_value=m.github),
        patch(f"{base}.config_flow.RedfishClient", return_value=m.redfish),
        patch(f"{base}.config_flow.GuardClient", return_value=m.guard),
        patch(f"{base}.config_flow.asyncssh.import_private_key", return_value=MagicMock()),
        patch(f"{base}.manager.send_magic_packet", new=AsyncMock()) as wol,
    ):
        m.wol = wol
        m.github.validate = AsyncMock()
        yield m


@pytest.fixture
def entry_options() -> dict:
    return {CONF_AUTOMATIC: False, CONF_DRY_RUN: True}


@pytest.fixture
def mock_entry(hass, entry_options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Octo-Org",
        unique_id="octo-org",
        data=dict(ENTRY_DATA),
        options=dict(entry_options),
    )
    entry.add_to_hass(hass)
    return entry
