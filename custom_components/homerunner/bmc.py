"""Vendor neutral Redfish client and Wake-on-LAN helper."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Any

import aiohttp

from .const import PowerState

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
ROOT = "/redfish/v1"
DEFAULT_RESET_TYPES = (
    "On",
    "ForceOff",
    "GracefulShutdown",
    "GracefulRestart",
    "ForceRestart",
    "PushPowerButton",
)


class BmcError(Exception):
    """Generic BMC error."""


class BmcAuthError(BmcError):
    """Credentials rejected (401/403)."""


class BmcConnectionError(BmcError):
    """BMC unreachable, timed out or returned a 5xx."""


class _NotFound(BmcError):
    """Internal: a cached URI returned 404."""


def _link(data: dict[str, Any] | None, key: str) -> str | None:
    """Return the @odata.id of a link property."""
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if isinstance(value, dict):
        uri = value.get("@odata.id")
        if isinstance(uri, str) and uri:
            return uri
    return None


class RedfishClient:
    """Minimal Redfish client using only standard resources."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
    ) -> None:
        self._session = session
        host = host.strip().rstrip("/")
        self._base = host if "://" in host else f"https://{host}"
        self._auth = aiohttp.BasicAuth(username, password)
        self._ssl: bool | None = None if verify_ssl else False
        self._system_uri: str | None = None
        self._chassis_uri: str | None = None
        self._reset_target: str | None = None
        self._reset_types: list[str] | None = None
        self._discovered = False

    # -- HTTP -----------------------------------------------------------

    async def _request(
        self, method: str, uri: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = uri if "://" in uri else f"{self._base}{uri}"
        try:
            async with self._session.request(
                method,
                url,
                json=json,
                auth=self._auth,
                ssl=self._ssl,
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            ) as resp:
                status = resp.status
                if status in (401, 403):
                    raise BmcAuthError(f"BMC rejected credentials (HTTP {status})")
                if status == 404:
                    raise _NotFound(f"{uri} not found")
                if status >= 500:
                    raise BmcConnectionError(f"BMC error HTTP {status} on {uri}")
                if status >= 400:
                    text = (await resp.text())[:200]
                    raise BmcError(f"HTTP {status} on {uri}: {text}")
                if status == 204:
                    return {}
                try:
                    data = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError) as err:
                    if method != "GET":
                        return {}
                    raise BmcError(f"Invalid JSON from {uri}") from err
                return data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            raise BmcConnectionError(f"Cannot reach BMC: {err}") from err

    async def _get_optional(self, uri: str | None) -> dict[str, Any] | None:
        """GET, returning None on 404 or missing uri."""
        if not uri:
            return None
        try:
            return await self._request("GET", uri)
        except _NotFound:
            return None

    async def _first_member(self, root: dict[str, Any], key: str) -> str | None:
        coll_uri = _link(root, key)
        if not coll_uri:
            return None
        coll = await self._get_optional(coll_uri)
        for member in (coll or {}).get("Members") or []:
            if isinstance(member, dict) and member.get("@odata.id"):
                return member["@odata.id"]
        return None

    # -- discovery ------------------------------------------------------

    async def discover(self) -> None:
        """Discover system/chassis URIs and the reset action."""
        try:
            root = await self._request("GET", ROOT)
        except _NotFound as err:
            raise BmcError("No Redfish service found at /redfish/v1") from err
        self._system_uri = await self._first_member(root, "Systems")
        self._chassis_uri = await self._first_member(root, "Chassis")
        self._reset_target = None
        self._reset_types = None
        if self._system_uri:
            system = await self._get_optional(self._system_uri)
            action = ((system or {}).get("Actions") or {}).get("#ComputerSystem.Reset")
            if isinstance(action, dict):
                target = action.get("target")
                if isinstance(target, str) and target:
                    self._reset_target = target
                allowed = action.get("ResetType@Redfish.AllowableValues")
                if isinstance(allowed, list) and allowed:
                    self._reset_types = [str(v) for v in allowed]
        self._discovered = True
        if not self._system_uri:
            raise BmcError("Redfish service exposes no ComputerSystem")

    async def _ensure(self) -> None:
        if not self._discovered:
            await self.discover()

    async def _with_rediscovery(self, func):
        """Run func; on a 404 of a cached URI rediscover once and retry."""
        await self._ensure()
        try:
            return await func()
        except _NotFound:
            _LOGGER.debug("Cached Redfish URI returned 404, rediscovering")
            await self.discover()
            try:
                return await func()
            except _NotFound as err:
                raise BmcError(str(err)) from err

    # -- operations -----------------------------------------------------

    async def get_power_state(self) -> PowerState:
        async def _do() -> PowerState:
            system = await self._request("GET", self._system_uri)
            state = str(system.get("PowerState", ""))
            if state in ("On", "PoweringOn"):
                return PowerState.ON
            if state in ("Off", "PoweringOff"):
                return PowerState.OFF
            return PowerState.UNKNOWN

        return await self._with_rediscovery(_do)

    async def _reset(self, reset_type: str) -> None:
        async def _do() -> None:
            target = self._reset_target or (
                f"{self._system_uri}/Actions/ComputerSystem.Reset"
            )
            await self._request("POST", target, {"ResetType": reset_type})

        await self._with_rediscovery(_do)

    def _allowed(self) -> list[str]:
        return self._reset_types or list(DEFAULT_RESET_TYPES)

    async def power_on(self) -> None:
        await self._ensure()
        allowed = self._allowed()
        if "On" in allowed:
            reset_type = "On"
        elif "PushPowerButton" in allowed:
            reset_type = "PushPowerButton"
        else:
            raise BmcError(f"Power on not supported, allowed: {allowed}")
        await self._reset(reset_type)

    async def power_off(self, force: bool = False) -> None:
        await self._ensure()
        reset_type = "ForceOff" if force else "GracefulShutdown"
        allowed = self._allowed()
        if reset_type not in allowed:
            raise BmcError(f"Reset type {reset_type} not supported, allowed: {allowed}")
        await self._reset(reset_type)

    async def get_power_watts(self) -> float | None:
        async def _do() -> float | None:
            if not self._chassis_uri:
                return None
            chassis = await self._request("GET", self._chassis_uri)

            power = await self._get_optional(_link(chassis, "Power"))
            controls = (power or {}).get("PowerControl")
            if isinstance(controls, list) and controls and isinstance(controls[0], dict):
                watts = _as_float(controls[0].get("PowerConsumedWatts"))
                if watts is not None:
                    return watts

            for key in ("EnvironmentMetrics", "PowerSubsystem"):
                res = await self._get_optional(_link(chassis, key))
                reading = ((res or {}).get("PowerWatts") or {})
                if isinstance(reading, dict):
                    watts = _as_float(reading.get("Reading"))
                    if watts is not None:
                        return watts
            return None

        return await self._with_rediscovery(_do)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# -- Wake-on-LAN ---------------------------------------------------------

_MAC_RE = re.compile(r"^[0-9a-fA-F]{12}$")


def build_magic_packet(mac: str) -> bytes:
    """Return the WoL magic packet (6x 0xFF + 16x MAC) for a MAC address."""
    cleaned = mac.strip()
    if ":" in cleaned or "-" in cleaned:
        parts = re.split(r"[:-]", cleaned)
        if len(parts) != 6 or any(len(p) != 2 for p in parts):
            raise ValueError(f"Invalid MAC address: {mac!r}")
        cleaned = "".join(parts)
    if not _MAC_RE.match(cleaned):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(cleaned) * 16


class _NullProtocol(asyncio.DatagramProtocol):
    pass


async def send_magic_packet(
    mac: str, broadcast: str = "255.255.255.255", port: int = 9
) -> None:
    """Send a Wake-on-LAN magic packet via UDP broadcast."""
    packet = build_magic_packet(mac)
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            _NullProtocol,
            remote_addr=(broadcast, port),
            family=socket.AF_INET,
            allow_broadcast=True,
        )
    except OSError as err:
        raise BmcError(f"Cannot send magic packet: {err}") from err
    try:
        transport.sendto(packet)
    finally:
        transport.close()
