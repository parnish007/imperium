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
