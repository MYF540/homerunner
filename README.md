# Homerunner

[English](README.md) | [Deutsch](README.de.md)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Home Assistant custom integration that powers a self-hosted GitHub Actions runner host on and off on demand, and starts scheduled workflows on time.

- **Power off when idle.** After an adjustable idle period without queued or running jobs, the host is shut down through a guard script on the host.
- **Wake on demand.** When a job for your runner labels is queued in any repository of an organisation or user, the host is powered on through its BMC (Redfish), optionally also with Wake-on-LAN. The job waits at GitHub until the runners are back.
- **Scheduled dispatch.** Workflows are dispatched at fixed times (for example a nightly test suite at 03:00), with the host woken beforehand. Missed times are not caught up.

## How it works

Home Assistant polls the GitHub API every 60 seconds for queued and in-progress jobs in all non-archived repositories of the owner (minus the ones you exclude). A job counts only if all of its labels are among your configured runner labels. The BMC is polled every 30 seconds for power state and power draw; the guard script on the host is queried over SSH for runner status.

A state machine (`off`, `starting`, `ready`, `active`, `shutting_down`, `wake_failed`) decides:

- **Wake** when automatic mode is on and a matching job is queued, a schedule's pre-wake window has started, or the host has been off for the forced-wake period (default 5 days, to keep runner registrations alive).
- **Shut down** when no matching job is queued or running for the idle time (default 2 hours after real work, 20 minutes if no job came since the wake).
- **Never shut down** while automatic mode is off, a keep-awake period is active, a schedule is due within 30 minutes, the minimum uptime (15 minutes) is not reached, GitHub is unreachable, or the power state is unknown.
- **Start limit:** at most 8 starts per day (local time). After that the host stays on until midnight and you are notified.

### Safety model

Home Assistant never powers the host off through the BMC on its own initiative. Automatic and manual shutdowns go through `homerunner-guard` on the host (see [server/install.md](server/install.md)):

- It is called over SSH with a key that is pinned in `authorized_keys` to this one command, which accepts only `status`, `shutdown` and `cancel`.
- On `shutdown` it refuses if a runner is executing a job, if the inhibit file exists (maintenance switch), or if users are logged in. If it stops runners and then finds a job, it starts them again.
- Only if all checks pass does it stop the runners and run the poweroff command.
- The only way around the guard is the explicit "Force off" button or `homerunner.shutdown` with `force: true`, which uses the BMC. The button is disabled by default.
- If SSH fails, nothing is shut down automatically.

## Requirements

| What | Details |
|---|---|
| Home Assistant | 2025.3.0 or newer (see `hacs.json`) |
| BMC | Redfish over HTTPS, reachable from Home Assistant. Use a dedicated BMC user with the smallest role that can power the system on and off. |
| Runner host | SSH from Home Assistant, `homerunner-guard` installed (Python 3, standard library only) |
| Wake-on-LAN (optional) | MAC address of the host; Home Assistant must be in the same layer-2 network |
| GitHub, observer token | Fine-grained personal access token, resource owner the organisation (or your user), **all repositories**, permission **Actions: Read** |
| GitHub, dispatch token | Only for schedules: one fine-grained token per schedule, limited to the target repository, **Actions: Read and write**. If left empty, the observer token is used, which then needs write access too. |

Tokens are stored only in the config entry and are not logged or shown in attributes.

## Installation

### HACS (custom repository)

1. In HACS open the menu, choose **Custom repositories**, add `https://github.com/MYF540/homerunner` with the type **Integration**.
2. Download **Homerunner** and restart Home Assistant.

### Manual

Copy `custom_components/homerunner` into the `custom_components` directory of your Home Assistant configuration and restart.

## Server setup

Details are in [server/install.md](server/install.md), with all guard options in [server/guard.conf.example](server/guard.conf.example). In short:

