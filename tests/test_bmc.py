"""Tests for the Redfish client and Wake-on-LAN helper."""

from __future__ import annotations

import asyncio
import socket

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.homerunner.bmc import (
    BmcAuthError,
    BmcConnectionError,
    BmcError,
    RedfishClient,
    build_magic_packet,
    send_magic_packet,
)
from custom_components.homerunner.const import PowerState

pytestmark = pytest.mark.usefixtures("socket_enabled")

SYS = "/redfish/v1/Systems/1"
CH = "/redfish/v1/Chassis/1"


class FakeRedfish:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.power_state = "On"
        self.allowable: list[str] | None = ["On", "ForceOff", "GracefulShutdown"]
        self.chassis: dict = {"Power": {"@odata.id": f"{CH}/Power"}}
        self.resources: dict[str, dict] = {
            f"{CH}/Power": {"PowerControl": [{"PowerConsumedWatts": 123.5}]},
        }
        self.status_override: int | None = None
        self.sys_prefix = SYS
        self.require_auth = ("user", "pw")
        self.auth_seen: list[str | None] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def handle(self, request: web.Request) -> web.Response:
        path = request.path
        self.auth_seen.append(request.headers.get("Authorization"))
        if self.status_override:
            return web.Response(status=self.status_override)
        if request.method == "POST":
            self.posts.append((path, await request.json()))
            return web.Response(status=204)
        self.gets.append(path)
        sys_uri = self.sys_prefix
        action = {"target": f"{sys_uri}/Actions/ComputerSystem.Reset"}
        if self.allowable is not None:
            action["ResetType@Redfish.AllowableValues"] = self.allowable
        data = {
            "/redfish/v1": {
                "Systems": {"@odata.id": "/redfish/v1/Systems"},
                "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
            },
            "/redfish/v1/Systems": {"Members": [{"@odata.id": sys_uri}, {"@odata.id": "/x"}]},
            "/redfish/v1/Chassis": {"Members": [{"@odata.id": CH}]},
            sys_uri: {"PowerState": self.power_state, "Actions": {"#ComputerSystem.Reset": action}},
            CH: self.chassis,
            **self.resources,
        }.get(path)
        if data is None:
            return web.Response(status=404)
        return web.json_response(data)


@pytest.fixture
async def fake():
    f = FakeRedfish()
    server = TestServer(f.app())
    await server.start_server()
    f.url = str(server.make_url("")).rstrip("/")
    async with aiohttp.ClientSession() as session:
        f.session = session
        yield f
    await server.close()


def client(fake: FakeRedfish, password: str = "pw", user: str = "user") -> RedfishClient:
    return RedfishClient(fake.session, fake.url, user, password, verify_ssl=False)


@pytest.mark.asyncio
async def test_discovery_and_basic_auth(fake):
    c = client(fake)
    await c.discover()
    assert c._system_uri == SYS
    assert c._chassis_uri == CH
    assert c._reset_target == f"{SYS}/Actions/ComputerSystem.Reset"
    assert c._reset_types == ["On", "ForceOff", "GracefulShutdown"]
    assert fake.auth_seen[0].startswith("Basic ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("On", PowerState.ON),
        ("PoweringOn", PowerState.ON),
        ("Off", PowerState.OFF),
        ("PoweringOff", PowerState.OFF),
        ("Paused", PowerState.UNKNOWN),
    ],
)
async def test_power_state(fake, raw, expected):
    fake.power_state = raw
    assert await client(fake).get_power_state() == expected


@pytest.mark.asyncio
async def test_power_on_and_off(fake):
    c = client(fake)
    await c.power_on()
    await c.power_off()
    await c.power_off(force=True)
    target = f"{SYS}/Actions/ComputerSystem.Reset"
    assert fake.posts == [
        (target, {"ResetType": "On"}),
        (target, {"ResetType": "GracefulShutdown"}),
        (target, {"ResetType": "ForceOff"}),
    ]


@pytest.mark.asyncio
async def test_push_power_button_fallback(fake):
    fake.allowable = ["PushPowerButton", "ForceOff"]
    await client(fake).power_on()
    assert fake.posts[0][1] == {"ResetType": "PushPowerButton"}


@pytest.mark.asyncio
async def test_missing_allowable_values_assumes_standard(fake):
    fake.allowable = None
    c = client(fake)
    await c.power_on()
    await c.power_off(force=True)
    assert [p[1]["ResetType"] for p in fake.posts] == ["On", "ForceOff"]


@pytest.mark.asyncio
async def test_unsupported_reset_type(fake):
    fake.allowable = ["ForceOff"]
    c = client(fake)
    with pytest.raises(BmcError):
        await c.power_on()
    with pytest.raises(BmcError):
        await c.power_off()
    assert fake.posts == []


@pytest.mark.asyncio
async def test_watts_via_power(fake):
    assert await client(fake).get_power_watts() == 123.5


