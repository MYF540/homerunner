"""Tests for the queue watcher."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.homerunner.github import (
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
    Job,
    RateLimit,
    Repo,
)
from custom_components.homerunner.queue_watcher import QueueWatcher, job_matches

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(days=1)
OLD = NOW - timedelta(days=90)


def repo(name: str, pushed: datetime | None = RECENT, archived: bool = False) -> Repo:
    return Repo(f"org/{name}", name, archived, pushed)


def job(jid: int, run_id: int, repo_name: str, status: str, labels: tuple[str, ...]) -> Job:
    return Job(jid, run_id, f"org/{repo_name}", f"job{jid}", status, labels, None, NOW, None)


class FakeClient:
    def __init__(self, repos: list[Repo]) -> None:
        self.repos = repos
        self.runs: dict[tuple[str, str], list[dict]] = {}
        self.jobs: dict[tuple[str, int], list[Job]] = {}
        self.errors: dict[str, Exception] = {}
        self.rate_limit = RateLimit(5000, 4000, None)
        self.list_repos_calls = 0
        self.run_calls: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0

    async def list_repos(self, owner: str) -> list[Repo]:
        assert owner == "org"
        self.list_repos_calls += 1
        if "repos" in self.errors:
            raise self.errors["repos"]
        return self.repos

    async def list_runs(self, repo_name: str, status: str) -> list[dict]:
        import asyncio

        self.run_calls.append((repo_name, status))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if repo_name in self.errors:
            raise self.errors[repo_name]
        return self.runs.get((repo_name, status), [])

    async def list_run_jobs(self, repo_name: str, run_id: int) -> list[Job]:
        return self.jobs.get((repo_name, run_id), [])


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> datetime:
        return self.t


def make(
    client: FakeClient, labels=("self-hosted", "linux"), excluded=(), clock=None
) -> QueueWatcher:
    return QueueWatcher(client, "org", list(labels), list(excluded), clock or Clock())  # type: ignore[arg-type]


def test_job_matches() -> None:
    assert job_matches(["self-hosted", "Linux"], ["SELF-hosted", "linux", "x64"])
    assert job_matches(["linux"], ["self-hosted", "linux"])  # subset rule
    assert not job_matches(["self-hosted", "gpu"], ["self-hosted", "linux"])
    assert not job_matches([], ["self-hosted"])
    assert not job_matches(["ubuntu-latest"], [])
    assert not job_matches([], [])


@pytest.mark.asyncio
async def test_queued_vs_running_and_label_filter() -> None:
    c = FakeClient([repo("a")])
    c.runs[("org/a", "queued")] = [{"id": 1}]
    c.runs[("org/a", "in_progress")] = [{"id": 2}, {"id": 1}]  # run 1 appears in both lists
    c.jobs[("org/a", 1)] = [
        job(1, 1, "a", "queued", ("self-hosted", "linux")),
        job(2, 1, "a", "waiting", ("Self-Hosted",)),
        job(3, 1, "a", "queued", ("ubuntu-latest",)),
        job(4, 1, "a", "completed", ("self-hosted",)),
        job(5, 1, "a", "queued", ()),
    ]
    c.jobs[("org/a", 2)] = [
        job(6, 2, "a", "in_progress", ("self-hosted", "linux")),
        job(7, 2, "a", "pending", ("linux",)),
        job(8, 2, "a", "requested", ("linux",)),
        job(9, 2, "a", "in_progress", ("windows",)),
    ]
    snap = await make(c).scan()
    assert snap.ok and snap.error is None and not snap.throttled
    assert sorted(j.id for j in snap.queued) == [1, 2, 7, 8]
    assert [j.id for j in snap.running] == [6]
    assert snap.repos_watched == 1
    assert snap.scanned_at == NOW
    assert snap.rate_limit is c.rate_limit


@pytest.mark.asyncio
async def test_excluded_and_archived_repos() -> None:
    c = FakeClient([repo("a"), repo("skip"), repo("old", archived=True), repo("b")])
    snap = await make(c, excluded=["Skip"]).scan()
    assert snap.repos_watched == 2
    assert {r for r, _ in c.run_calls} == {"org/a", "org/b"}


@pytest.mark.asyncio
async def test_repo_list_refreshed_hourly() -> None:
    clock = Clock()
    c = FakeClient([repo("a")])
    w = make(c, clock=clock)
    await w.scan()
    clock.t += timedelta(minutes=59)
    await w.scan()
    assert c.list_repos_calls == 1
    clock.t += timedelta(minutes=2)
    await w.scan()
    assert c.list_repos_calls == 2


@pytest.mark.asyncio
async def test_old_repos_every_ten_minutes_and_jobs_kept() -> None:
    clock = Clock()
    c = FakeClient([repo("new"), repo("old", pushed=OLD)])
    c.runs[("org/old", "queued")] = [{"id": 1}]
    c.jobs[("org/old", 1)] = [job(1, 1, "old", "queued", ("self-hosted",))]
    w = make(c, clock=clock)
    snap = await w.scan()
    assert [j.id for j in snap.queued] == [1]
    c.run_calls.clear()
    clock.t += timedelta(minutes=5)
    snap = await w.scan()
    assert {r for r, _ in c.run_calls} == {"org/new"}
    assert [j.id for j in snap.queued] == [1]  # kept from the previous scan
    c.run_calls.clear()
    clock.t += timedelta(minutes=5)
    await w.scan()
    assert {r for r, _ in c.run_calls} == {"org/new", "org/old"}


@pytest.mark.asyncio
async def test_throttled_skips_old_repos() -> None:
    c = FakeClient([repo("new"), repo("old", pushed=OLD)])
    c.rate_limit = RateLimit(5000, 400, None)  # < 10 %
    snap = await make(c).scan()
    assert snap.throttled is True
    assert snap.ok
    assert {r for r, _ in c.run_calls} == {"org/new"}


@pytest.mark.asyncio
async def test_not_throttled_at_threshold_and_unknown_limit() -> None:
    c = FakeClient([repo("old", pushed=OLD)])
    c.rate_limit = RateLimit(5000, 500, None)
    assert (await make(c).scan()).throttled is False
    c2 = FakeClient([repo("old", pushed=OLD)])
    c2.rate_limit = RateLimit(None, None, None)
    snap = await make(c2).scan()
    assert snap.throttled is False and c2.run_calls


@pytest.mark.asyncio
async def test_scan_error_sets_ok_false_and_keeps_previous_jobs() -> None:
    c = FakeClient([repo("a")])
    c.runs[("org/a", "queued")] = [{"id": 1}]
    c.jobs[("org/a", 1)] = [job(1, 1, "a", "queued", ("self-hosted",))]
    w = make(c)
    assert (await w.scan()).ok
    c.errors["org/a"] = GitHubTransientError("boom")
    snap = await w.scan()
    assert snap.ok is False
    assert snap.error and "boom" in snap.error
    assert [j.id for j in snap.queued] == [1]


@pytest.mark.asyncio
async def test_repo_list_error_sets_ok_false() -> None:
    c = FakeClient([repo("a")])
    c.errors["repos"] = GitHubRateLimitError("limited", 403)
    snap = await make(c).scan()
    assert snap.ok is False
    assert snap.queued == [] and snap.running == []
    assert snap.repos_watched == 0


@pytest.mark.asyncio
async def test_per_repo_404_is_ignored() -> None:
    c = FakeClient([repo("gone"), repo("a")])
    c.errors["org/gone"] = GitHubNotFoundError("nf", 404)
    c.runs[("org/a", "queued")] = [{"id": 1}]
    c.jobs[("org/a", 1)] = [job(1, 1, "a", "queued", ("self-hosted",))]
    snap = await make(c).scan()
    assert snap.ok
    assert [j.id for j in snap.queued] == [1]


@pytest.mark.asyncio
async def test_concurrency_limited_to_eight() -> None:
    c = FakeClient([repo(f"r{i}") for i in range(30)])
    snap = await make(c).scan()
    assert snap.ok
    assert 1 < c.max_active <= 8


@pytest.mark.asyncio
async def test_default_now_is_aware_utc() -> None:
    c = FakeClient([])
    snap = await QueueWatcher(c, "org", ["self-hosted"], []).scan()  # type: ignore[arg-type]
    assert snap.scanned_at.tzinfo is not None
