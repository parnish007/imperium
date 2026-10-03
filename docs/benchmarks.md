# Benchmarks

These are historical measurements from the original implementation, not a measurement of the revised concurrent
scheduler or container verification runner. They have not been rerun for this change. The fake-server harness
measures delivery accounting, not coding success, reliability across real tasks, or comparative productivity.
See [VALIDATION.md](VALIDATION.md) for current regression coverage and the real-task evaluation still required.

## Delivery under random faults

`bench/delivery.py` sends messages to a fake OpenCode server (it reproduces OpenCode 1.18.32's HTTP behaviour and
its known bugs) and draws one fault per message at random: none (3 in 10), HTTP 500 before the save, HTTP 500
after the save, a dropped connection after the save, a 204 answer that never saves (OpenCode answers before
saving), a save that is never run (OpenCode issue #46842), a busy builder, or a crash of Imperium right after the
request. Pending decisions are then taken the way a careful director would: an UNCERTAIN message is resent with
confirmation, a STRANDED one is cancelled (and the fake then runs it late, as OpenCode can).

Counted: **lost** (never reached an outcome) and **silent duplicates** (run more than once without a
`DUPLICATE_RAN` event naming it). Both must be 0.

| Run | Messages | Lost | Silent duplicates | Ran | Ran after a confirmed resend | Cancelled, then ran late (reported) | Time |
|---|---|---|---|---|---|---|---|
| seed 1 | 300 | **0** | **0** | 208 | 64 | 28 | 90 s |
| seed 2 | 300 | **0** | **0** | 219 | 51 | 30 | 86 s |

Per fault, every message under "500 after save", "dropped after save", "busy" and "crash after the request" ran
exactly once with no resend: Imperium found its own message id instead of posting again. Every message under "500
before save" and "204 never saved" became UNCERTAIN (never resent automatically) and ran once after the confirmed
resend.

Reproduce: `PYTHONPATH=src:tests python bench/delivery.py --messages 300 --seed 1` (on Windows use `;` in
`PYTHONPATH`). Machine: Windows 11 (10.0.26200), Python 3.13.15, Imperium 0.1.0.dev0, 2026-10-02.

## Time from a permission ask to the director's feed

Measured in the same harness (20 samples per run, two runs): median 31 ms and 64 ms, maximum 95 ms, for one poll cycle plus one feed read.
In service, latency also includes adapter requests and worker availability. A poll interval alone is not an upper bound.

## Footprint

The idle service on the same machine: 28.8 MB working set, 16.7 MB private memory (Windows, Python 3.13, one
process). For comparison on the same machine, one long-running `opencode serve` used 554 MB working set.

## A real round

One round against a real OpenCode 1.18.32 server (`opencode serve --pure`, a scratch repository with a broken
`add()` and its test, the builder using a free model): brief queued, delivered and admitted by id, the builder
fixed the code, ran the test, and reported through Imperium's MCP tool; verification passed on the candidate and
failed on the base snapshot (the check discriminates); objective recorded; accepted. 50 seconds from opening to
acceptance. OpenCode kept the message id Imperium chose. This is one run, not a rate.

The same round through the Agent Client Protocol adapter: Imperium started `opencode acp --pure` itself (with its
own data and config folders), opened a session with the builder's tool, and sent the brief; the agent fixed the
code and reported through the tool; checks passed on the candidate and failed on the base; accepted 30 seconds
after opening. Stopping Imperium stopped the agent. One run.

## The test suite

Unit, integration and fault tests, including mutation checks: for each guarantee a deliberate bug was put back
and a test had to fail. Of 120 such bugs, 115 were caught; each of the other 5 is blocked by a second guard that
still holds (for example, a check repeated at the API route). Where a mutation first survived, a test was added.
The suite runs on Windows, Linux and macOS with Python 3.11 and 3.13.

## Comparative evidence

No competing supervisor was run in these measurements. There is no evidence here for a comparative productivity
or reliability advantage. Feature comparisons require versioned primary sources and a shared evaluation setup;
the previous unversioned comparison table has been removed.
