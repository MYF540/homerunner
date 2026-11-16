"""Tests for server/homerunner-guard using a fake command runner (no root, no lxc)."""

import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "server" / "homerunner-guard"


def load():
    loader = importlib.machinery.SourceFileLoader("homerunner_guard", str(SCRIPT))
    spec = importlib.util.spec_from_loader("homerunner_guard", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Fake:
    """Simulates lxc/systemctl/pgrep/who. Records every call."""

    def __init__(self, containers, mode="lxd"):
        # containers: {name: {"status": "Running", "services": {svc: active}, "job": False}}
        self.c = containers
        self.mode = mode
        self.calls = []
        self.who = ""
        self.fail = set()  # substrings: command containing it fails (rc 1)
        self.raise_on = set()  # substrings: raises CmdError
        self.job_after_pgrep = {}  # container -> number of pgrep calls after which job appears
        self.pgrep_count = {}
        self.poweroff = []
        self.hooks = []

    def __call__(self, args, timeout=60, env=None):
        mod = self.mod
        self.calls.append(list(args))
        line = " ".join(args)
        for s in self.raise_on:
            if s in line:
                raise mod.CmdError("boom: " + line)
        for s in self.fail:
            if s in line:
                return 1, "", "failed"
        if args[0] == "who":
            return 0, self.who, ""
        if args[0] == "lxc" and args[1] == "list":
            return (
                0,
                json.dumps(
                    [{"name": n, "status": d.get("status", "Running")} for n, d in self.c.items()]
                ),
                "",
            )
        if args[0] == "lxc" and args[1] == "exec":
            return self.inside(args[2], args[4:])
        if args[:3] == ["sudo", "-n", "/usr/bin/systemctl"] and args[3] in ("stop", "start"):
            return self.inside("host", ["systemctl", *args[3:]])
        if args[0] == "/usr/bin/true-poweroff" or "poweroff" in line:
            self.poweroff.append(line)
            return 0, "", ""
        if args[0].endswith(".d/10-hook") or "/hooks/" in args[0]:
            self.hooks.append((args[0], (env or {}).get("HOMERUNNER_EVENT")))
            return (1, "", "") if args[0].endswith("20-bad") else (0, "", "")
        if args[0] in ("systemctl", "pgrep"):
            return self.inside("host", args)
        raise AssertionError(f"unexpected command {args!r}")

    def inside(self, c, args):
        d = self.c[c]
        if args[0] == "pgrep":
            n = self.pgrep_count[c] = self.pgrep_count.get(c, 0) + 1
            if c in self.job_after_pgrep and n > self.job_after_pgrep[c]:
                d["job"] = True
            return (0 if d.get("job") else 1), "", ""
        assert args[0] == "systemctl"
        if args[1] == "list-units":
            import fnmatch

            glob = args[-1]
            out = "".join(
                f"{s} loaded {'active' if a else 'inactive'} running x\n"
                for s, a in d["services"].items()
                if fnmatch.fnmatchcase(s, glob)
            )
            return 0, out, ""
        if args[1] == "is-active":
            return (0 if d["services"][args[-1]] else 3), "", ""
        if args[1] == "stop":
            d["services"][args[2]] = False
            return 0, "", ""
        if args[1] == "start":
            d["services"][args[2]] = True
            return 0, "", ""
        raise AssertionError(args)


@pytest.fixture
def env(tmp_path, monkeypatch):
    mod = load()
    conf = tmp_path / "guard.conf"
    inhibit = tmp_path / "inhibit"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    state = tmp_path / "state"

    def write(extra="", mode="lxd"):
        conf.write_text(
            f"[guard]\nmode = {mode}\ninhibit_file = {inhibit}\npre_shutdown_dir = {hooks}\nstate_dir = {state}\n"
            "poweroff_command = sudo -n /usr/bin/systemctl poweroff\n"
            f"{extra}\n"
        )

    write()
    fake = Fake(
        {
            "runner-1": {"services": {"actions.runner.example-org.r1.service": True}, "job": False},
            "runner-2": {"services": {"actions.runner.example-org.r2.service": True}, "job": False},
        }
    )
    fake.mod = mod
    monkeypatch.setattr(mod, "run_cmd", fake)
    monkeypatch.setattr(mod, "read_uptime", lambda: 1234)

    class E:
        pass

    e = E()
    e.mod, e.fake, e.write = mod, fake, write
    e.inhibit, e.hooks, e.state = inhibit, hooks, state
    e.env = {"HOMERUNNER_GUARD_CONF": str(conf)}

    def call(cmd, via_ssh=True):
        out = io.StringIO()
        if via_ssh:
            rc = mod.main(["homerunner-guard"], dict(e.env, SSH_ORIGINAL_COMMAND=cmd), out)
        else:
            rc = mod.main(["homerunner-guard", cmd], dict(e.env), out)
        lines = out.getvalue().splitlines()
        assert len(lines) == 1
        return rc, json.loads(lines[0])

    e.call = call
    return e


def test_status_lxd(env):
    env.fake.who = "alice pts/0 2024-01-01 10:00\nalice pts/1 x\nbob pts/2 x\n"
    env.write("ignore_users = bob")
    rc, o = env.call("status")
    assert rc == 0
    assert o["version"] == 1 and o["inhibit"] is False and o["uptime_seconds"] == 1234
    assert o["users"] == ["alice"]
    assert o["runners"][0] == {
        "name": "runner-1",
        "service": "actions.runner.example-org.r1.service",
        "active": True,
        "job": False,
    }
    assert len(o["runners"]) == 2 and o["hostname"]


def test_status_job_and_inhibit(env):
    env.fake.c["runner-2"]["job"] = True
    env.inhibit.write_text("")
    _, o = env.call("status")
    assert o["inhibit"] is True
    assert [r["job"] for r in o["runners"]] == [False, True]


def test_glob_exclude_and_not_running(env):
    env.fake.c["other"] = {"services": {"actions.runner.x.y.service": True}}
    env.fake.c["stopped"] = {"status": "Stopped", "services": {"actions.runner.x.z.service": True}}
    env.write("container_glob = runner-*\ncontainer_exclude = runner-2")
    _, o = env.call("status")
    assert [r["name"] for r in o["runners"]] == ["runner-1"]


def test_service_glob_filters(env):
    env.fake.c["runner-1"]["services"]["ssh.service"] = True
    _, o = env.call("status")
    assert len(o["runners"]) == 2


def test_status_host_mode(env):
    env.write(mode="host")
    env.fake.c = {
        "host": {
            "services": {
                "actions.runner.example-org.a.service": True,
                "actions.runner.example-org.b.service": False,
            },
            "job": False,
        }
    }
    _, o = env.call("status")
    assert [(r["name"], r["active"]) for r in o["runners"]] == [
        ("actions.runner.example-org.a.service", True),
        ("actions.runner.example-org.b.service", False),
    ]
    assert not any(c[0] == "lxc" for c in env.fake.calls)


def test_shutdown_accepted(env):
    h = env.hooks / "10-hook"
    h.write_text("#!/bin/sh\n")
    h.chmod(0o755)
    bad = env.hooks / "20-bad"
    bad.write_text("#!/bin/sh\n")
    bad.chmod(0o755)
    (env.hooks / "30-notexec").write_text("x")
    rc, o = env.call("shutdown")
    assert rc == 0 and o["result"] == "accepted" and o["reason"] is None
    assert "20-bad" in o["detail"]
    assert all(r["active"] is False for r in o["runners"])
    stops = [c[2] for c in env.fake.calls if c[:2] == ["lxc", "exec"] and "stop" in c]
    assert stops == ["runner-1", "runner-2"]
    assert [x[1] for x in env.fake.hooks] == ["shutdown", "shutdown"]
    assert (env.state / "planned-off").read_text().strip()
    assert env.fake.poweroff == ["sudo -n /usr/bin/systemctl poweroff"]


def test_shutdown_dry_run(env):
    env.write("dry_run = true")
    _, o = env.call("shutdown")
    assert o["result"] == "accepted"
    assert env.fake.poweroff == []
    assert (env.state / "planned-off").exists()


def test_shutdown_host_mode(env):
    env.write(mode="host")
    env.fake.c = {
        "host": {"services": {"actions.runner.example-org.a.service": True}, "job": False}
    }
    _, o = env.call("shutdown")
    assert o["result"] == "accepted"
    assert [
        "sudo",
        "-n",
        "/usr/bin/systemctl",
        "stop",
        "actions.runner.example-org.a.service",
    ] in env.fake.calls


def test_refused_inhibit(env):
    env.inhibit.write_text("")
    rc, o = env.call("shutdown")
    assert rc == 0 and o["result"] == "refused" and o["reason"] == "inhibited"
    assert env.fake.poweroff == []


def test_refused_users(env):
    env.fake.who = "alice pts/0 x\n"
    _, o = env.call("shutdown")
    assert o["reason"] == "users_logged_in"
    env.write("refuse_if_users_logged_in = false")
    _, o = env.call("shutdown")
    assert o["result"] == "accepted"


def test_refused_job_before(env):
    env.fake.c["runner-2"]["job"] = True
    _, o = env.call("shutdown")
    assert o["reason"] == "job_running"
    assert not any("stop" in c for c in env.fake.calls)
    assert env.fake.poweroff == []


def test_job_appears_during_sequence(env):
    # runner-1 pgrep: 1 (discovery) + 1 (recheck) clean; runner-2 job appears at recheck
    env.fake.job_after_pgrep["runner-2"] = 1
    _, o = env.call("shutdown")
    assert o["result"] == "refused" and o["reason"] == "job_running"
    assert all(d["services"][s] for d in env.fake.c.values() for s in d["services"])
    assert any(
        c[:2] == ["lxc", "exec"] and "start" in c and c[2] == "runner-1" for c in env.fake.calls
    )
    assert env.fake.poweroff == [] and not (env.state / "planned-off").exists()


def test_command_failure_restarts(env):
    env.fake.fail.add("runner-2 -- systemctl stop")
    _, o = env.call("shutdown")
    assert o["result"] == "refused" and o["reason"] == "error" and "stop" in o["detail"]
    assert env.fake.c["runner-1"]["services"]["actions.runner.example-org.r1.service"] is True
    assert env.fake.poweroff == []


def test_lxc_missing_is_error(env):
    env.fake.raise_on.add("lxc list")
    rc, o = env.call("shutdown")
    assert rc == 0 and o["reason"] == "error"


def test_unknown_command(env):
    rc, o = env.call("rm -rf /")
    assert (
        rc == 2
        and o["result"] == "refused"
        and o["reason"] == "error"
        and o["detail"] == "unknown command"
    )
    rc, o = env.call("", via_ssh=False)
    assert rc == 2


def test_argv_command_and_ssh_precedence(env):
    rc, o = env.call("status", via_ssh=False)
    assert rc == 0 and "runners" in o
    out = io.StringIO()
    rc = env.mod.main(["g", "shutdown"], dict(env.env, SSH_ORIGINAL_COMMAND="status"), out)
    assert "result" not in json.loads(out.getvalue())


def test_cancel(env):
    env.call("shutdown")
    assert (env.state / "planned-off").exists()
    rc, o = env.call("cancel")
    assert rc == 0 and o["result"] == "ok"
    assert all(r["active"] for r in o["runners"])
    assert not (env.state / "planned-off").exists()


def test_pgrep_error_is_not_treated_as_no_job(env):
    env.fake.fail.add("runner-2 -- pgrep")  # rc 1 == "no match" in the fake; use rc 255 below
    orig = env.fake.__call__

    def call(args, timeout=60, env_=None):
        if "pgrep" in args and "runner-2" in args:
            return 255, "", "lxc: cannot connect"
        return orig(args, timeout, env_)

    env.fake.fail.clear()
    env.mod.run_cmd = call
    _, o = env.call("shutdown")
    assert o["result"] == "refused" and o["reason"] == "error"
    assert not any("stop" in c for c in env.fake.calls)
    assert env.fake.poweroff == []


def test_who_failure_refuses_when_users_matter(env):
    orig = env.fake.__call__

    def call(args, timeout=60, env_=None):
        if args[0] == "who":
            return 1, "", "who: broken"
        return orig(args, timeout, env_)

    env.mod.run_cmd = call
    _, o = env.call("shutdown")
    assert o["result"] == "refused" and o["reason"] == "error"
    assert env.fake.poweroff == []


def test_poweroff_failure_restarts_runners(env):
    orig = env.fake.__call__

    def call(args, timeout=60, env_=None):
        if "poweroff" in " ".join(args):
            return 1, "", "denied"
        return orig(args, timeout, env_)

    env.mod.run_cmd = call
    env.call("shutdown")
    assert all(d["services"][s] for d in env.fake.c.values() for s in d["services"])
    assert not (env.state / "planned-off").exists()


def test_job_after_stop_aborts_and_restarts(env):
    # pgrep counts per container: discovery 1, recheck 2, final recheck 3 -> job appears
    env.fake.job_after_pgrep["runner-1"] = 2
    _, o = env.call("shutdown")
    assert o["result"] == "refused" and o["reason"] == "job_running"
    assert all(d["services"][s] for d in env.fake.c.values() for s in d["services"])
    assert env.fake.poweroff == []
