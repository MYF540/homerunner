# Homerunner – architecture and internal interfaces

This document is the contract between the modules of `custom_components/homerunner`.
The German design rationale lives in `docs/plan.md`. Everything here is generic:
nothing is tied to a specific organisation, repository, server model or BMC vendor.

## Layout

```
custom_components/homerunner/
  __init__.py          setup/unload, runtime data, services, wiring of the parts below
  manifest.json        domain "homerunner", config_flow true, iot_class "local_polling", requirements ["asyncssh>=2.14"]
  const.py             DOMAIN, config keys, defaults (single source of truth for all names)
  github.py            async GitHub REST client (aiohttp), ETag cache, error classes
  queue_watcher.py     scans all repos of an owner for queued/in-progress jobs matching the runner labels
  bmc.py               Redfish client (power state, power on/off, watts) + Wake-on-LAN magic packet
  guard.py             asyncssh client for the server-side guard script (status / shutdown)
  power_manager.py     pure state machine (no HA imports): decides wake / shutdown / notifications
  scheduler.py         pure schedule maths (next fire, pre-wake) + dispatch with retry
  coordinator.py       DataUpdateCoordinators: GitHub queue (60 s) and server state (30 s)
  manager.py           HA glue: runs the PowerStateMachine, executes actions, persists state in a Store
  config_flow.py       config flow, options flow, schedule subentries
  entity.py            base entity / device info
  sensor.py binary_sensor.py switch.py button.py number.py
  services.yaml, repairs.py, diagnostics.py, webhook.py (optional workflow_job webhook)
  strings.json, translations/en.json, translations/de.json
server/
  homerunner-guard     guard script for the runner host (Python 3, stdlib only)
  guard.conf.example
  install.md           SSH key, authorized_keys forced command, sudoers line, inhibit file, BMC user
tests/                 pytest + pytest-homeassistant-custom-component; tests/server for the guard script
```

## const.py (names every module uses)

```python
DOMAIN = "homerunner"
# config entry data
CONF_GITHUB_TOKEN = "github_token"
CONF_API_BASE = "api_base"                  # default "https://api.github.com"
CONF_OWNER = "owner"                        # organisation or user whose repos are watched
CONF_EXCLUDED_REPOS = "excluded_repos"      # list[str] of repo names (without owner)
CONF_RUNNER_LABELS = "runner_labels"        # list[str], default ["self-hosted"]
CONF_BMC_HOST = "bmc_host"; CONF_BMC_USERNAME = "bmc_username"; CONF_BMC_PASSWORD = "bmc_password"
CONF_BMC_VERIFY_SSL = "bmc_verify_ssl"      # default False (BMCs ship self-signed certs)
CONF_WOL_MAC = "wol_mac"                    # optional
CONF_SSH_HOST = "ssh_host"; CONF_SSH_PORT = "ssh_port"; CONF_SSH_USERNAME = "ssh_username"
CONF_SSH_KEY = "ssh_private_key"            # PEM/OpenSSH text
CONF_SSH_KNOWN_HOST_KEY = "ssh_host_key"    # optional public host key line; empty = accept and pin on first use
# options (rules)
CONF_IDLE_HOURS = "idle_hours"              # default 2.0, step 0.25
CONF_SHORT_IDLE_MINUTES = "short_idle_minutes"   # default 20
CONF_MIN_UPTIME_MINUTES = "min_uptime_minutes"   # default 15
CONF_MAX_STARTS_PER_DAY = "max_starts_per_day"   # default 8
CONF_FORCED_WAKE_DAYS = "forced_wake_days"       # default 5
CONF_FORCED_WAKE_MINUTES = "forced_wake_minutes" # default 30
CONF_WAKE_TIMEOUT_MINUTES = "wake_timeout_minutes" # default 10
CONF_SCHEDULE_GUARD_MINUTES = "schedule_guard_minutes" # default 30
CONF_QUEUE_ALERT_MINUTES = "queue_alert_minutes" # default 15
CONF_AUTOMATIC = "automatic"                # default False on first setup
CONF_DRY_RUN = "dry_run"                    # default True on first setup
# schedule subentry (subentry_type "schedule")
CONF_REPOSITORY = "repository"   # "owner/name"
CONF_WORKFLOW = "workflow"       # file name or id
CONF_REF = "ref"; CONF_INPUTS = "inputs"   # inputs: JSON object text
CONF_TIME = "time"               # "HH:MM"
CONF_WEEKDAYS = "weekdays"       # list[int] 0=Mon..6=Sun, default all
CONF_TIMEZONE = "timezone"       # IANA name, default HA's timezone
CONF_PREWAKE = "prewake"; CONF_PREWAKE_MINUTES = "prewake_minutes"   # default True, 15
CONF_RETRIES = "retries"         # default 5
CONF_DISPATCH_TOKEN = "dispatch_token"  # optional, falls back to CONF_GITHUB_TOKEN
SUBENTRY_SCHEDULE = "schedule"
```

