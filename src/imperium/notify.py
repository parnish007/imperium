"""Owner notifications: run a command the owner configured for every event at or above a floor.

The notifier is an ordinary feed consumer (`system:notify:<FLOOR>`), so it has the feed guarantee: every event is
shown at least once, in order, across restarts; an event is acknowledged only after the command exits 0. A failing
command is retried with backoff and reported once per failure streak (NOTIFY_FAILED, a NOTICE, which is below any
floor the notifier accepts, so a failing notifier cannot feed itself). A new notifier starts at the current head: it
does not replay history.

The command receives one event as JSON on stdin and in IMPERIUM_EVENT_* variables. Builder-written text
(`untrusted`) is never passed: a notification must not carry a builder's words to a program that may act on them.
"""
import json
import logging
import os
import subprocess
import threading

from . import feeds, journal, verify

log = logging.getLogger("imperium.notify")
PRINCIPAL = "system:notify"
FLOORS = ("ACTION", "CRITICAL")


def consumer_name(floor):
    return f"{PRINCIPAL}:{floor}"


def payload(e):
    return {"seq": e["seq"], "ts": e["ts"], "type": e["type"], "severity": e["severity"], "builder": e["builder"],
            "headline": e["headline"], "data": e["data"]}


def run_command(argv, event, timeout):
    """Run the owner's command for one event; returns (ok, detail)."""
    body = json.dumps(payload(event), sort_keys=True)
    env = dict(os.environ)
    env.update({"IMPERIUM_EVENT_SEQ": str(event["seq"]), "IMPERIUM_EVENT_TYPE": event["type"],
                "IMPERIUM_EVENT_SEVERITY": event["severity"], "IMPERIUM_EVENT_BUILDER": event["builder"] or "",
                "IMPERIUM_EVENT_HEADLINE": event["headline"]})
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
                             **kw)
    except OSError as e:
        return False, f"cannot start: {e}"
    try:
        _, err = p.communicate(body.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        verify._kill_tree(p)
        p.wait()
        return False, f"timed out after {timeout} s"
    if p.returncode != 0:
        return False, f"exit {p.returncode}: {err.decode('utf-8', 'replace').strip()[-200:]}"
    return True, "ok"


class Notifier:
    def __init__(self, daemon):
        cfg = daemon.cfg["notify"]
        self.d = daemon
        self.argv = [str(x) for x in cfg["command"]]
        self.floor = cfg["floor"].upper()
        self.timeout = cfg["timeout"]
        self.name = consumer_name(self.floor)
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.failures = 0

    @property
    def enabled(self):
        return bool(self.argv)

    def start(self):
        if not self.enabled:
            return
        if self.floor not in FLOORS:
            raise ValueError(f"[notify] floor must be one of {', '.join(FLOORS)}")
        with self.d.store.tx() as conn:
            if conn.execute("SELECT 1 FROM consumers WHERE name=?", (self.name,)).fetchone() is None:
                feeds.create(conn, self.name, principal=PRINCIPAL, floor=self.floor)
                head = journal.head(conn)[0]
                conn.execute("UPDATE consumers SET acked_seq=?, shown_through=? WHERE name=?", (head, head, self.name))
        self.thread = threading.Thread(target=self._loop, name="imperium-notify", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=self.timeout + 5)

    def _loop(self):
        while not self.stop_event.is_set():
            delay = 2.0
            try:
                delay = self.cycle()
            except Exception:  # never let the notifier take the daemon down
                log.exception("notifier cycle failed")
                delay = 30.0
            self.wake.wait(delay)
            self.wake.clear()

    def cycle(self):
        """Deliver pending events in order; returns how long to wait before the next cycle."""
        if self.d.store is None or self.d.store.failed:
            return 30.0
        with self.d.store.tx() as conn:
            page = feeds.events_since(conn, self.name, PRINCIPAL, limit=20)
        for e in page["events"]:
            if self.stop_event.is_set():
                return 0
            ok, detail = run_command(self.argv, e, self.timeout)
            if not ok:
                self.failures += 1
                if self.failures == 1:
                    with self.d.store.tx() as conn:
                        journal.append(conn, "NOTIFY_FAILED", "NOTICE", data={"event": e["seq"], "detail": detail})
                log.warning("notification for event %s failed (%s); attempt %s", e["seq"], detail, self.failures)
                return min(300.0, 2.0 * 2 ** min(self.failures, 8))
            if self.failures:
                with self.d.store.tx() as conn:
                    journal.append(conn, "NOTIFY_RECOVERED", "NOTICE", data={"event": e["seq"],
                                                                            "attempts": self.failures + 1})
                self.failures = 0
            with self.d.store.tx() as conn:
                feeds.ack(conn, self.name, PRINCIPAL, e["seq"])
        return 0.2 if page["more"] else 2.0
