"""Runtime data of a Homerunner config entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .bmc import RedfishClient
    from .coordinator import QueueCoordinator, ServerCoordinator
    from .github import GitHubClient
    from .guard import GuardClient
    from .manager import HomerunnerManager
    from .schedule_runner import ScheduleRunner


@dataclass
class HomerunnerData:
    """Everything a running config entry owns."""

    github: GitHubClient
    redfish: RedfishClient
    guard: GuardClient
    queue: QueueCoordinator
    server: ServerCoordinator
    manager: HomerunnerManager
    runner: ScheduleRunner
    signature: tuple
    webhook_id: str | None = None


type HomerunnerConfigEntry = ConfigEntry[HomerunnerData]


def get_conf(entry: ConfigEntry, key: str, default=None):
    """Read a setting: options override the data the entry was created with."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)
