"""asyncssh client for the server-side homerunner guard script."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import asyncssh

PROTOCOL_VERSION = 1
SHUTDOWN_TIMEOUT = 120


class GuardError(Exception):
    """Generic guard error."""


class GuardConnectionError(GuardError):
    """SSH connection, authentication or timeout problem."""


class GuardProtocolError(GuardError):
    """Guard answered with something that violates the protocol."""


@dataclass
class RunnerStatus:
    name: str
    service: str
    active: bool
    job: bool


@dataclass
class GuardStatus:
    hostname: str
    runners: list[RunnerStatus] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    inhibit: bool = False
    uptime_seconds: int | None = None

    @property
    def all_active(self) -> bool:
        return bool(self.runners) and all(r.active for r in self.runners)

    @property
    def any_job(self) -> bool:
        return any(r.job for r in self.runners)


@dataclass
class ShutdownResult:
    accepted: bool
    reason: str | None
    detail: str | None


def _parse_runners(raw: Any) -> list[RunnerStatus]:
    if not isinstance(raw, list):
        raise GuardProtocolError("'runners' must be a list")
    runners = []
    for item in raw:
        if not isinstance(item, dict):
            raise GuardProtocolError("runner entry must be an object")
        try:
            runners.append(
                RunnerStatus(
                    name=str(item["name"]),
                    service=str(item.get("service", "")),
                    active=bool(item["active"]),
                    job=bool(item.get("job", False)),
                )
            )
        except KeyError as err:
            raise GuardProtocolError(f"runner entry misses {err}") from err
    return runners


def _parse_json(stdout: str) -> dict[str, Any]:
    try:
        data = json.loads(stdout.strip())
    except (ValueError, AttributeError) as err:
        raise GuardProtocolError("Guard output is not valid JSON") from err
    if not isinstance(data, dict):
        raise GuardProtocolError("Guard output is not a JSON object")
    if data.get("version") != PROTOCOL_VERSION:
        raise GuardProtocolError(f"Unsupported guard protocol version: {data.get('version')!r}")
    return data


class GuardClient:
    """Runs guard commands over SSH."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        private_key: str,
        known_host_key: str | None,
        timeout: float = 30,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._private_key = private_key
        self._known_host_key = (known_host_key or "").strip() or None
        self._timeout = timeout

    def _known_hosts(self) -> Any:
        if self._known_host_key is None:
            return None
        pattern = (
            self._host if self._port == 22 else f"[{self._host}]:{self._port}"
        )
        return asyncssh.import_known_hosts(f"{pattern} {self._known_host_key}\n")

    async def _connect(self) -> asyncssh.SSHClientConnection:
        try:
            key = asyncssh.import_private_key(self._private_key)
            return await asyncssh.connect(
                self._host,
                port=self._port,
                username=self._username,
                client_keys=[key],
                known_hosts=self._known_hosts(),
                connect_timeout=self._timeout,
                login_timeout=self._timeout,
            )
        except (asyncssh.Error, OSError, asyncio.TimeoutError, ValueError) as err:
            raise GuardConnectionError(f"SSH connection failed: {err}") from err

    async def _run(self, command: str, timeout: float) -> dict[str, Any]:
        conn = await self._connect()
        try:
            try:
                result = await conn.run(command, check=False, timeout=timeout)
            except (asyncssh.Error, OSError, asyncio.TimeoutError) as err:
                raise GuardConnectionError(
                    f"Guard command '{command}' failed: {err}"
                ) from err
        finally:
            conn.close()
        stdout = result.stdout
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        return _parse_json(stdout or "")

    async def status(self) -> GuardStatus:
        data = await self._run("status", self._timeout)
        try:
            uptime = data.get("uptime_seconds")
            users = data.get("users") or []
            if not isinstance(users, list):
                raise GuardProtocolError("'users' must be a list")
            return GuardStatus(
                hostname=str(data["hostname"]),
                runners=_parse_runners(data["runners"]),
                users=[str(u) for u in users],
                inhibit=bool(data.get("inhibit", False)),
                uptime_seconds=int(uptime) if uptime is not None else None,
            )
        except KeyError as err:
            raise GuardProtocolError(f"Guard status misses {err}") from err
        except (TypeError, ValueError) as err:
            raise GuardProtocolError(f"Invalid guard status: {err}") from err

    async def shutdown(self) -> ShutdownResult:
        data = await self._run("shutdown", max(self._timeout, SHUTDOWN_TIMEOUT))
        result = data.get("result")
        if result not in ("accepted", "refused"):
            raise GuardProtocolError(f"Unexpected shutdown result: {result!r}")
        reason = data.get("reason")
        detail = data.get("detail")
        return ShutdownResult(
            accepted=result == "accepted",
            reason=None if reason is None else str(reason),
            detail=None if detail is None else str(detail),
        )

    async def cancel(self) -> None:
        await self._run("cancel", max(self._timeout, SHUTDOWN_TIMEOUT))

    async def fetch_host_key(self) -> str:
        try:
            key = await asyncio.wait_for(
                asyncssh.get_server_host_key(self._host, port=self._port),
                self._timeout,
            )
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as err:
            raise GuardConnectionError(f"Cannot fetch host key: {err}") from err
        if key is None:
            raise GuardConnectionError("Server presented no host key")
        return key.export_public_key().decode().strip()