1. Install `server/homerunner-guard` as `/usr/local/bin/homerunner-guard`, create `/etc/homerunner/guard.conf` (all keys optional) and `/var/lib/homerunner`.
2. Create a dedicated user (in the `lxd` group for the default `mode = lxd`; set `mode = host` if runner services run directly on the host).
3. Create a key pair and add the public key to `authorized_keys` with `command="/usr/local/bin/homerunner-guard",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty`. The private key must have no passphrase.
4. Add a sudoers rule for `systemctl poweroff` (and, in host mode, for stopping and starting the runner services).
5. Optional: maintenance switch `/etc/homerunner/inhibit`, pre-shutdown hooks, planned-off marker for monitoring.
6. Create the BMC user; set "restore on AC power loss" to "last state" in the firmware.
7. Test with `homerunner-guard status` and, with `dry_run = true` in `guard.conf`, `shutdown` and `cancel`.

## Configuration

Add the integration under **Settings > Devices & services > Add integration > Homerunner**.

1. **GitHub access:** token, API base URL (change only for GitHub Enterprise Server), organisation or user, excluded repositories (names without owner), runner labels (default `self-hosted`). The token is validated by listing repositories and reading runs.
2. **Runner server:** BMC address, user, password, "verify the BMC certificate" (off by default because BMCs usually have self-signed certificates), optional MAC address for Wake-on-LAN, SSH host, port, user and private key, optional SSH host key. If the host key is left empty, it is fetched and you confirm it on the next page; it is then pinned. The BMC and the guard are tested before the entry is created.
3. **Rules and filters** (entry options): idle time, short idle time, minimum uptime, maximum starts per day, forced wake after N days off and its duration, wake timeout, no shutdown before a schedule, alert for waiting jobs, excluded repositories, runner labels, webhook.
4. **Schedules** are added as sub-entries of the integration (see below).

A new entry starts with **Automatic off** and **Dry run on**.

### Options

| Option | Default | Meaning |
|---|---|---|
| Idle time after work | 2 h | Time without matching jobs before shutdown, after at least one job ran since the wake. Also available as number entity (0.25 to 24 h). |
| Short idle time | 20 min | Used instead if no job came since the wake. Also a number entity (5 to 240 min). |
| Minimum uptime | 15 min | No shutdown before this uptime. |
| Maximum starts per day | 8 | Protects against power cycling. |
| Forced wake after | 5 days | If the host was off this long it is started once to keep runner registrations alive (GitHub removes runners that stay offline for 14 days). |
| Forced wake duration | 30 min | How long it stays on, then the short idle time applies. |
| Wake timeout | 10 min | If no runner is ready by then the state becomes `wake_failed`, you are notified and the wake is retried once per timeout. |
| No shutdown before a schedule | 30 min | |
| Alert for waiting jobs | 15 min | Notification if a matching job waits although the host is up. |

## Recommended rollout

1. **Dry run and automatic on, no real power switching.** Keep **Dry run** on and switch **Automatic** on. The state machine decides and logs, and notifications are prefixed with "[dry run]", but nothing is powered on or off. Watch the *State*, *Shutdown at* and *Last wake reason* entities and the logbook for a few days.
2. **Tune the idle time** with the *Idle time* number entity. A longer value keeps the host on between pull requests; a shorter one saves more power but causes more starts.
3. **Enable wake:** turn **Dry run** off while the host is still switched off or running. Wake requests now go to the BMC. Because shutdowns also become real with the same switch, first set `homerunner-guard` to `dry_run = true` (see [server/install.md](server/install.md)) or enable the maintenance file `/etc/homerunner/inhibit`, which refuses every shutdown.
4. **Enable shutdown:** remove the inhibit file and set the guard to `dry_run = false`. Check the first real shutdown and wake by hand.

Dry run is one switch for both directions: it affects waking and shutting down. In dry run the state machine assumes its actions succeeded.

## Entities

All entities belong to one device per entry. "Diagnostic" and "Config" refer to the entity category.

### Server

