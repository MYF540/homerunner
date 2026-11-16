"""Scan all repositories of an owner for jobs waiting on / running on our runners."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.util.dt import utcnow

from .github import (
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    Job,
    RateLimit,
    Repo,
)

_LOGGER = logging.getLogger(__name__)

REPO_REFRESH_INTERVAL = timedelta(hours=1)
RECENT_PUSH_WINDOW = timedelta(days=30)
OLD_REPO_INTERVAL = timedelta(minutes=10)
THROTTLE_FRACTION = 0.10
MAX_CONCURRENCY = 8

QUEUED_STATUSES = frozenset({"queued", "waiting", "pending", "requested"})
RUNNING_STATUSES = frozenset({"in_progress"})


def job_matches(job_labels: Iterable[str], runner_labels: Iterable[str]) -> bool:
    """True if the job has labels and all of them are runner labels (case-insensitive)."""
    wanted = {str(label).lower() for label in job_labels}
    if not wanted:
        return False
    available = {str(label).lower() for label in runner_labels}
    return wanted <= available


@dataclass
class QueueSnapshot:
    """Result of one scan."""

    queued: list[Job]
    running: list[Job]
    ok: bool
    error: str | None
    rate_limit: RateLimit
    repos_watched: int
    scanned_at: datetime
    throttled: bool = False


class QueueWatcher:
    """Polls GitHub for queued and running jobs matching the runner labels."""

    def __init__(
        self,
        client: GitHubClient,
        owner: str,
        runner_labels: list[str],
        excluded: list[str],
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = client
        self._owner = owner
        self._labels = [label.lower() for label in runner_labels]
        self._excluded = {name.lower() for name in excluded}
        self._now = now
        self._repos: list[Repo] | None = None
        self._repos_at: datetime | None = None
        self._last_scanned: dict[str, datetime] = {}
        self._jobs: dict[str, list[Job]] = {}

    def _excluded_repo(self, repo: Repo) -> bool:
        return (
            repo.name.lower() in self._excluded
            or repo.full_name.lower() in self._excluded
        )

    async def _refresh_repos(self, now: datetime) -> None:
        if (
            self._repos is not None
            and self._repos_at is not None
            and now - self._repos_at < REPO_REFRESH_INTERVAL
        ):
            return
        repos = await self._client.list_repos(self._owner)
        self._repos = [r for r in repos if not r.archived and not self._excluded_repo(r)]
        self._repos_at = now
        names = {r.full_name for r in self._repos}
        for stale in set(self._jobs) - names:
            del self._jobs[stale]
            self._last_scanned.pop(stale, None)

    def _is_throttled(self) -> bool:
        rl = self._client.rate_limit
        return (
            rl.limit is not None
            and rl.remaining is not None
            and rl.limit > 0
            and rl.remaining < rl.limit * THROTTLE_FRACTION
        )

    def _is_recent(self, repo: Repo, now: datetime) -> bool:
        return repo.pushed_at is None or now - repo.pushed_at <= RECENT_PUSH_WINDOW

    async def _scan_repo(self, repo: Repo) -> list[Job]:
        """Return the matching queued/running jobs of one repo."""
        run_ids: list[int] = []
        for status in ("queued", "in_progress"):
            for run in await self._client.list_runs(repo.full_name, status):
                run_id = int(run["id"])
                if run_id not in run_ids:
                    run_ids.append(run_id)
        found: dict[int, Job] = {}
        for run_id in run_ids:
            for job in await self._client.list_run_jobs(repo.full_name, run_id):
                status = job.status.lower()
                if (
                    status in QUEUED_STATUSES | RUNNING_STATUSES
                    and job_matches(job.labels, self._labels)
                ):
                    found[job.id] = job
        return list(found.values())

    async def scan(self) -> QueueSnapshot:
        """Run one scan. Never raises GitHub errors; reports them via ok/error."""
        now = self._now()
        error: str | None = None
        throttled = False
        try:
            await self._refresh_repos(now)
            repos = self._repos or []
            throttled = self._is_throttled()
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            todo: list[Repo] = []
            for repo in repos:
                if not self._is_recent(repo, now):
                    last = self._last_scanned.get(repo.full_name)
                    if throttled or (last is not None and now - last < OLD_REPO_INTERVAL):
                        continue
                todo.append(repo)

            async def run(repo: Repo) -> tuple[Repo, list[Job] | None, GitHubError | None]:
                async with sem:
                    try:
                        return repo, await self._scan_repo(repo), None
                    except GitHubNotFoundError:
                        _LOGGER.debug("Repository %s not found, ignoring", repo.full_name)
                        return repo, [], None
                    except GitHubError as err:
                        return repo, None, err

            for repo, jobs, err in await asyncio.gather(*(run(r) for r in todo)):
                if err is not None:
                    error = error or f"{repo.full_name}: {err}"
                    continue
                self._jobs[repo.full_name] = jobs or []
                self._last_scanned[repo.full_name] = now
        except GitHubError as err:
            error = str(err) or type(err).__name__
        if error:
            _LOGGER.warning("GitHub queue scan failed: %s", error)

        queued: list[Job] = []
        running: list[Job] = []
        for repo in self._repos or []:
            for job in self._jobs.get(repo.full_name, []):
                status = job.status.lower()
                if status in RUNNING_STATUSES:
                    running.append(job)
                elif status in QUEUED_STATUSES:
                    queued.append(job)
        return QueueSnapshot(
            queued=queued,
            running=running,
            ok=error is None,
            error=error,
            rate_limit=self._client.rate_limit,
            repos_watched=len(self._repos or []),
            scanned_at=now,
            throttled=throttled,
        )
