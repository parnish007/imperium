# Benchmarks

Only measured numbers are published here, each with how to reproduce it. Nothing below compares Imperium's
numbers with another tool's: the other tools were not run (see the end of this page for why), so the comparison is
a feature table with sources, not a measurement.

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
In service, add up to one poll interval (2 s by default, `[opencode] poll_interval`).

## Footprint

The idle service on the same machine: 28.8 MB working set, 16.7 MB private memory (Windows, Python 3.13, one
process). For comparison on the same machine, one long-running `opencode serve` used 554 MB working set.

## A real round

One round against a real OpenCode 1.18.32 server (`opencode serve --pure`, a scratch repository with a broken
`add()` and its test, the builder using a free model): brief queued, delivered and admitted by id, the builder
fixed the code, ran the test, and reported through Imperium's MCP tool; verification passed on the candidate and
failed on the base snapshot (the check discriminates); objective recorded; accepted. 50 seconds from opening to
acceptance. OpenCode kept the message id Imperium chose. This is one run, not a rate.

## The test suite

Unit, integration and fault tests, including mutation checks: for each guarantee a deliberate bug was put back
and a test had to fail. Stage 3: 27 single and 5 paired mutations, all caught. Stage 4: 25, 24 caught (the
survivor is guarded twice; removing the second guard is caught). Stages 5-9: 27; 22 caught at first, 24 after two tests were
added; each of the 3 survivors has a second guard that still holds (for the presence lease, removing that second guard is caught).

## Other tools: features, from their source or documentation

Not measured. They were read, not run: they need tmux (not native on Windows) or a desktop install, and running
them on this machine needs the owner's approval for each install.

| | Message delivery | What counts as done | Platforms | Source read |
|---|---|---|---|---|
| **Imperium** | durable outbox; proof by the message's own id; uncertain outcomes wait for a decision | trusted checks on a code snapshot, then a named principal's acceptance | Windows, Linux, macOS | — |
| Agent Deck | durable at-most-once outbox, reconciled against the transcript; typed into tmux panes | a rule in the conductor's prompt (`GATES`, `VERDICT.md`), not enforced by the tool | macOS, Linux, WSL | source |
| Gas City | work items pulled from a store; the wake-up nudge is fire-and-forget | the agent closes the work item | tmux platforms | source |
| Agent Orchestrator | workers in their own branch and worktree, ~25 agent adapters | pull request, CI and review facts | macOS, Windows, Linux | README, directory listing |
| Beads | not a supervisor: a work graph | `bd close` by the agent | many | README |
| Claude Squad | tmux sessions with worktrees | the human reviews the diff | tmux platforms | README |