## github.py

```python
class GitHubError(Exception): status: int | None
class GitHubAuthError(GitHubError)         # 401
class GitHubForbiddenError(GitHubError)    # 403 without rate-limit headers
class GitHubRateLimitError(GitHubError)    # 403/429 with x-ratelimit-remaining == 0 or retry-after; .reset_at: datetime | None
class GitHubNotFoundError(GitHubError)     # 404
class GitHubValidationError(GitHubError)   # 422
class GitHubTransientError(GitHubError)    # network errors, timeouts, 5xx  -> the only retryable class

@dataclass(frozen=True) class Repo: full_name: str; name: str; archived: bool; pushed_at: datetime | None
@dataclass(frozen=True) class Job: id: int; run_id: int; repo: str; name: str; status: str; labels: tuple[str, ...];
                                   html_url: str | None; created_at: datetime | None; started_at: datetime | None
@dataclass(frozen=True) class RateLimit: limit: int | None; remaining: int | None; reset_at: datetime | None

class GitHubClient:
    def __init__(self, session: aiohttp.ClientSession, token: str, api_base: str = "https://api.github.com") -> None
    rate_limit: RateLimit                       # updated from every response's x-ratelimit-* headers
    async def list_repos(self, owner: str) -> list[Repo]          # tries /orgs/{owner}/repos, falls back to /users/{owner}/repos on 404; paginates (per_page=100, Link header)
    async def list_runs(self, repo: str, status: str) -> list[dict]   # /repos/{repo}/actions/runs?status=..&per_page=100 ; returns workflow_runs
    async def list_run_jobs(self, repo: str, run_id: int) -> list[Job] # /repos/{repo}/actions/runs/{id}/jobs?filter=latest&per_page=100
    async def get_run(self, repo: str, run_id: int) -> dict
    async def dispatch(self, repo: str, workflow: str, ref: str, inputs: dict | None, token: str | None = None) -> int | None
        # POST /repos/{repo}/actions/workflows/{workflow}/dispatches ; body {"ref", "inputs", "return_run_details": true}
        # 200 with {"workflow_run_id": ...} -> that id ; 204 -> None
    async def validate(self, owner: str) -> None   # list repos + read runs of the first repo; raises on failure
```
GETs send `If-None-Match` with the cached ETag; on 304 the cached body is returned
(304s do not count against the rate limit). Headers: `Authorization: Bearer`, `Accept: application/vnd.github+json`,
`X-GitHub-Api-Version: 2022-11-28`. Never log tokens.

## queue_watcher.py