| Entity | Type | Description |
|---|---|---|
| State | sensor | `off`, `starting`, `ready`, `active`, `shutting_down`, `wake_failed`, `unknown`. Attributes: `blockers`, `dry_run`. |
| Queued jobs | sensor | Matching jobs waiting; attribute `jobs` with repo, name and URL. |
| Running jobs | sensor | Matching jobs in progress; same attributes. |
| Shutdown at | sensor | Timestamp when the idle period ends. |
| Power | sensor | Watts reported by the BMC; unavailable if the BMC cannot be read. |
| Last wake reason | sensor (diagnostic) | `queued_job`, `schedule`, `manual`, `forced_wake`. |
| Starts today | sensor (diagnostic) | Counter for the start limit. |
| API rate limit remaining | sensor (diagnostic) | Attributes `limit`, `reset_at`. |
| Server on | binary sensor | Power state from the BMC. |
| GitHub reachable | binary sensor (diagnostic) | |
| Problem | binary sensor (diagnostic) | On if wake failed, GitHub or the BMC cannot be read, or the host is on but the guard does not answer. |
| Automatic | switch (config) | Master switch for automatic wake and shutdown. |
| Dry run | switch (config) | Decide and log, but do not power anything. |
| Idle time, Short idle time | number (config) | Change the idle timers at runtime. |
| Wake | button | Power on now. |
| Safe shutdown | button | Shut down through the guard (refused if it says so). |
| Keep awake for 2 hours | button | Blocks automatic shutdown for 2 hours. |
| Force off | button | BMC power off without the guard. Disabled by default; running jobs are lost. |

### Per schedule

| Entity | Type | Description |
|---|---|---|
| Enabled | switch (config) | |
| Dispatch now | button | Runs the dispatch (with retries) immediately. |
| Next dispatch | sensor | Next fire time. |
| Last dispatch | sensor | Time of the last dispatch; attributes `result`, `status`, `run_id`, `error`, `attempts`. |
| Last run | sensor | Result or status of the workflow run; attributes `url`, `run_id`. |

## Services

The optional field `config_entry` selects the entry when several are configured.

| Service | Fields | Description |
|---|---|---|
| `homerunner.wake` | | Power on regardless of the Automatic switch. |
| `homerunner.shutdown` | `force` (default false) | Shut down through the guard. With `force: true` the BMC powers off without asking the guard. |
| `homerunner.keep_awake` | `duration` (default 2 hours) | Block automatic shutdown for that long. |
| `homerunner.dispatch` | `schedule` or `repository` + `workflow`; optional `ref`, `inputs` | Dispatch a schedule's workflow, or any workflow of any repository. `ref` and `inputs` override the schedule's values. `inputs` must be a JSON object. |

```yaml
service: homerunner.dispatch
data:
  repository: example-org/example-repo
  workflow: nightly.yml
  ref: main
  inputs:
    full: "true"
```

## Events

Use these in automations. Events from the state machine:

| Event | Payload | When |
|---|---|---|
| `homerunner_woke` | `wake_reason`, `seconds`, `recovered` | The host is up and runners are ready. |
| `homerunner_shut_down` | | The host was seen powered off after a shutdown. |
| `homerunner_shutdown_refused` | `reason` (`job_running`, `inhibited`, `users_logged_in`, `error`) | The guard refused or failed. |
| `homerunner_wake_failed` | `entry_id`, `error`, `minutes` | The host did not become ready in time. Also a persistent notification. |
| `homerunner_start_limit` | `entry_id`, `starts_today`, `max_starts_per_day` | Start limit reached. Also a persistent notification. |
| `homerunner_unexpected_off` | `entry_id`, `previous_state` | The host went off without a shutdown request. Also a persistent notification. |
| `homerunner_queue_stuck` | `entry_id`, `queued`, `waiting_since` | A job waits longer than the alert time although the host is up. Also a persistent notification. |
| `homerunner_shutdown_stuck` | `entry_id`, `minutes`, `accepted` | Host still on 15 minutes after a shutdown request. Also a persistent notification. |

Schedule events:

| Event | Payload |
|---|---|
| `homerunner_dispatched` | `entry_id`, `schedule`, `repository`, `workflow`, `ref`, `run_id`, `status`, `attempts` |
| `homerunner_dispatch_failed` | the same, plus `error` |

A rejected token (HTTP 401) also creates a repair issue "GitHub token rejected". Renewing the observer token is possible through the re-authentication flow.

## Schedules

Add a schedule via the integration page: **Add schedule** (sub-entry type "schedule"). Existing schedules can be reconfigured there.

| Field | Default | Notes |
|---|---|---|
| Name | | |
| Repository | | `example-org/example-repo` |
| Workflow | | File name or id, for example `nightly.yml` |
| Branch or tag (`ref`) | `main` | |
| Inputs | `{}` | JSON object |
| Time | 03:00 | |
| Weekdays | all | |
| Time zone | HA time zone | IANA name; daylight saving time is handled. Nonexistent local times fire at the first valid instant, ambiguous ones fire once. |
| Wake beforehand | on | |
| Wake lead time | 15 min | Choose at least the measured boot time plus a reserve. |
| Retries | 5 | Backoff 30 s, 60 s, 120 s, 240 s, 480 s. |
| Enabled | on | |
| Dispatch token | empty | Optional; falls back to the observer token. |

