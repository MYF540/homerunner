# Installing homerunner-guard on the runner host

`homerunner-guard` is a small Python 3 script (standard library only) that the
Home Assistant integration calls over SSH. It reports runner status and stops the
runners safely before the host is powered off. Examples below use the user
`homerunner`; adapt names to your setup.

## 1. Install the script

```sh
sudo install -m 0755 homerunner-guard /usr/local/bin/homerunner-guard
sudo mkdir -p /etc/homerunner /var/lib/homerunner
sudo cp guard.conf.example /etc/homerunner/guard.conf   # edit as needed
sudo chown homerunner: /var/lib/homerunner
```

All config keys are optional (see `guard.conf.example`). The default mode is `lxd`
(runners inside LXD containers); set `mode = host` if the runner services run
directly on the host.

## 2. User and SSH key

Use a dedicated user that is member of the `lxd` group (lxd mode):

```sh
sudo adduser --disabled-password homerunner
sudo usermod -aG lxd homerunner
```

Create a dedicated key pair, either on a trusted machine
(`ssh-keygen -t ed25519 -f homerunner_key -C homerunner`) and paste the private key into
the Home Assistant config flow, or generate it in Home Assistant and paste the public
key here. Then add this line to `~homerunner/.ssh/authorized_keys`:

```
command="/usr/local/bin/homerunner-guard",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... homerunner
```

The key can then only run the guard, which accepts only `status`, `shutdown`, `cancel`.

## 3. sudo rules

Create `/etc/sudoers.d/homerunner`:

```
homerunner ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
```

For `mode = host`, the guard also needs to stop and start the runner services
(in lxd mode this happens inside the container as root through `lxc exec`):

```
homerunner ALL=(root) NOPASSWD: /usr/bin/systemctl stop actions.runner.example-org.*, /usr/bin/systemctl start actions.runner.example-org.*
```

Validate before relying on it:

```sh
sudo visudo -cf /etc/sudoers.d/homerunner
sudo chmod 0440 /etc/sudoers.d/homerunner
```

## 4. Maintenance switch (inhibit)

While the file exists, every `shutdown` is refused with reason `inhibited`:

```sh
sudo touch /etc/homerunner/inhibit     # block automatic power-off
sudo rm /etc/homerunner/inhibit        # allow it again
```

The file must be readable by the guard user (it only needs to exist).

## 5. Pre-shutdown hooks

Executable files in `/etc/homerunner/pre-shutdown.d/` run in sorted order after all
runners are stopped and before power-off, each with `hook_timeout` seconds and
`HOMERUNNER_EVENT=shutdown` in the environment. A failing hook is reported in the
`detail` field but never blocks the shutdown. Example `10-status-page`:

```sh
#!/bin/sh
curl -fsS -m 10 -X POST -d 'planned off' https://status.example.org/api/note
```

## 6. Planned-off marker

On an accepted shutdown the guard writes an ISO timestamp to
`/var/lib/homerunner/planned-off` (or `~/.local/state/homerunner/planned-off` if the
state directory is not writable). Monitoring on the host can read it to distinguish a
planned power-off from an outage. `cancel` removes it; clear it yourself after the
host has booted again if your monitoring needs that (for instance in a boot-time unit).

## 7. BMC and BIOS

- Create a dedicated Redfish user on the BMC with the smallest role that can power
  the system on and off ("Operator" in most BMCs). Do not reuse the administrator.
- In the BIOS/firmware set "Restore on AC power loss" to "last state", so the host
  returns to the correct state after a power cut.

## 8. Testing

1. On the host: `homerunner-guard status` and check the JSON (runners, users, inhibit).
2. From another machine: `ssh -i homerunner_key homerunner@host status`.
   Any other command (for example `ssh ... ls`) must be answered with
   `"detail":"unknown command"`.
3. Set `dry_run = true` in `/etc/homerunner/guard.conf`, run
   `ssh -i homerunner_key homerunner@host shutdown`. Runners are stopped, hooks run and
   the marker is written, but the host is not powered off. Restore the runners with
   `ssh -i homerunner_key homerunner@host cancel`, then set `dry_run = false`.

## Behaviour notes

- Output is exactly one JSON object; exit code 0 even for `refused` (2 only for an
  unknown command).
- If a job is found before or while stopping runners, every runner stopped by that
  call is started again and the result is `refused` / `job_running`.
- A lock file in the state directory prevents overlapping shutdowns.
