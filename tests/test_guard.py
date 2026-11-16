"""Tests for the guard SSH client."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import asyncssh
import pytest

from custom_components.homerunner import guard as guard_mod
from custom_components.homerunner.guard import (
    GuardClient,
    GuardConnectionError,
    GuardProtocolError,
)

STATUS = {
    "version": 1,
    "hostname": "host",
    "runners": [
        {"name": "c1", "service": "actions.runner.org.c1", "active": True, "job": False},
        {"name": "c2", "service": "actions.runner.org.c2", "active": True, "job": True},
    ],
    "users": ["alice"],
    "inhibit": False,
    "uptime_seconds": 1234,
}


class FakeConn:
    def __init__(self, stdout="", run_exc=None):
        self.stdout = stdout
        self.run_exc = run_exc
        self.commands: list[tuple[str, float | None]] = []
        self.closed = False

    async def run(self, cmd, check=False, timeout=None):
        self.commands.append((cmd, timeout))
        if self.run_exc:
            raise self.run_exc
        return SimpleNamespace(stdout=self.stdout, exit_status=0)

    def close(self):
        self.closed = True


@pytest.fixture
def key():
    return asyncssh.generate_private_key("ssh-ed25519")


@pytest.fixture
def connect(monkeypatch):
    state = SimpleNamespace(conn=FakeConn(json.dumps(STATUS)), kwargs=None, host=None, exc=None)

    async def fake_connect(host, **kwargs):
        state.host, state.kwargs = host, kwargs
        if state.exc:
            raise state.exc
        return state.conn

    monkeypatch.setattr(guard_mod.asyncssh, "connect", fake_connect)
    return state


def make(key, port=22, known=None):
    pem = key.export_private_key().decode()
    return GuardClient("h.example", port, "bot", pem, known, timeout=5)


@pytest.mark.asyncio
async def test_status(connect, key):
    st = await make(key).status()
    assert st.hostname == "host"
    assert st.users == ["alice"]
    assert st.uptime_seconds == 1234
    assert st.all_active and st.any_job
    assert st.runners[0].service == "actions.runner.org.c1"
    assert connect.conn.commands == [("status", 5)]
    assert connect.conn.closed
    assert connect.kwargs["username"] == "bot"
    assert connect.kwargs["port"] == 22
    assert len(connect.kwargs["client_keys"]) == 1


@pytest.mark.asyncio
async def test_status_no_runners_not_all_active(connect, key):
    connect.conn.stdout = json.dumps({**STATUS, "runners": [], "uptime_seconds": None})
    st = await make(key).status()
    assert not st.all_active and not st.any_job
    assert st.uptime_seconds is None


@pytest.mark.asyncio
async def test_status_inactive_runner(connect, key):
    r = [{"name": "c", "service": "s", "active": False, "job": False}]
    connect.conn.stdout = json.dumps({**STATUS, "runners": r})
    assert not (await make(key).status()).all_active


@pytest.mark.asyncio
async def test_shutdown_accepted(connect, key):
    connect.conn.stdout = json.dumps({"version": 1, "result": "accepted", "reason": None, "detail": "ok", "runners": []})
    res = await make(key).shutdown()
    assert res.accepted and res.reason is None and res.detail == "ok"
    assert connect.conn.commands == [("shutdown", 120)]


@pytest.mark.asyncio
async def test_shutdown_refused(connect, key):
    connect.conn.stdout = json.dumps({"version": 1, "result": "refused", "reason": "job_running", "detail": "busy"})
    res = await make(key).shutdown()
    assert not res.accepted and res.reason == "job_running" and res.detail == "busy"


@pytest.mark.asyncio
async def test_shutdown_bad_result(connect, key):
    connect.conn.stdout = json.dumps({"version": 1, "result": "maybe"})
    with pytest.raises(GuardProtocolError):
        await make(key).shutdown()


@pytest.mark.asyncio
async def test_cancel(connect, key):
    connect.conn.stdout = json.dumps({"version": 1, "result": "accepted"})
    await make(key).cancel()
    assert connect.conn.commands[0][0] == "cancel"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "",
        "[]",
        json.dumps({**STATUS, "version": 2}),
        json.dumps({k: v for k, v in STATUS.items() if k != "version"}),
        json.dumps({k: v for k, v in STATUS.items() if k != "hostname"}),
        json.dumps({k: v for k, v in STATUS.items() if k != "runners"}),
        json.dumps({**STATUS, "runners": [{"name": "x"}]}),
        json.dumps({**STATUS, "runners": "x"}),
    ],
)
async def test_status_protocol_errors(connect, key, stdout):
    connect.conn.stdout = stdout
    with pytest.raises(GuardProtocolError):
        await make(key).status()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        asyncssh.PermissionDenied("denied"),
        asyncssh.HostKeyNotVerifiable("bad key"),
        ConnectionRefusedError("refused"),
        asyncio.TimeoutError(),
        OSError("unreachable"),
    ],
)
async def test_connect_errors(connect, key, exc):
    connect.exc = exc
    with pytest.raises(GuardConnectionError):
        await make(key).status()


@pytest.mark.asyncio
async def test_invalid_private_key(connect):
    with pytest.raises(GuardConnectionError):
        await GuardClient("h", 22, "u", "garbage", None).status()


@pytest.mark.asyncio
async def test_run_errors_map_to_connection_error(connect, key):
    connect.conn.run_exc = asyncssh.TimeoutError(None, "status", None, None, None, None, "", "")
    with pytest.raises(GuardConnectionError):
        await make(key).status()
    assert connect.conn.closed


@pytest.mark.asyncio
async def test_run_disconnect(connect, key):
    connect.conn.run_exc = asyncssh.ConnectionLost("gone")
    with pytest.raises(GuardConnectionError):
        await make(key).status()


@pytest.mark.asyncio
async def test_known_hosts_none(connect, key):
    await make(key).status()
    assert connect.kwargs["known_hosts"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [22, 2222])
async def test_known_hosts_from_key(connect, key, port):
    pub = key.export_public_key().decode().strip()
    await make(key, port=port, known=pub).status()
    kh = connect.kwargs["known_hosts"]
    assert connect.kwargs["port"] == port
    # the imported known_hosts matches the pinned key for exactly this host/port
    matched = kh.match("h.example", "", port)
    assert any(k.public_data == key.public_data for k in matched[0])


@pytest.mark.asyncio
async def test_known_hosts_wrong_port_does_not_match(connect, key):
    pub = key.export_public_key().decode().strip()
    await make(key, port=2222, known=pub).status()
    kh = connect.kwargs["known_hosts"]
    assert not kh.match("h.example", "", 22)[0]


@pytest.mark.asyncio
async def test_fetch_host_key(monkeypatch, key):
    seen = {}

    async def fake(host, port):
        seen.update(host=host, port=port)
        return key

    monkeypatch.setattr(guard_mod.asyncssh, "get_server_host_key", fake)
    line = await make(key, port=2200).fetch_host_key()
    assert line == key.export_public_key().decode().strip()
    assert seen == {"host": "h.example", "port": 2200}


@pytest.mark.asyncio
async def test_fetch_host_key_errors(monkeypatch, key):
    async def boom(host, port):
        raise OSError("no route")

    monkeypatch.setattr(guard_mod.asyncssh, "get_server_host_key", boom)
    with pytest.raises(GuardConnectionError):
        await make(key).fetch_host_key()

    async def none(host, port):
        return None

    monkeypatch.setattr(guard_mod.asyncssh, "get_server_host_key", none)
    with pytest.raises(GuardConnectionError):
        await make(key).fetch_host_key()