Behaviour:

- **No catch-up.** If Home Assistant was down at the fire time, the dispatch is not made later; the next fire time is computed from "now".
- Retries happen only for network errors, timeouts, 5xx responses and short rate limits. 401 raises the repair issue; 403, 404 and 422 fail immediately.
- The dispatch is not delayed until the host is ready. If waking fails, the job waits at GitHub.
- If GitHub returns a run id, "Last run" follows that run until it completes.

## Optional webhook

Polling is always active. The webhook only triggers an immediate queue refresh when GitHub reports a matching `workflow_job`, so the host is woken without waiting for the next poll.

1. Open the integration options and enable the **workflow_job webhook**. Id and secret are generated; the next page shows the payload URL and the secret.
2. In GitHub (organisation settings > Webhooks > Add webhook):
   - Payload URL: the URL shown by Home Assistant. It must be reachable from the internet (for example Nabu Casa Cloud or your own tunnel).
   - Content type: `application/json`
   - Secret: the secret shown by Home Assistant
   - Events: select individual events, **Workflow jobs**
3. Requests with an invalid `X-Hub-Signature-256` are rejected with 401; `ping` is answered with 200.

## Energy dashboard

The *Power* sensor reports watts. To get energy for the Energy dashboard, create a helper under **Settings > Devices & services > Helpers > Integral sensor** (Riemann sum, metric prefix k, time unit hours) with the Power sensor as source, and add the result as a consumption source. While the host is off, the BMC usually still reports the standby draw.

## Troubleshooting

| Symptom | Check |
|---|---|
| Config flow: `cannot_connect_bmc` / `invalid_auth_bmc` | BMC address, user and password; Redfish enabled; certificate verification (off for self-signed certificates). |
| `cannot_connect_guard` | SSH host, port, user and key; `homerunner-guard` installed and executable; `authorized_keys` line; key without passphrase. |
| `guard_protocol_error` | The forced command in `authorized_keys` is not the guard script. |
| Host not woken | Automatic on and Dry run off? See the *State* attribute `blockers`, the *Starts today* sensor against the limit, and whether *GitHub reachable* is on. Does the job's label set fit entirely inside your runner labels? Excluded or archived repositories are ignored. |
| Host does not shut down | Same checks, then the idle timer (*Shutdown at*), a schedule due soon, minimum uptime, `homerunner_shutdown_refused` events, the inhibit file, logged-in users (also ones logged in over SSH), or `dry_run` in `guard.conf`. |
| `wake_failed` | The host did not report ready runners within the wake timeout. Check BMC and boot time, runner services starting on boot, and that the guard `status` shows active runners. |
| Token rejected | Renew it via the repair issue or reauthentication; check "Actions" permission and repository access. |
| Rate limit | See *API rate limit remaining*. The limit is 5000 requests per hour per GitHub user, shared with every other use of that user. Unchanged responses (ETag) do not count. When the remaining quota falls under 10 percent, repositories without recent pushes are skipped. |
| Webhook has no effect | Payload URL reachable, secret equal, event "Workflow jobs", recent deliveries in GitHub. |

Download diagnostics from the integration page; secrets are redacted. Enable debug logging with `logger: logs: custom_components.homerunner: debug`.

## Limitations

- Home Assistant becomes a requirement for CI. If it is down while the host is off, nothing wakes the host; use the BMC by hand.
- GitHub keeps a queued job for up to 24 hours. If the host takes longer than that to come up, GitHub fails the job.
- The API rate limit of 5000 requests per hour applies per GitHub user, not per token.
- Polling delay is up to 60 seconds before a wake (less with the webhook), plus the host's boot time.
- Schedules do not catch up after downtime.
- Shutdown means power off (S5), not suspend.
- One guard host per config entry; an entry watches one owner.
- Jobs are matched by labels only; the integration does not read the runner list from GitHub.

## License

[MIT](LICENSE)
