# Imperium

**Imperium sits between an AI *director* and the AI *builders* it supervises, and makes sure nothing between them is lost, duplicated or taken on trust.**

A director is an AI session that plans and reviews work (for example a Claude Code session). A builder is a coding agent that does the work (OpenCode first). A human *owner* stays in charge of both.

> **Status: pre-alpha.** Stages 1-3 of 9 are built: the store, the event journal, the reading feeds, the background service, the command line, *watching* OpenCode builders (everything a builder does becomes an event), and *delivering* messages to them through a durable outbox. Rounds and claim verification, approvals, the MCP server and the dashboard are designed but **not built yet**. Do not rely on it for real work.

## Why

Running coding agents unattended for hours fails in ways that are easy to miss:

- a prompt is sent twice after a timeout, or silently lost in a client-side queue;
- the builder sits on a permission prompt nobody saw;
- the director's monitor dies and events are never read;
- the builder says "done" when half the work is done, or deletes the failing test;
- a sub-agent is still working while the main agent looks idle.

Imperium's job is to make each of these visible and recoverable.

## What it does (design goals)

1. **Never turns uncertainty into success or into a silent retry.** Every prompt ends in a recorded outcome: not sent, delivered (proven by finding Imperium's own message in the builder's history), delivery uncertain, duplicate detected, or resent by an explicit decision. Against a builder that cannot deduplicate, no outside tool can promise exactly-once delivery; Imperium's promise is that every exception is visible and decided by someone, never guessed. Prompts go out only when the builder is ready (idle, nothing pending, no sub-agent working).
2. **Records every state change and builder event** in one append-only journal with an integrity chain. (API-call telemetry is kept separately, with bounded retention.) Each reader has its own feed and bookmark, and the bookmark cannot be moved past what the reader was actually shown.
3. **Tracks rounds of work.** A builder's "done" is a *claim*. It becomes *verified* only when configured checks pass on a snapshot of the code, and *accepted* only by the director or the owner.
4. **Keeps the owner in control.** Trust-changing actions need the owner's credential, there is a global stop, and nothing is approved "always" on the owner's behalf. (Read the security model: on one OS user this is a protocol, not a wall.)
5. **Runs on one modest machine**: one small Python service, SQLite, no cloud.

## How it works

```
  Director (AI)            Owner
  MCP · CLI · hooks        CLI · dashboard (read-only)
          \                  /
           v                v
   imperiumd: one local service, the only writer
     local API on 127.0.0.1 with bearer tokens
     SQLite (WAL) journal with an integrity chain
     dispatcher · rounds · approvals · liveness
           |
           v
   builder: OpenCode server (HTTP)
```

- **One writer.** Only the service writes the database. A state change, its event and its checkpoint commit together.
- **Fails closed.** If a write fails, the service stops dispatching and answering, and says so in every reply. If the journal's integrity chain is broken at start-up, Imperium goes into **quarantine**: reading and backups still work, but nothing that dispatches, decides or changes trust runs until the owner inspects it and releases it with a recorded reason.
- **Feeds.** Each reader (director, owner) has a feed with a fixed minimum severity. Reading is strictly in order. A drain stops at a high-water mark, so it always ends. Acknowledging beyond what was shown is refused.
- **Retention.** Only the oldest part of the journal can be pruned, and it is archived first, by a crash-safe protocol. The integrity chain still checks from the prune boundary.
- **Backups** use SQLite's online backup API. After a restore, the service starts in observe-only mode until the owner confirms.

## Security model: read this

- **Imperium is not a sandbox.** A builder running as your OS user can do anything you can, including reading Imperium's files and acting as the director. A builder hijacked by a malicious web page (prompt injection) is a realistic way for that to happen. If you need containment, run builders as another OS user, in a container or in a VM.
- **"Owner-only" is enforced against software that follows the protocol, not against a hostile process.** The owner's token is a file in your runtime folder; any program running as your OS user, including a builder, can read it and act as the owner. A real boundary needs the builder under another OS user, in a container or a VM. A mode where the service runs under its own identity and is reached through an OS-permissioned pipe or socket is planned.
- The local API listens on `127.0.0.1` only, checks the `Host` and `Origin` headers (these stop web pages, not local programs), and needs a 256-bit bearer token. Tokens are stored hashed; the runtime folder is readable only by your user.
- The journal's hash chain detects accidental corruption and naive edits. A process running as the same user could recompute it, so it is an integrity check, not proof.

## Install (from source, for now)