@pytest.mark.asyncio
async def test_watts_via_environment_metrics(fake):
    fake.chassis = {"EnvironmentMetrics": {"@odata.id": f"{CH}/Env"}}
    fake.resources = {f"{CH}/Env": {"PowerWatts": {"Reading": 77}}}
    assert await client(fake).get_power_watts() == 77.0


@pytest.mark.asyncio
async def test_watts_via_power_subsystem(fake):
    fake.chassis = {"PowerSubsystem": {"@odata.id": f"{CH}/PS"}}
    fake.resources = {f"{CH}/PS": {"PowerWatts": {"Reading": 55.5}}}
    assert await client(fake).get_power_watts() == 55.5


@pytest.mark.asyncio
async def test_watts_power_without_reading_falls_back(fake):
    fake.chassis = {
        "Power": {"@odata.id": f"{CH}/Power"},
        "EnvironmentMetrics": {"@odata.id": f"{CH}/Env"},
    }
    fake.resources = {
        f"{CH}/Power": {"PowerControl": [{"PowerConsumedWatts": None}]},
        f"{CH}/Env": {"PowerWatts": {"Reading": 12}},
    }
    assert await client(fake).get_power_watts() == 12.0


@pytest.mark.asyncio
async def test_watts_none(fake):
    fake.chassis = {}
    assert await client(fake).get_power_watts() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error(fake, status):
    fake.status_override = status
    with pytest.raises(BmcAuthError):
        await client(fake, password="secret").get_power_state()


@pytest.mark.asyncio
async def test_server_error_is_connection_error(fake):
    fake.status_override = 503
    with pytest.raises(BmcConnectionError):
        await client(fake).get_power_state()


@pytest.mark.asyncio
async def test_other_http_error(fake):
    fake.status_override = 400
    with pytest.raises(BmcError) as exc:
        await client(fake).get_power_state()
    assert not isinstance(exc.value, (BmcAuthError, BmcConnectionError))


@pytest.mark.asyncio
async def test_connection_refused():
    async with aiohttp.ClientSession() as session:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        c = RedfishClient(session, f"http://127.0.0.1:{port}", "u", "p")
        with pytest.raises(BmcConnectionError):
            await c.get_power_state()


@pytest.mark.asyncio
async def test_timeout_is_connection_error(fake, monkeypatch):
    import custom_components.homerunner.bmc as bmc

    monkeypatch.setattr(bmc, "REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.05))

    async def slow(request):
        await asyncio.sleep(1)

    fake.handle = slow  # type: ignore[assignment]
    server = TestServer(fake.app())
    await server.start_server()
    try:
        c = RedfishClient(fake.session, str(server.make_url("")).rstrip("/"), "u", "p")
        with pytest.raises(BmcConnectionError):
            await c.get_power_state()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_rediscovery_on_404(fake):
    c = client(fake)
    assert await c.get_power_state() == PowerState.ON
    # system moves to another URI
    fake.sys_prefix = "/redfish/v1/Systems/2"
    assert await c.get_power_state() == PowerState.ON
    assert c._system_uri == "/redfish/v1/Systems/2"


@pytest.mark.asyncio
async def test_rediscovery_only_once(fake):
    c = client(fake)
    await c.discover()
    fake.sys_prefix = "/redfish/v1/Systems/gone"
    # discovery now points at a URI that 404s when fetched
    orig = fake.handle

    async def handle(request):
        if request.path == "/redfish/v1/Systems/gone":
            return web.Response(status=404)
        return await orig(request)

    fake.handle = handle  # type: ignore[assignment]
    server = TestServer(fake.app())
    await server.start_server()
    try:
        c2 = RedfishClient(fake.session, str(server.make_url("")).rstrip("/"), "user", "pw")
        with pytest.raises(BmcError):
            await c2.get_power_state()
    finally:
        await server.close()


# -- magic packet ---------------------------------------------------------


@pytest.mark.parametrize("mac", ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabbccddeeff"])
def test_build_magic_packet(mac):
    packet = build_magic_packet(mac)
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddeeff") * 16


@pytest.mark.parametrize(
    "mac", ["", "aa:bb:cc:dd:ee", "gg:bb:cc:dd:ee:ff", "aabbccddeef", "aa:bb:cc:dd:ee:ff:00"]
)
def test_build_magic_packet_invalid(mac):
    with pytest.raises(ValueError):
        build_magic_packet(mac)


@pytest.mark.asyncio
async def test_send_magic_packet_udp():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.setblocking(False)
    port = rx.getsockname()[1]
    try:
        await send_magic_packet("aa:bb:cc:dd:ee:ff", "127.0.0.1", port)
        loop = asyncio.get_running_loop()
        data = await asyncio.wait_for(loop.sock_recv(rx, 1024), 2)
    finally:
        rx.close()
    assert data == build_magic_packet("aabbccddeeff")


@pytest.mark.asyncio
async def test_send_magic_packet_invalid_mac():
    with pytest.raises(ValueError):
        await send_magic_packet("nope")
