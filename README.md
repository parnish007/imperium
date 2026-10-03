# Imperium: a supervisor for AI coding agents

**Imperium is an open-source, local supervisor for AI coding agents such as Claude Code, OpenCode, Codex and Gemini
CLI.** It sits between the AI that plans the work (the *director*), the coding agents that do it (the *builders*),
and you (the *owner*). It makes sure no instruction is lost or silently sent twice, no event goes unread, no
permission prompt waits unseen, and no agent's "done" is accepted until checks the agent cannot edit have passed on
a snapshot of the code.

It runs on your own machine as one small Python service with SQLite: no cloud, no account, no dependencies outside
the standard library.

[![tests](https://github.com/parnish007/imperium/actions/workflows/tests.yml/badge.svg)](https://github.com/parnish007/imperium/actions/workflows/tests.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Licence: MIT](https://img.shields.io/badge/licence-MIT-green)
![Windows, Linux, macOS](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

## The problem it solves

Running AI coding agents for hours without watching them fails in ways that are easy to miss:

- a prompt is **sent twice** after a timeout, or **silently lost** in a client-side queue;
- the agent sits on a **permission prompt** nobody saw;
- the orchestrating session's monitor dies and **events are never read**;
- the agent says **"done"** when half the work is done, or deletes the failing test so the suite passes;
- a **sub-agent** is still working while the main agent looks idle, and the next prompt interrupts it.

Imperium makes each of these visible, recorded and recoverable.

## Features

- **Delivery with proof.** Every instruction ends in a recorded outcome. "Delivered" means Imperium found its own
  message id in the agent's history, never that an HTTP call returned 200. Uncertain cases wait for a decision;
  Imperium never resends on its own. Prompts go out only when the agent is truly idle (no pending permission, no
  question, no busy sub-agent).
- **Verified work, not claimed work.** Work is organised in *rounds*. An agent's "done" is a claim. It becomes
  *verified* only when trusted checks pass on a snapshot of the code, in a fresh copy outside the live workspace,
  with the checks' own files unchanged, and a check must fail without the change to count. It is *accepted* only by
  a named person or director.
- **Permission approvals with a human in the loop.** Your rules can answer an agent's permission prompts
  automatically, but only while the director is present, and every automatic answer is listed for you to review.
  Deny rules always win.
- **An event journal you can trust.** One append-only journal with a hash chain; each reader has its own feed and
  bookmark, and nothing is marked read before it was shown. Integrity failures stop everything that acts until you
  inspect them.
- **Liveness that understands sub-agents.** Working, waiting for approval, waiting for an answer, stalled, hung:
  reported, never killed behind your back.
- **Works with your agents.** OpenCode over its HTTP server, and any agent that speaks the
  [Agent Client Protocol](https://agentclientprotocol.com) (ACP), which Imperium starts and drives itself: OpenCode,
  Gemini CLI, and Claude Code or Codex through their ACP adapters.
- **Built for Claude Code as the director.** A generated Claude Code plugin with an MCP server, hooks and a
  playbook skill; any MCP client can use the director tools.
- **Isolation mode.** Run agents under their own operating-system account and let them reach Imperium only through
  a named pipe (Windows) or Unix socket (Linux, macOS) that checks who is at the other end.
- **Notifications, dashboard, backups.** A command of your choice runs for anything that needs you; a read-only
  browser dashboard; online backups and crash-safe restore.

## How it works

```
  Director (an AI session)        Owner (you)
  MCP tools · CLI · hooks         CLI · read-only dashboard · notifications
              \                     /
               v                   v
      imperiumd: one local service, the only writer
        local API on 127.0.0.1, bearer tokens (or OS-checked pipes in isolation mode)
        SQLite journal with an integrity chain
        delivery · rounds and checks · approvals · liveness
               |                        |
               v                        v
      OpenCode server (HTTP)     any ACP agent (stdio, started by Imperium)
```

The guarantees, and exactly what each one does *not* cover, are in the [specification](docs/SPEC.md).

## Install

Requires Python 3.11 or newer. [`uv`](https://docs.astral.sh/uv/) is the easiest way to get one.

```
git clone https://github.com/parnish007/imperium.git
cd imperium
uv tool install .        # or: pip install .
```

## Quick start

```
imperium init                      # ~/.imperium (or $IMPERIUM_HOME): database, config, owner token
imperium up                        # start the background service

# a builder: an OpenCode session (from `opencode serve`) ...
imperium builder add coding --endpoint http://127.0.0.1:<port> --session <ses_id> --directory <repo>
imperium builder mcp-config coding --write    # gives it Imperium's report tool; restart OpenCode while idle

# ... or any Agent Client Protocol agent, which Imperium starts itself
imperium builder add helper --acp "opencode acp" --directory <repo>

# a trusted check: run on a snapshot, never in the live workspace
imperium check add unit --builder coding --depends tests/test_calc.py --must-fail-on-base -- python -m pytest -q

# one round of work
imperium round open coding --objective "Make add() add; tests/test_calc.py must pass."
imperium round verify <round> --wait 600
imperium round objective <round> met
imperium round accept <round>      # refused unless verified and the workspace is unchanged
```

Use Claude Code as the director:

```
imperium plugin write ~/imperium-plugin       # skill, MCP server, hooks
claude --plugin-dir ~/imperium-plugin
imperium --as owner director claim            # once, inside that session
```

Everyday commands:

```
imperium status / events / ack <seq> / show <seq>      # what happened; your feed and bookmark
imperium send coding --message "..." --key k1           # a message outside rounds
imperium queue / msg show <id> / msg resolve <id> wait|cancel|resend --confirm-may-run-twice
imperium approvals / approve <id> / deny <id>           # permission prompts; --auto lists what your rules answered
imperium rule add bash "git status*" --allow            # owner rules, used only while the director is present
imperium questions / answer <id> --choice "A"
imperium dashboard                                      # read-only view in your browser
imperium stop-all / resume-all                          # the emergency brake and its release
imperium backup / restore <file> / verify-journal / export --out f.jsonl / doctor / down
```

Every command takes `--json`. Configuration lives in `~/.imperium/imperium.toml`; to be notified outside the feed,
set `[notify] command` (see the [specification](docs/SPEC.md), section 7.1).

## How a round is verified

1. When the round opens, its brief (objective, nonce, how to report, when to escalate) is queued, and the code is
   snapshotted just before it is sent.
2. The agent reports `ready` through its tool, quoting the round's nonce and generation. A claim is evidence, not
   acceptance.
3. `round verify` snapshots the code again. For each trusted check it refuses to trust the check if a file it
   depends on changed (only the owner approves new versions), runs it in a fresh copy with a minimal environment and
   a timeout, runs it on the starting snapshot when it must fail there, and records the executable and its hash, the
   environment, the exit code and the output.
4. A person or the director records whether the objective is met. Only then is the round verified.
5. Accept is refused if the workspace changed since the verified snapshot. A follow-up message starts a new
   generation and voids the verification.

## How it compares

| | Message delivery | What counts as "done" | Platforms |
|---|---|---|---|
| **Imperium** | durable queue; proof by the message's own id; uncertain outcomes wait for a decision | trusted checks on a code snapshot, then a named person's or director's acceptance | Windows, Linux, macOS |
| Agent Deck | durable at-most-once outbox, typed into tmux panes | a rule in the conductor's prompt, not enforced by the tool | macOS, Linux, WSL |
| Gas City | work items pulled from a store; fire-and-forget wake-up | the agent closes the work item | tmux platforms |
| Claude Squad | tmux sessions with worktrees | the human reviews the diff | tmux platforms |

Imperium can also sit *under* these tools: it supervises delivery and verification, not your workflow. Sources and
measured numbers are in [docs/benchmarks.md](docs/benchmarks.md).

## Measured

- 600 messages under random faults (dropped connections, servers that answer before saving, crashes right after a
  request): **0 lost, 0 silent duplicates**.
- Idle service: about **29 MB** of memory.
- Real rounds against OpenCode, over HTTP and over ACP: brief delivered, code fixed, checks passed on the change and
  failed without it, accepted.

Details and how to reproduce them: [docs/benchmarks.md](docs/benchmarks.md).

## Security model

- **Imperium is not a sandbox.** An agent running as your operating-system user can do anything you can, including
  reading Imperium's files. A prompt-injected agent is a realistic way for that to happen. For containment, run
  agents under another account ([isolation mode](docs/ISOLATION.md)), in a container or in a VM.
- Without isolation mode, "owner-only" holds against software that follows the protocol, not against a hostile
  process running as you.
- The local API listens on `127.0.0.1` only, checks `Host` and `Origin` (which stops web pages), and needs a
  256-bit bearer token; tokens are stored hashed.
- Nothing Imperium runs in an agent's repository executes the agent's git configuration (filters, diff drivers,
  hooks).
- The journal's hash chain detects corruption and naive edits; a process running as you could recompute it.

## FAQ

**What is Imperium?**
A local, open-source supervisor for AI coding agents. It guarantees that instructions to agents are delivered with
proof, that every event is recorded and read, and that an agent's claim of "done" is verified by checks the agent
cannot edit before anyone accepts it.

**Which coding agents does it work with?**
OpenCode (attached to a running `opencode serve` session) and any Agent Client Protocol agent: OpenCode, Gemini
CLI, and Claude Code or Codex through their ACP adapters. Live runs so far used OpenCode over both routes.

**Can Claude Code supervise other agents with it?**
Yes. `imperium plugin write` generates a Claude Code plugin with an MCP server, hooks and a playbook, so a Claude
Code session can send work, read events, answer approvals, and verify and accept rounds.

**How does it stop an AI agent from faking "tests pass"?**
Checks are defined outside the agent's reach and run on a snapshot of the code in a fresh copy. If a file a check
depends on changed, the check is not trusted until you approve it, and a check must fail on the code before the
change to count as testing it.

**Does it retry failed prompts automatically?**
No. A prompt whose fate is unknown becomes *uncertain* and waits for a decision (wait, cancel, or resend knowing it
may run twice). Silent retries are how instructions run twice.

**Does it need the cloud or an API key?**
No. It is one local Python service with SQLite and no third-party dependencies. Your agents use whatever models
they are configured with.

**Is it a sandbox?**
No. See the security model above and [isolation mode](docs/ISOLATION.md).

**Which operating systems?**
Windows, Linux and macOS; tested on all three with Python 3.11 and 3.13.

## Documentation

- [Specification](docs/SPEC.md): every guarantee, state and limit
- [Isolation mode](docs/ISOLATION.md): agents under their own account
- [Benchmarks](docs/benchmarks.md): measured numbers and the comparison sources

## Tests

The tests are part of the product: crash and fault cases, tampering, restore, idempotency, hostile
repositories, isolation channels, a fake OpenCode server with its known quirks and a fake ACP agent run as a real
process. They run on Windows, Linux and macOS.

```
PYTHONPATH=src python -m unittest discover -s tests
```

## Licence

MIT. See [LICENSE](LICENSE).