```python
def job_matches(job_labels: Iterable[str], runner_labels: Iterable[str]) -> bool
    # True if job has >=1 label and every job label (case-insensitive) is in runner_labels

@dataclass class QueueSnapshot:
    queued: list[Job]; running: list[Job]   # only jobs that match the runner labels
    ok: bool                                # False if the last scan could not see GitHub reliably
    error: str | None; rate_limit: RateLimit; repos_watched: int; scanned_at: datetime

class QueueWatcher:
    def __init__(self, client: GitHubClient, owner: str, runner_labels: list[str], excluded: list[str],
                 now: Callable[[], datetime] = utcnow) -> None
    async def scan(self) -> QueueSnapshot
        # refresh repo list hourly (non-archived, minus excluded);
        # repos pushed within 30 days: every scan; older: at most every 10 min;
        # per repo list_runs("queued") and list_runs("in_progress"); for each run list_run_jobs;
        # keep jobs with status "queued"/"waiting"/"pending" -> queued, "in_progress" -> running, if job_matches.
        # If rate_limit.remaining < 10% of limit: skip non-recent repos and set snapshot.throttled = True.
        # Any GitHubError for the whole scan -> ok=False (previous jobs kept), per-repo 404 is ignored.
```

## bmc.py

```python
class PowerState(StrEnum): ON="on"; OFF="off"; UNKNOWN="unknown"
class BmcError(Exception); class BmcAuthError(BmcError); class BmcConnectionError(BmcError)

class RedfishClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, username: str, password: str, verify_ssl: bool = False)
    async def discover(self) -> None    # GET /redfish/v1 -> Systems, Chassis collections; remembers first system/chassis URIs and
                                        # the reset target + allowable ResetType values (Actions."#ComputerSystem.Reset")
    async def get_power_state(self) -> PowerState     # System.PowerState: On/PoweringOn -> ON, Off/PoweringOff -> OFF
    async def power_on(self) -> None                  # ResetType "On"
    async def power_off(self, force: bool = False) -> None  # "GracefulShutdown" or "ForceOff"
    async def get_power_watts(self) -> float | None   # Chassis Power -> PowerControl[0].PowerConsumedWatts,
                                                      # fallback PowerSubsystem / EnvironmentMetrics.PowerWatts.Reading
async def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None  # plain UDP, no extra deps
```
Basic auth over HTTPS. No vendor-specific paths.

## guard.py (client) and the guard JSON protocol

The HA side runs `ssh <user>@<host> <command>`; on the host `authorized_keys` pins
`command="/usr/local/bin/homerunner-guard"`, so the script reads the requested command from
`SSH_ORIGINAL_COMMAND` (or argv[1] when run by hand). Commands: `status`, `shutdown`, `cancel` (start runners again,
used if HA aborts). Output: exactly one JSON object on stdout, exit code 0 even for "refused".

```json
// status
{"version": 1, "hostname": "host", "runners": [{"name": "c1", "service": "actions.runner.org.c1", "active": true, "job": false}],
 "users": ["alice"], "inhibit": false, "uptime_seconds": 1234}
// shutdown
{"version": 1, "result": "accepted" | "refused", "reason": null | "job_running" | "inhibited" | "users_logged_in" | "error",
 "detail": "free text", "runners": [...same as status...]}
```

```python
@dataclass class RunnerStatus: name: str; service: str; active: bool; job: bool
@dataclass class GuardStatus: hostname: str; runners: list[RunnerStatus]; users: list[str]; inhibit: bool; uptime_seconds: int | None
    @property all_active -> bool (at least one runner and all active); @property any_job -> bool
@dataclass class ShutdownResult: accepted: bool; reason: str | None; detail: str | None
class GuardError(Exception); class GuardConnectionError(GuardError); class GuardProtocolError(GuardError)
class GuardClient:
    def __init__(self, host: str, port: int, username: str, private_key: str, known_host_key: str | None, timeout: float = 30)
    async def status(self) -> GuardStatus
    async def shutdown(self) -> ShutdownResult
    async def cancel(self) -> None
    async def fetch_host_key(self) -> str    # for trust-on-first-use in the config flow
```

## power_manager.py (pure, no Home Assistant imports, fully unit-testable)

