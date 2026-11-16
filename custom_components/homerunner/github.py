"""Async GitHub REST client for Homerunner (aiohttp, ETag cache, typed errors)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
MAX_PAGES = 50
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GitHubError(Exception):
    """Base class for all GitHub client errors."""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubAuthError(GitHubError):
    """HTTP 401."""


class GitHubForbiddenError(GitHubError):
    """HTTP 403 without rate-limit indication."""


class GitHubRateLimitError(GitHubError):
    """Rate limit exhausted (403/429)."""

    def __init__(
        self,
        message: str = "",
        status: int | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        super().__init__(message, status)
        self.reset_at = reset_at


class GitHubNotFoundError(GitHubError):
    """HTTP 404."""


class GitHubValidationError(GitHubError):
    """HTTP 422."""


class GitHubTransientError(GitHubError):
    """Network errors, timeouts and 5xx: the only retryable class."""


def parse_github_datetime(value: Any) -> datetime | None:
    """Parse a GitHub ISO timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class Repo:
    """A repository of the watched owner."""

    full_name: str
    name: str
    archived: bool
    pushed_at: datetime | None


@dataclass(frozen=True)
class Job:
    """A workflow job."""

    id: int
    run_id: int
    repo: str
    name: str
    status: str
    labels: tuple[str, ...]
    html_url: str | None
    created_at: datetime | None
    started_at: datetime | None


