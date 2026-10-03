"""Liveness and the resource gate (DESIGN §10.1, §10.2). Report first: Imperium never kills anything on its own.

Each builder has one operational state, derived from what the reader saw:
  UNREACHABLE, PAUSED, WAITING_APPROVAL, WAITING_QUESTION, WAITING_PROVIDER, WORKING, SUSPECTED_STALL, IDLE.
Stall rules apply only while WORKING: a builder waiting on an approval, a question or its model provider is
reported as waiting, never as stalled. Progress is new journal activity from the builder or a reply that keeps
growing. While a sub-agent session is busy, silence in the parent is normal and the stall alarm is suppressed, but
only for `max_suppress`; after that it is HANG_SUSPECTED ("active but no progress"), so a busy loop is still caught.
"""
import ctypes
import os
import sys

WORKING, IDLE = "WORKING", "IDLE"
STATES = ("UNREACHABLE", "PAUSED", "WAITING_APPROVAL", "WAITING_QUESTION", "WAITING_PROVIDER", WORKING,
          "SUSPECTED_STALL", IDLE)


def operational_state(cp, health, builder, stop_all):
    if health.get("reachable") is False:
        return "UNREACHABLE"
    if stop_all or (builder or {}).get("paused"):
        return "PAUSED"
    if cp.get("permissions") is None or cp.get("permissions"):
        return "WAITING_APPROVAL"
    if cp.get("questions"):
        return "WAITING_QUESTION"
    if cp.get("status") == "retry":
        return "WAITING_PROVIDER"
    if cp.get("status") == "busy" or cp.get("open") or cp.get("busy_children"):
        return WORKING
    return IDLE


def progress_fingerprint(cp):
    """Changes whenever the builder visibly moves: a new message, or a reply still being written grows."""
    return [cp.get("last_id"), cp.get("progress_seq"), sorted((k, v.get("size")) for k, v in (cp.get("open") or {}).items()),
            sorted(cp.get("busy_children") or [])]


def check_stall(h, state, cp, now, stall_after, max_suppress):
    """Update the builder's stall tracking; return (event_type, data) when an alarm should be raised, else None.
    Each alarm is raised once per episode."""
    fp = progress_fingerprint(cp)
    if state != WORKING:
        h.pop("stall", None)
        h["progress_fp"], h["progress_at"] = fp, now
        return None
    if fp != h.get("progress_fp"):
        h["progress_fp"], h["progress_at"] = fp, now
        h.pop("stall", None)
        return None
    quiet = now - h.get("progress_at", now)
    children = bool(cp.get("busy_children"))
    if children:
        if quiet >= max_suppress and h.get("stall") != "hang":
            h["stall"] = "hang"
            return "HANG_SUSPECTED", {"quiet_s": round(quiet), "busy_subagents": cp.get("busy_children"),
                                      "note": "sub-agents are busy but nothing has progressed; look before killing"}
        return None
    if quiet >= stall_after and h.get("stall") is None:
        h["stall"] = "stall"
        return "SUSPECTED_STALL", {"quiet_s": round(quiet), "status": cp.get("status"),
                                   "note": "working but silent; it may be thinking, or stuck"}
    return None


# --- resources ---------------------------------------------------------------------------------------------

def free_memory_gb():
    """Free (available) physical memory in GB, or None when it cannot be read on this platform."""
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return st.ullAvailPhys / 2 ** 30
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 2 ** 20
            return None
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = 4096
            free = 0
            for line in out.splitlines():
                if "page size of" in line:
                    page = int(line.split("page size of")[1].split()[0])
                for key in ("Pages free:", "Pages inactive:", "Pages speculative:"):
                    if line.startswith(key):
                        free += int(line.split(":")[1].strip().rstrip("."))
            return free * page / 2 ** 30
    except (OSError, ValueError, AttributeError):
        return None
    return None


def gate(min_free_gb, max_load=None):
    """('OK' | 'WAIT', reason, measurements). Unknown memory is reported, not treated as a pass."""
    free = free_memory_gb()
    m = {"free_gb": None if free is None else round(free, 2), "min_free_gb": min_free_gb}
    if free is None:
        return "WAIT", "free memory cannot be measured on this platform", m
    if free < min_free_gb:
        return "WAIT", f"free memory {free:.1f} GB is below {min_free_gb:.1f} GB", m
    if max_load and hasattr(os, "getloadavg"):
        load = os.getloadavg()[0]
        m["load"] = round(load, 2)
        if load > max_load:
            return "WAIT", f"load {load:.1f} is above {max_load:.1f}", m
    return "OK", "resources available", m
