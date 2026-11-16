"""Tests for the GitHub client."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

import aiohttp
import pytest

from custom_components.homerunner.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubForbiddenError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
    GitHubValidationError,
    parse_github_datetime,
)

API = "https://api.github.com"


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, headers: dict | None = None):
        self.status = status
        self._text = body if isinstance(body, str) else ("" if body is None else json.dumps(body))
        self.headers = headers or {}

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeSession:
    """Scripted session: responses are consumed in order per (method, url)."""

    def __init__(self) -> None:
        self.script: dict[tuple[str, str], list[Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, url: str, *responses: Any) -> None:
        self.script.setdefault((method, url), []).extend(responses)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        queue = self.script.get((method, url))
        assert queue, f"unexpected request {method} {url}"
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(session: FakeSession) -> GitHubClient:
    return GitHubClient(session, "secret-token")  # type: ignore[arg-type]


def test_parse_datetime() -> None:
    assert parse_github_datetime("2026-11-16T03:00:00Z") == datetime(2026, 11, 16, 3, tzinfo=UTC)
    assert parse_github_datetime(None) is None
    assert parse_github_datetime("garbage") is None


@pytest.mark.asyncio
async def test_headers_and_etag_304(client: GitHubClient, session: FakeSession) -> None:
    url = f"{API}/repos/o/r/actions/runs/5"
    session.add(
        "GET",
        url,
        FakeResponse(200, {"id": 5}, {"ETag": '"abc"', "x-ratelimit-limit": "5000",
                                      "x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1800000000"}),
        FakeResponse(304, None, {"x-ratelimit-remaining": "4999"}),
    )
    assert await client.get_run("o/r", 5) == {"id": 5}
    assert await client.get_run("o/r", 5) == {"id": 5}
    first, second = session.calls
    assert first["headers"]["Authorization"] == "Bearer secret-token"
    assert first["headers"]["Accept"] == "application/vnd.github+json"
    assert first["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert "If-None-Match" not in first["headers"]
    assert second["headers"]["If-None-Match"] == '"abc"'
    assert first["timeout"].total == 30
    assert client.rate_limit.limit == 5000
    assert client.rate_limit.remaining == 4999
    assert client.rate_limit.reset_at == datetime.fromtimestamp(1800000000, UTC)


@pytest.mark.asyncio
async def test_etag_cache_keyed_by_query(client: GitHubClient, session: FakeSession) -> None:
    q = f"{API}/repos/o/r/actions/runs?status=queued&per_page=100"
    p = f"{API}/repos/o/r/actions/runs?status=in_progress&per_page=100"
    session.add("GET", q, FakeResponse(200, {"workflow_runs": [{"id": 1}]}, {"ETag": '"q"'}),
                FakeResponse(304))
    session.add("GET", p, FakeResponse(200, {"workflow_runs": [{"id": 2}]}, {"ETag": '"p"'}))
    assert await client.list_runs("o/r", "queued") == [{"id": 1}]
    assert await client.list_runs("o/r", "in_progress") == [{"id": 2}]
    assert await client.list_runs("o/r", "queued") == [{"id": 1}]
    assert session.calls[1]["headers"].get("If-None-Match") is None
    assert session.calls[2]["headers"]["If-None-Match"] == '"q"'


@pytest.mark.asyncio
async def test_pagination_via_link_header(client: GitHubClient, session: FakeSession) -> None:
    p1 = f"{API}/orgs/o/repos?per_page=100"
    p2 = f"{API}/orgs/o/repos?per_page=100&page=2"
    repo = lambda n, archived=False: {  # noqa: E731
        "full_name": f"o/{n}", "name": n, "archived": archived, "pushed_at": "2026-11-16T03:00:00Z"}
    session.add("GET", p1, FakeResponse(200, [repo("a")], {"Link": f'<{p2}>; rel="next", <{p2}>; rel="last"'}))
    session.add("GET", p2, FakeResponse(200, [repo("b", True)], {"Link": f'<{p1}>; rel="prev"'}))
    repos = await client.list_repos("o")
    assert [r.name for r in repos] == ["a", "b"]
    assert repos[1].archived is True
    assert repos[0].pushed_at == datetime(2026, 11, 16, 3, tzinfo=UTC)
    assert repos[0].full_name == "o/a"


@pytest.mark.asyncio
async def test_list_repos_falls_back_to_users(client: GitHubClient, session: FakeSession) -> None:
    session.add("GET", f"{API}/orgs/me/repos?per_page=100", FakeResponse(404, {"message": "Not Found"}))
    session.add("GET", f"{API}/users/me/repos?per_page=100", FakeResponse(200, [
        {"full_name": "me/x", "name": "x", "archived": False, "pushed_at": None}]))
    repos = await client.list_repos("me")
    assert [r.full_name for r in repos] == ["me/x"]
    assert repos[0].pushed_at is None


@pytest.mark.asyncio
async def test_list_run_jobs(client: GitHubClient, session: FakeSession) -> None:
    session.add("GET", f"{API}/repos/o/r/actions/runs/7/jobs?filter=latest&per_page=100", FakeResponse(200, {
        "total_count": 1,
        "jobs": [{"id": 11, "run_id": 7, "name": "build", "status": "queued",
                  "labels": ["self-hosted", "linux"], "html_url": "https://x/11",
                  "created_at": "2026-11-16T03:00:00Z", "started_at": None}]}))
    jobs = await client.list_run_jobs("o/r", 7)
    assert len(jobs) == 1
    job = jobs[0]
    assert (job.id, job.run_id, job.repo, job.status) == (11, 7, "o/r", "queued")
    assert job.labels == ("self-hosted", "linux")
    assert job.created_at == datetime(2026, 11, 16, 3, tzinfo=UTC)
    assert job.started_at is None


@pytest.mark.parametrize(
    ("status", "headers", "exc"),
    [
        (401, {}, GitHubAuthError),
        (403, {}, GitHubForbiddenError),
        (403, {"x-ratelimit-remaining": "3"}, GitHubForbiddenError),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}, GitHubRateLimitError),
        (429, {}, GitHubRateLimitError),
        (404, {}, GitHubNotFoundError),
        (422, {}, GitHubValidationError),
        (500, {}, GitHubTransientError),
        (503, {}, GitHubTransientError),
    ],
)
@pytest.mark.asyncio
async def test_error_mapping(client: GitHubClient, session: FakeSession, status: int,
                             headers: dict, exc: type[GitHubError]) -> None:
    session.add("GET", f"{API}/repos/o/r/actions/runs/1", FakeResponse(status, {"message": "nope"}, headers))
    with pytest.raises(exc) as info:
        await client.get_run("o/r", 1)
    assert type(info.value) is exc
    assert info.value.status == status


@pytest.mark.asyncio
async def test_rate_limit_reset_at(client: GitHubClient, session: FakeSession) -> None:
    session.add("GET", f"{API}/repos/o/r/actions/runs/1", FakeResponse(
        403, {"message": "API rate limit exceeded"},
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}))
    with pytest.raises(GitHubRateLimitError) as info:
        await client.get_run("o/r", 1)
    assert info.value.reset_at == datetime.fromtimestamp(1800000000, UTC)
    assert client.rate_limit.remaining == 0


@pytest.mark.parametrize("err", [aiohttp.ClientConnectionError("boom"), TimeoutError()])
@pytest.mark.asyncio
async def test_network_errors_are_transient(client: GitHubClient, session: FakeSession,
                                            err: BaseException) -> None:
    session.add("GET", f"{API}/repos/o/r/actions/runs/1", err)
    with pytest.raises(GitHubTransientError):
        await client.get_run("o/r", 1)


@pytest.mark.asyncio
async def test_errors_never_leak_token(client: GitHubClient, session: FakeSession) -> None:
    session.add("GET", f"{API}/repos/o/r/actions/runs/1", FakeResponse(401, {"message": "Bad credentials"}))
    with pytest.raises(GitHubAuthError) as info:
        await client.get_run("o/r", 1)
    assert "secret-token" not in str(info.value)


DISPATCH = f"{API}/repos/o/r/actions/workflows/ci.yml/dispatches"


@pytest.mark.asyncio
async def test_dispatch_200_returns_run_id(client: GitHubClient, session: FakeSession) -> None:
    session.add("POST", DISPATCH, FakeResponse(200, {"workflow_run_id": 42, "run_url": "x"}))
    assert await client.dispatch("o/r", "ci.yml", "main", {"a": "b"}) == 42
    assert session.calls[0]["json"] == {"ref": "main", "inputs": {"a": "b"}, "return_run_details": True}


@pytest.mark.asyncio
async def test_dispatch_204_returns_none(client: GitHubClient, session: FakeSession) -> None:
    session.add("POST", DISPATCH, FakeResponse(204))
    assert await client.dispatch("o/r", "ci.yml", "main", None) is None
    assert session.calls[0]["json"]["inputs"] == {}


@pytest.mark.asyncio
async def test_dispatch_422_retries_without_run_details(client: GitHubClient, session: FakeSession) -> None:
    session.add("POST", DISPATCH,
                FakeResponse(422, {"message": "Unexpected inputs provided: [\"return_run_details\"]"}),
                FakeResponse(204))
    assert await client.dispatch("o/r", "ci.yml", "main", {}) is None
    assert len(session.calls) == 2
    assert "return_run_details" in session.calls[0]["json"]
    assert "return_run_details" not in session.calls[1]["json"]


@pytest.mark.asyncio
async def test_dispatch_other_422_raises_without_retry(client: GitHubClient, session: FakeSession) -> None:
    session.add("POST", DISPATCH, FakeResponse(422, {"message": "Workflow does not have workflow_dispatch"}))
    with pytest.raises(GitHubValidationError):
        await client.dispatch("o/r", "ci.yml", "main", {})
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_token_override_only_for_call(client: GitHubClient, session: FakeSession) -> None:
    session.add("POST", DISPATCH, FakeResponse(204))
    session.add("GET", f"{API}/repos/o/r/actions/runs/1", FakeResponse(200, {"id": 1}))
    await client.dispatch("o/r", "ci.yml", "main", {}, token="other-token")
    await client.get_run("o/r", 1)
    assert session.calls[0]["headers"]["Authorization"] == "Bearer other-token"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_dispatch_errors_mapped(client: GitHubClient, session: FakeSession) -> None:
    session.add("POST", DISPATCH, FakeResponse(404, {"message": "Not Found"}))
    with pytest.raises(GitHubNotFoundError):
        await client.dispatch("o/r", "ci.yml", "main", {})


@pytest.mark.asyncio
async def test_validate(client: GitHubClient, session: FakeSession) -> None:
    session.add("GET", f"{API}/orgs/o/repos?per_page=100", FakeResponse(200, [
        {"full_name": "o/a", "name": "a", "archived": False, "pushed_at": None}]))
    session.add("GET", f"{API}/repos/o/a/actions/runs?status=queued&per_page=100",
                FakeResponse(200, {"workflow_runs": []}))
    await client.validate("o")
    session.script.clear()
    session.add("GET", f"{API}/orgs/o/repos?per_page=100", FakeResponse(401, {"message": "Bad credentials"}))
    with pytest.raises(GitHubAuthError):
        await client.validate("o")


@pytest.mark.asyncio
async def test_custom_api_base(session: FakeSession) -> None:
    c = GitHubClient(session, "t", "https://ghe.example/api/v3/")  # type: ignore[arg-type]
    session.add("GET", "https://ghe.example/api/v3/repos/o/r/actions/runs/1", FakeResponse(200, {"id": 1}))
    assert await c.get_run("o/r", 1) == {"id": 1}