@dataclass(frozen=True)
class RateLimit:
    """Rate limit state from the x-ratelimit-* headers."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None


def _int_header(headers: Any, name: str) -> int | None:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


class GitHubClient:
    """Minimal async GitHub REST client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        api_base: str = DEFAULT_API_BASE,
    ) -> None:
        self._session = session
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._etags: dict[str, tuple[str, Any]] = {}
        self.rate_limit = RateLimit(None, None, None)

    # ------------------------------------------------------------------ helpers

    def _headers(self, token: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token or self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self._api_base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    def _update_rate_limit(self, headers: Any) -> None:
        limit = _int_header(headers, "x-ratelimit-limit")
        remaining = _int_header(headers, "x-ratelimit-remaining")
        reset = _int_header(headers, "x-ratelimit-reset")
        if limit is None and remaining is None and reset is None:
            return
        prev = self.rate_limit
        self.rate_limit = RateLimit(
            limit if limit is not None else prev.limit,
            remaining if remaining is not None else prev.remaining,
            datetime.fromtimestamp(reset, UTC) if reset is not None else prev.reset_at,
        )

    @staticmethod
    def _error_message(text: str) -> str:
        try:
            data = json.loads(text)
        except ValueError:
            return text[:200]
        if isinstance(data, dict):
            message = str(data.get("message", ""))
            errors = data.get("errors")
            if errors:
                message = f"{message} {errors}"
            return message[:500]
        return text[:200]

    def _raise_for_status(self, status: int, headers: Any, text: str) -> None:
        message = self._error_message(text)
        if status == 401:
            raise GitHubAuthError(message or "unauthorized", status)
        if status in (403, 429):
            remaining = _int_header(headers, "x-ratelimit-remaining")
            retry_after = _int_header(headers, "retry-after")
            limited = (
                status == 429
                or remaining == 0
                or retry_after is not None
                or "rate limit" in message.lower()
            )
            if limited:
                reset_at = self.rate_limit.reset_at
                if retry_after is not None:
                    reset_at = datetime.fromtimestamp(
                        datetime.now(UTC).timestamp() + retry_after, UTC
                    )
                raise GitHubRateLimitError(message or "rate limited", status, reset_at)
            raise GitHubForbiddenError(message or "forbidden", status)
        if status == 404:
            raise GitHubNotFoundError(message or "not found", status)
        if status == 422:
            raise GitHubValidationError(message or "validation failed", status)
        if status >= 500:
            raise GitHubTransientError(message or f"server error {status}", status)
        raise GitHubError(message or f"unexpected status {status}", status)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: Any = None,
    ) -> tuple[int, Any, str]:
        """Perform a request; returns (status, headers, text). Raises transient errors."""
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                text = await resp.text()
                status = resp.status
                resp_headers = resp.headers
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GitHubTransientError(f"{method} request failed: {type(err).__name__}") from err
        self._update_rate_limit(resp_headers)
        return status, resp_headers, text

    async def _get(self, url: str) -> tuple[Any, Any]:
        """GET with ETag cache. Returns (parsed body, response headers)."""
        headers = self._headers()
        cached = self._etags.get(url)
        if cached:
            headers["If-None-Match"] = cached[0]
        status, resp_headers, text = await self._send("GET", url, headers=headers)
        if status == 304:
            if cached is None:
                raise GitHubTransientError("304 without cached body", 304)
            return cached[1], resp_headers
        if status != 200:
            self._raise_for_status(status, resp_headers, text)
        try:
            body = json.loads(text) if text else None
        except ValueError as err:
            raise GitHubTransientError("invalid JSON in response", status) from err
        etag = resp_headers.get("ETag") or resp_headers.get("etag")
        if etag:
            self._etags[url] = (etag, body)
        return body, resp_headers

    async def _get_paginated(self, path: str, params: dict[str, Any], key: str | None) -> list[Any]:
        """GET all pages (Link rel=next). `key` selects the list inside object bodies."""
        url: str | None = self._url(path, params)
        items: list[Any] = []
        pages = 0
        while url and pages < MAX_PAGES:
            body, headers = await self._get(url)
            if key is not None:
                body = (body or {}).get(key, []) if isinstance(body, dict) else []
            items.extend(body or [])
            pages += 1
            match = _NEXT_LINK.search(headers.get("Link") or headers.get("link") or "")
            url = match.group(1) if match else None
        return items

    # --------------------------------------------------------------------- API

    async def list_repos(self, owner: str) -> list[Repo]:
        """List the repositories of an organisation, falling back to a user."""
        params = {"per_page": 100}
        try:
            raw = await self._get_paginated(f"/orgs/{quote(owner)}/repos", params, None)
        except GitHubNotFoundError:
            raw = await self._get_paginated(f"/users/{quote(owner)}/repos", params, None)
        return [
            Repo(
                full_name=item["full_name"],
                name=item["name"],
                archived=bool(item.get("archived", False)),
                pushed_at=parse_github_datetime(item.get("pushed_at")),
            )
            for item in raw
        ]

    async def list_runs(self, repo: str, status: str) -> list[dict]:
        """List workflow runs of a repository with the given status."""
        return await self._get_paginated(
            f"/repos/{repo}/actions/runs",
            {"status": status, "per_page": 100},
            "workflow_runs",
        )

    async def list_run_jobs(self, repo: str, run_id: int) -> list[Job]:
        """List the jobs of the latest attempt of a run."""
        raw = await self._get_paginated(
            f"/repos/{repo}/actions/runs/{run_id}/jobs",
            {"filter": "latest", "per_page": 100},
            "jobs",
        )
        return [
            Job(
                id=int(item["id"]),
                run_id=int(item.get("run_id", run_id)),
                repo=repo,
                name=str(item.get("name", "")),
                status=str(item.get("status", "")),
                labels=tuple(item.get("labels") or ()),
                html_url=item.get("html_url"),
                created_at=parse_github_datetime(item.get("created_at")),
                started_at=parse_github_datetime(item.get("started_at")),
            )
            for item in raw
        ]

    async def get_run(self, repo: str, run_id: int) -> dict:
        """Fetch one workflow run."""
        body, _ = await self._get(self._url(f"/repos/{repo}/actions/runs/{run_id}"))
        return body if isinstance(body, dict) else {}

    async def dispatch(
        self,
        repo: str,
        workflow: str,
        ref: str,
        inputs: dict | None,
        token: str | None = None,
    ) -> int | None:
        """Dispatch a workflow. Returns the run id when GitHub reports it."""
        url = self._url(
            f"/repos/{repo}/actions/workflows/{quote(str(workflow), safe='')}/dispatches"
        )
        headers = self._headers(token)
        body: dict[str, Any] = {
            "ref": ref,
            "inputs": inputs or {},
            "return_run_details": True,
        }
        for attempt in (1, 2):
            status, resp_headers, text = await self._send(
                "POST", url, headers=headers, json_body=body
            )
            if status == 204:
                return None
            if status == 200:
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    return None
                run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
                return int(run_id) if run_id is not None else None
            if status == 422 and attempt == 1 and "return_run_details" in self._error_message(text):
                _LOGGER.debug("return_run_details not supported, retrying without it")
                body = {k: v for k, v in body.items() if k != "return_run_details"}
                continue
            self._raise_for_status(status, resp_headers, text)
        return None  # pragma: no cover

    async def validate(self, owner: str) -> None:
        """Check token and owner: list repos and read runs of the first repo."""
        repos = await self.list_repos(owner)
        if repos:
            await self.list_runs(repos[0].full_name, "queued")