Requires Python 3.11 or newer. [`uv`](https://docs.astral.sh/uv/) is the easiest way to get one.

```
git clone https://github.com/parnish007/imperium.git
cd imperium
uv tool install .        # or: pip install .
```

## Use what exists today

```
imperium init            # create ~/.imperium (or $IMPERIUM_HOME): database, config, owner token
imperium up              # start the background service (idempotent)
imperium status          # service, journal and feed status; "needs you" count
imperium events          # read your feed up to its high-water mark
imperium ack <seq>       # mark it read, only as far as you were shown
imperium show <seq>      # look at one event without moving anything
imperium verify-journal  # check the integrity chain (works with the service stopped)
imperium backup          # online backup; `imperium restore <file>` with the service stopped
imperium doctor          # check the installation (`--contract` prints a setup report)
imperium down            # stop the service

imperium builder add coding --endpoint http://127.0.0.1:<port> --session <ses_id> \n    --directory <workspace> --password-env OPENCODE_SERVER_PASSWORD   # watch an OpenCode session
imperium builder list   # registered builders; `imperium status` shows reachability and why nothing is sent

imperium send coding --message "..." --key fix-42   # queue a message; the same key and text never send twice
imperium queue coding    # what is waiting or in flight
imperium msg show <id>   # one message and its state
imperium msg resolve <id> wait|cancel|resend [--confirm-may-run-twice]   # decide on an UNCERTAIN/STRANDED one
imperium stop-all        # send nothing to anyone (`imperium resume-all`, owner, to undo)
imperium builder pause coding                 # owner: only the owner's messages go to this builder
imperium builder allow-version coding 1.19.0  # owner: accept an OpenCode version Imperium was not tested on

# inside the Claude Code session that will direct (the owner authorises it):
imperium --as owner director claim
imperium status         # now acts as the director, with its own feed

imperium quarantine release --reason "..."   # owner, after inspecting a broken journal
```

Once a builder is registered, the service polls it and journals what happens: turns starting and ending, tool errors, permission asks, questions, retries, compactions, and any message that Imperium did not send (shown to you as CRITICAL). Builder text is treated as untrusted: secrets are redacted before anything is stored. The server password is never stored, only the name of the variable (or file) that holds it.

A queued message is sent only when the builder is idle and stays idle, no permission or question is pending, no reply or sub-agent is still running, and its OpenCode version is one Imperium was tested on. One message is in flight per builder. Imperium names the message (in OpenCode's own id format) before sending it and proves delivery by finding exactly that id in the builder's history, never by matching text. Anything short of proof (a timeout, a dropped connection, an error after the server may have saved it) is looked up by id; if it is not found within a minute the message becomes **UNCERTAIN** and waits for your decision. Imperium never resends on its own: a resend may run twice, so it needs explicit confirmation, and if both copies run you are told (CRITICAL). A message saved but never run (a known OpenCode failure) becomes **STRANDED** and blocks the builder until decided. A queue that stays blocked for ten minutes raises an ACTION event saying why.

Inside a Claude Code session the CLI acts as the *director* and never falls back to the owner's credential; the owner adds `--as owner` there. Every command takes `--json`. Exit codes: `0` ok, `1` error, `2` usage, `3` service not running, `4` refused, `5` integrity or doctor failure.

Configuration lives in `~/.imperium/imperium.toml`. Unknown keys are rejected.

## Roadmap

| Stage | Content | State |
|---|---|---|
| 1 | Store, journal and integrity chain, retention, backup and restore, feeds, call audit, service, CLI | **done** |
| 2 | Watching OpenCode builders: turns, messages, permissions, questions, status, catch-up after downtime | **done** |
| 3 | Queue, dispatcher and the delivery state machine, recovery | **done** |
| 4 | Rounds, claims, escalation, builder MCP | next |
| 5 | Approvals, director identity, questions, path rules | planned |
| 6 | Director MCP server, `watch`, tokens and the full local API | planned |
| 7 | Liveness (sub-agent aware) and resource gates | planned |
| 8 | Claude Code plugin and the director's playbook | planned |
| 9 | Read-only dashboard | planned |

Later: an Agent Client Protocol (ACP) adapter, dashboard controls, a reviewer module, a secrets manager.

## Development

The core uses only the Python standard library (Python 3.11 or newer). The tests are part of the product: crash and fault cases, tampering, restore, idempotency, history catch-up and a fake OpenCode server with its known quirks. Run them with:

```
PYTHONPATH=src python -m unittest discover -s tests
```

One test starts and stops a real background service.

## Licence

MIT. See [LICENSE](LICENSE).
