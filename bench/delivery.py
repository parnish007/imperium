"""Delivery under random faults, against the fake OpenCode server (SYSTEM-DESIGN §11).

For each message a fault is drawn at random (none, HTTP 500 before or after the save, a dropped connection after
the save, a 204 that never saves, a save that never runs, a busy builder, a daemon crash right after the POST).
The run then settles: pending decisions are taken the way a careful director would (UNCERTAIN: wait out the
window, then resend with confirmation; STRANDED: cancel, then let it run).

Counted per message: its outcome class, and how many times the builder actually ran it.
  lost              never reached an outcome (still queued or in flight at the end)        must be 0
  silent duplicate  ran more than once with no DUPLICATE_RAN event naming it              must be 0
Also measured: time from a permission ask appearing to it being in the director's feed.

Run:  PYTHONPATH=src:tests python bench/delivery.py --messages 200 --seed 1
"""
import argparse
import collections
import json
import os
import platform
import random
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
from fake_opencode import FakeOpenCode  # noqa: E402
from helpers import TempHome  # noqa: E402
from imperium import __version__, cli, opencode  # noqa: E402

SID, DIR = "ses_bench", "/work/bench"
FAULTS = ["none", "none", "none", "error_before_persist", "error_after_persist", "drop_after_persist", "fail_async",
          "drop_prompt", "busy", "crash_after_post"]
CONFIG = ("[daemon]\nrate_per_sec = 100000.0\nburst = 100000\n[opencode]\npoll_interval = 3600.0\n"
          "[delivery]\nidle_stable_polls = 1\nreconcile_window = 60.0\nadmit_timeout = 120.0\nstall_alert = 1e9\n")


class Bench:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.fake = FakeOpenCode()
        self.fake.add_session(SID, DIR)
        self.h = TempHome(CONFIG).init().start()
        self.now = 1_000_000.0
        self.h.d.engine.clock = lambda: self.now
        cli.main(["--home", self.h.home, "--json", "builder", "add", "coding", "--endpoint", self.fake.url,
                  "--session", SID, "--directory", DIR], out=_Null(), err=_Null(), env={})
        self.c = self.h.client()
        self.cycle()

    def cycle(self, n=1):
        for _ in range(n):
            self.h.d.engine.run_once()

    def msg(self, mid):
        return self.c.call("GET", f"/v1/message?id={mid}")["message"]

    def restart(self):
        self.h.stop()
        self.h.start()
        self.h.d.engine.clock = lambda: self.now
        self.c = self.h.client()

    def one(self, i):
        fault = self.rng.choice(FAULTS)
        m = self.c.call("POST", "/v1/send", {"builder": "coding", "body": f"task {i}", "client_key": f"b{i}"})["message"]
        ids = [m["id"]]
        if fault == "busy":
            self.fake.set_status(SID, "busy")
            self.cycle(2)
            self.fake.set_status(SID, "idle")
        elif fault == "crash_after_post":
            real = opencode.OpenCodeClient.prompt_async

            def post_then_die(self_, *a, **kw):
                real(self_, *a, **kw)
                raise SystemExit("crash")
            with mock.patch.object(opencode.OpenCodeClient, "prompt_async", post_then_die):
                try:
                    self.cycle()
                except SystemExit:
                    pass
            self.restart()
        elif fault != "none":
            self.fake.quirks.add(fault)
            self.cycle()
            self.fake.quirks.discard(fault)
        for _ in range(4):
            self.cycle()
        # settle the way a careful director would
        for _ in range(6):
            cur = self.msg(ids[-1])
            if cur["state"] in ("POSTED", "UNKNOWN", "DELIVERED"):
                self.now += 130
                self.cycle(2)
                continue
            if cur["state"] == "UNCERTAIN":
                r = self.c.call("POST", "/v1/message/resolve", {"id": cur["id"], "choice": "resend",
                                                                "confirm_may_run_twice": True})
                ids.append(r["resent_as"])
                self.cycle(4)
                continue
            if cur["state"] == "STRANDED":
                self.c.call("POST", "/v1/message/resolve", {"id": cur["id"], "choice": "cancel"})
                self.fake.run_pending(SID)  # the stranded copy runs late anyway
                self.cycle(3)
                continue
            break
        return fault, ids

    def report(self, results):
        with self.h.d.store.read() as conn:
            dup_named = set()
            for (data,) in conn.execute("SELECT data FROM events WHERE type='DUPLICATE_RAN'"):
                d = json.loads(data)
                dup_named |= {d["original"], d["resent_as"]}
        out = collections.Counter()
        by_fault = collections.defaultdict(collections.Counter)
        lost = silent = 0
        for fault, ids in results:
            msgs = [self.msg(x) for x in ids]
            final = msgs[-1]["state"]
            runs = sum(self.fake.runs.get(m["oc_message_id"] or "", 0) for m in msgs)
            if final in ("QUEUED", "DISPATCHING", "POSTED", "UNKNOWN"):
                lost += 1
            if runs > 1 and not any(m["id"] in dup_named for m in msgs):
                silent += 1
            outcome = {"ADMITTED": "ran", "DELIVERED": "delivered, not yet run", "UNCERTAIN": "uncertain",
                       "STRANDED": "stranded", "CANCELLED": "cancelled", "REJECTED": "rejected"}.get(final, final)
            if final == "CANCELLED" and runs:
                outcome = "cancelled, then ran late (LATE_ADMISSION reported)"
            if len(ids) > 1:
                outcome += " after resend"
            if runs > 1:
                outcome += f" (ran {runs}x, reported)"
            out[outcome] += 1
            by_fault[fault][outcome] += 1
        return {"messages": len(results), "lost": lost, "silent_duplicates": silent, "outcomes": dict(out),
                "by_fault": {k: dict(v) for k, v in by_fault.items()}}

    def ask_latency(self, n=20):
        lat = []
        for _ in range(n):
            self.fake.permissions.clear()
            self.cycle()
            t0 = time.perf_counter()
            p = self.fake.add_permission(SID)
            self.cycle()
            r = self.c.call("POST", "/v1/events_since", {"consumer": "owner", "limit": 500})
            assert any(e["data"].get("permission_id") == p["id"] for e in r["events"])
            lat.append(time.perf_counter() - t0)
            self.c.call("POST", "/v1/ack", {"consumer": "owner", "seq": r["events"][-1]["seq"]})
        lat.sort()
        return {"samples": n, "median_ms": round(1000 * lat[n // 2], 1), "max_ms": round(1000 * lat[-1], 1),
                "note": "one poll cycle plus a feed read; in service add up to one poll interval (2 s default)"}

    def close(self):
        self.h.cleanup()
        self.fake.close()


class _Null:
    def write(self, s):
        return len(s)

    def flush(self):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    b = Bench(a.seed)
    t0 = time.perf_counter()
    try:
        results = [b.one(i) for i in range(a.messages)]
        rep = b.report(results)
        rep["ask_to_feed"] = b.ask_latency()
    finally:
        b.close()
    rep.update({"seconds": round(time.perf_counter() - t0, 1), "seed": a.seed, "imperium": __version__,
                "python": platform.python_version(), "platform": platform.platform()})
    print(json.dumps(rep, indent=2))
    return 0 if rep["lost"] == 0 and rep["silent_duplicates"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
