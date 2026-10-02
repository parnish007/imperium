"""`imperium.toml`: schema-validated; unknown keys are rejected (DESIGN §12)."""
import os
import tomllib

from . import paths

SCHEMA = {
    "daemon": {
        "rate_per_sec": (float, 20.0),  # API requests per second per principal
        "burst": (int, 50),
        "audit_cap": (int, 100_000),  # rows kept in the call audit table
        "max_body": (int, 262_144),  # bytes
    },
    "opencode": {
        "poll_interval": (float, 2.0),  # seconds between polls of each builder
        "timeout": (float, 10.0),  # seconds per HTTP request to OpenCode
        "page_size": (int, 50),  # messages per history page
        "max_scan_pages": (int, 20),  # history pages searched per cycle (the search resumes next cycle)
        "unreachable_after": (int, 3),  # failed polls before BUILDER_UNREACHABLE
        "partless_polls": (int, 3),  # polls to wait for a user message's parts
    },
    "delivery": {
        "idle_stable_polls": (int, 2),  # consecutive idle polls before a builder may receive a message
        "reconcile_window": (float, 60.0),  # seconds to find a sent message before it is UNCERTAIN
        "admit_timeout": (float, 300.0),  # seconds a delivered message may wait unrun on an idle builder
        "stall_alert": (float, 600.0),  # seconds messages may wait on a blocked builder before an ACTION event
    },
    "approvals": {
        "lease_ttl": (float, 900.0),  # seconds a director's presence lasts after its last call (automatic answers)
    },
    "liveness": {
        "stall_after": (float, 600.0),  # seconds a WORKING builder may be silent before SUSPECTED_STALL
        "max_suppress": (float, 1800.0),  # seconds busy sub-agents may hide a silent parent before HANG_SUSPECTED
    },
    "resources": {
        "min_free_gb": (float, 3.0),  # free memory below this makes `imperium gate` say WAIT
    },
    "isolation": {
        # Isolation mode (Windows): accounts (SIDs) admitted to the owner pipe (owner and director) and to the
        # builder pipe. Empty: off. When on, the TCP port accepts only the read-only dashboard.
        "owner_accounts": (list, []),
        "builder_accounts": (list, []),
    },
    "notify": {
        # A command (argv list) run for every event at or above `floor`, with the event as JSON on stdin.
        "command": (list, []),
        "floor": (str, "ACTION"),  # ACTION or CRITICAL
        "timeout": (float, 30.0),
    },
    "integrity": {
        "verify_interval": (float, 300.0),  # seconds between journal chain checks while running
    },
    "redaction": {
        "env_names": (list, []),  # environment variables whose values are redacted from builder text
    },
}

DEFAULT_TEXT = """\
# Imperium configuration. Unknown keys are rejected; see DESIGN.md.

[daemon]
# Requests per second allowed per principal on the local API, and the burst above that rate.
rate_per_sec = 20.0
burst = 50
# Rows kept in the call audit table (oldest dropped first; drops are counted).
audit_cap = 100000
# Largest request body accepted, in bytes.
max_body = 262144

[opencode]
# Seconds between polls of each builder, and per request.
poll_interval = 2.0
timeout = 10.0
# Messages per history page, and pages searched per cycle when catching up.
page_size = 50
max_scan_pages = 20
# Failed polls before a builder is reported unreachable.
unreachable_after = 3
# Polls to wait for a user message whose content is not yet stored.
partless_polls = 3

[delivery]
# Consecutive idle polls before a builder may receive a message.
idle_stable_polls = 2
# Seconds to find a sent message in the builder's history before it is UNCERTAIN (never resent automatically).
reconcile_window = 60.0
# Seconds a delivered message may wait without running on an idle builder before it is STRANDED.
admit_timeout = 300.0
# Seconds messages may wait on a blocked builder before Imperium raises an ACTION event saying why.
stall_alert = 600.0

[approvals]
# Seconds the director counts as present after its last call. Automatic answers by the owner's rules happen only
# while it is present; otherwise a matching ask is HELD for a decision by hand.
lease_ttl = 900.0

[liveness]
# Seconds a working builder may be silent before SUSPECTED_STALL (report only; nothing is killed).
stall_after = 600.0
# Seconds busy sub-agents may hide a silent builder before HANG_SUSPECTED.
max_suppress = 1800.0

[resources]
# Free memory (GB) below which `imperium gate` says WAIT and messages sent with --needs-resources wait.
min_free_gb = 3.0

[integrity]
# Seconds between journal chain checks while running (a break quarantines Imperium).
verify_interval = 300.0

[redaction]
# Environment variables whose values are removed from builder text (values are never stored).
env_names = []
"""


class ConfigError(ValueError):
    pass


def defaults():
    return {sec: {k: (list(v[1]) if isinstance(v[1], list) else v[1]) for k, v in keys.items()}
            for sec, keys in SCHEMA.items()}


def load(home):
    cfg = defaults()
    path = paths.config(home)
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    problems = []
    for sec, values in data.items():
        if sec not in SCHEMA:
            problems.append(f"unknown section [{sec}]")
            continue
        if not isinstance(values, dict):
            problems.append(f"[{sec}] must be a table")
            continue
        for k, v in values.items():
            if k not in SCHEMA[sec]:
                problems.append(f"unknown key {sec}.{k}")
                continue
            typ = SCHEMA[sec][k][0]
            if typ is list:
                if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                    problems.append(f"{sec}.{k} must be a list of names")
                else:
                    cfg[sec][k] = v
                continue
            if typ is float and isinstance(v, int) and not isinstance(v, bool):
                v = float(v)
            if not isinstance(v, typ) or isinstance(v, bool) or v <= 0:
                problems.append(f"{sec}.{k} must be a positive {typ.__name__}")
                continue
            cfg[sec][k] = v
    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))
    return cfg
