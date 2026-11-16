"""Constants for the Homerunner integration (single source of truth for names)."""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "homerunner"

# config entry data
CONF_GITHUB_TOKEN = "github_token"
CONF_API_BASE = "api_base"
CONF_OWNER = "owner"
CONF_EXCLUDED_REPOS = "excluded_repos"
CONF_RUNNER_LABELS = "runner_labels"
CONF_BMC_HOST = "bmc_host"
CONF_BMC_USERNAME = "bmc_username"
CONF_BMC_PASSWORD = "bmc_password"
CONF_BMC_VERIFY_SSL = "bmc_verify_ssl"
CONF_WOL_MAC = "wol_mac"
CONF_SSH_HOST = "ssh_host"
CONF_SSH_PORT = "ssh_port"
CONF_SSH_USERNAME = "ssh_username"
CONF_SSH_KEY = "ssh_private_key"
CONF_SSH_KNOWN_HOST_KEY = "ssh_host_key"

# options (rules)
CONF_IDLE_HOURS = "idle_hours"
CONF_SHORT_IDLE_MINUTES = "short_idle_minutes"
CONF_MIN_UPTIME_MINUTES = "min_uptime_minutes"
CONF_MAX_STARTS_PER_DAY = "max_starts_per_day"
CONF_FORCED_WAKE_DAYS = "forced_wake_days"
CONF_FORCED_WAKE_MINUTES = "forced_wake_minutes"
CONF_WAKE_TIMEOUT_MINUTES = "wake_timeout_minutes"
CONF_SCHEDULE_GUARD_MINUTES = "schedule_guard_minutes"
CONF_QUEUE_ALERT_MINUTES = "queue_alert_minutes"
CONF_AUTOMATIC = "automatic"
CONF_DRY_RUN = "dry_run"

# webhook (options)
CONF_USE_WEBHOOK = "use_webhook"
CONF_WEBHOOK_ID = "webhook_id"
CONF_WEBHOOK_SECRET = "webhook_secret"

# schedule subentry (subentry_type "schedule")
CONF_REPOSITORY = "repository"
CONF_WORKFLOW = "workflow"
CONF_REF = "ref"
CONF_INPUTS = "inputs"
CONF_TIME = "time"
CONF_WEEKDAYS = "weekdays"
CONF_TIMEZONE = "timezone"
CONF_PREWAKE = "prewake"
CONF_PREWAKE_MINUTES = "prewake_minutes"
CONF_RETRIES = "retries"
CONF_DISPATCH_TOKEN = "dispatch_token"
CONF_NAME = "name"
CONF_ENABLED = "enabled"
SUBENTRY_SCHEDULE = "schedule"

# services and events
SERVICE_WAKE = "wake"
SERVICE_SHUTDOWN = "shutdown"
SERVICE_KEEP_AWAKE = "keep_awake"
SERVICE_DISPATCH = "dispatch"
ATTR_CONFIG_ENTRY = "config_entry"
ATTR_FORCE = "force"
ATTR_DURATION = "duration"
ATTR_SCHEDULE = "schedule"
EVENT_DISPATCHED = f"{DOMAIN}_dispatched"
EVENT_DISPATCH_FAILED = f"{DOMAIN}_dispatch_failed"

# defaults
DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_RUNNER_LABELS: list[str] = ["self-hosted"]
DEFAULT_BMC_VERIFY_SSL = False
DEFAULT_SSH_PORT = 22
DEFAULT_IDLE_HOURS = 2.0
DEFAULT_SHORT_IDLE_MINUTES = 20
DEFAULT_MIN_UPTIME_MINUTES = 15
DEFAULT_MAX_STARTS_PER_DAY = 8
DEFAULT_FORCED_WAKE_DAYS = 5
DEFAULT_FORCED_WAKE_MINUTES = 30
DEFAULT_WAKE_TIMEOUT_MINUTES = 10
DEFAULT_SCHEDULE_GUARD_MINUTES = 30
DEFAULT_QUEUE_ALERT_MINUTES = 15
DEFAULT_AUTOMATIC = False
DEFAULT_DRY_RUN = True
DEFAULT_REF = "main"
DEFAULT_WEEKDAYS: list[int] = [0, 1, 2, 3, 4, 5, 6]
DEFAULT_PREWAKE = True
DEFAULT_PREWAKE_MINUTES = 15
DEFAULT_RETRIES = 5
DEFAULT_KEEP_AWAKE_HOURS = 2


class PowerState(StrEnum):
    """Power state of the server as reported by the BMC."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class ServerState(StrEnum):
    """State of the power state machine."""

    OFF = "off"
    STARTING = "starting"
    READY = "ready"
    ACTIVE = "active"
    SHUTTING_DOWN = "shutting_down"
    WAKE_FAILED = "wake_failed"
    UNKNOWN = "unknown"


class WakeReason(StrEnum):
    """Why the server is (being) woken."""

    QUEUED_JOB = "queued_job"
    SCHEDULE = "schedule"
    MANUAL = "manual"
    FORCED = "forced_wake"