```python
class ServerState(StrEnum): OFF="off"; STARTING="starting"; READY="ready"; ACTIVE="active";
                            SHUTTING_DOWN="shutting_down"; WAKE_FAILED="wake_failed"; UNKNOWN="unknown"
class WakeReason(StrEnum): QUEUED_JOB="queued_job"; SCHEDULE="schedule"; MANUAL="manual"; FORCED="forced_wake"

@dataclass class Rules: idle_hours, short_idle_minutes, min_uptime_minutes, max_starts_per_day, forced_wake_days,
                        forced_wake_minutes, wake_timeout_minutes, schedule_guard_minutes, queue_alert_minutes,
                        automatic: bool, dry_run: bool
@dataclass class Observation:
    now: datetime (aware, UTC); local_date: date  # for the per-day start counter
    power: PowerState; runners_ready: bool | None  # from guard status, None = unknown
    queued: int; running: int; github_ok: bool
    next_schedule_at: datetime | None   # next dispatch time of any enabled schedule
    prewake_due: bool                   # a schedule's pre-wake window has started
    oldest_queued_at: datetime | None
@dataclass class Action: kind: Literal["wake","shutdown","notify","event"]; reason: str; detail: dict
class PowerStateMachine:
    def __init__(self, rules: Rules, stored: dict | None = None)
    rules: Rules  (mutable at runtime; changes take effect on the next step)
    state: ServerState; idle_since: datetime | None; shutdown_at: datetime | None (property); last_wake_reason;
    starts_today: int; worked_since_wake: bool; keep_awake_until: datetime | None; last_on_at; last_off_at
    def step(self, obs: Observation) -> list[Action]
    def request_wake(self, now, reason=WakeReason.MANUAL) -> list[Action]
    def request_shutdown(self, now) -> list[Action]          # safe shutdown via guard, ignores idle timers but not the guard
    def keep_awake(self, now, until: datetime) -> None
    def on_shutdown_result(self, now, accepted: bool, reason: str | None) -> list[Action]
    def on_wake_result(self, now, ok: bool, error: str | None) -> list[Action]
    def as_dict(self) -> dict                                 # persisted in a Store, restored via `stored`
```
Rules (from plan §4/§6): idle timer starts when queued==running==0 and restarts on every job; long idle =
idle_hours after real work, short idle = short_idle_minutes when no job ran since the wake; never shut down when
automatic is off, keep_awake_until in the future, a schedule is due within schedule_guard_minutes, uptime <
min_uptime_minutes, github_ok is False, or power state unknown. Wake when automatic and (queued>0 or prewake_due or
forced wake due: off for >= forced_wake_days) — forced wake keeps the server up for forced_wake_minutes then uses
the short idle. Starts per local day are capped by max_starts_per_day; when reached, no more automatic shutdowns
until local midnight and a "notify" action ("start_limit"). STARTING -> WAKE_FAILED after wake_timeout_minutes
without runners_ready (notify, retry wake once per timeout). OFF observed while state is READY/ACTIVE and no shutdown
was requested -> notify "unexpected_off". Queued job older than queue_alert_minutes while server ON -> notify
"queue_stuck" (once per job burst). In dry_run, wake/shutdown actions are still returned but flagged
detail["dry_run"]=True and the manager only logs them; the state machine then assumes they succeeded.

## scheduler.py

```python
@dataclass class Schedule: id: str; name: str; repository: str; workflow: str; ref: str; inputs: dict;
                           time: time; weekdays: frozenset[int]; timezone: str; prewake: bool; prewake_minutes: int;
                           retries: int; enabled: bool; dispatch_token: str | None
def next_fire(schedule, after: datetime) -> datetime | None   # aware UTC; DST-correct via zoneinfo; nonexistent
    # local times (spring forward) fire at the first valid instant after; ambiguous times (fall back) fire once (fold=0)
def prewake_at(schedule, fire: datetime) -> datetime | None
@dataclass class DispatchResult: ok: bool; status: int | None; run_id: int | None; error: str | None; attempts: int; auth_failed: bool
async def dispatch_with_retry(client, schedule, *, fallback_token: str, sleep=asyncio.sleep,
                              backoff=(30, 60, 120, 240, 480)) -> DispatchResult
    # retry only GitHubTransientError (and rate limit with reset < 10 min); 401 -> auth_failed; 403/404/422 -> fail now
```
No catch-up: on start-up and after each fire the next fire is computed from "now".
