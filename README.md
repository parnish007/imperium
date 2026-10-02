# Imperium

**Imperium sits between an AI *director* and the AI *builders* it supervises, and makes sure nothing between them is lost, duplicated or taken on trust.**

A director is an AI session that plans and reviews work (for example a Claude Code session). A builder is a coding agent that does the work (OpenCode first). A human *owner* stays in charge of both.

> **Status: pre-alpha.** All nine planned stages are built and tested against a fake OpenCode server reproducing OpenCode 1.18.32's behaviour and known bugs; see [Testing](#testing) for what has and has not been run against a real server. Expect rough edges.

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
- **"Owner-only" is enforced against software that follows the protocol, not against a hostile process.** The owner's token is a file in your runtime folder; any program running as your OS user, including a builder, can read it and act as the owner. A real boundary needs the builder under another OS user, in a container or a VM. **Isolation mode** runs builders under their own account and admits them only through a channel the operating system guards (a named pipe on Windows, a Unix socket on Linux and macOS; the account at the other end is checked on every connection): see [docs/ISOLATION.md](docs/ISOLATION.md).
- The local API listens on `127.0.0.1` only, checks the `Host` and `Origin` headers (these stop web pages, not local programs), and needs a 256-bit bearer token. Tokens are stored hashed; the runtime folder is readable only by your user.
- The journal's hash chain detects accidental corruption and naive edits. A process running as the same user could recompute it, so it is an integrity check, not proof.

## Install (from source, for now)

Requires Python 3.11 or newer. [`uv`](https://docs.astral.sh/uv/) is the easiest way to get one.

```
git clone https://github.com/parnish007/imperium.git
cd imperium
uv tool install .        # or: pip install .
```

## Quick start

```
imperium init                      # ~/.imperium (or $IMPERIUM_HOME): database, config, owner token
imperium up                        # start the background service (idempotent)

# register an OpenCode session as a builder (its server: `opencode serve`)
imperium builder add coding --endpoint http://127.0.0.1:<port> --session <ses_id> --directory <repo>
imperium builder mcp-config coding --write    # adds Imperium's builder tool; restart OpenCode while idle

# a trusted check: Imperium runs it on a snapshot, never in the live workspace
imperium check add unit --builder coding --depends tests/test_calc.py --must-fail-on-base -- python -m pytest -q

# one round of work
imperium round open coding --objective "Make add() add; tests/test_calc.py must pass."
imperium round list                # PENDING -> OPEN -> CLAIMED_READY ...
imperium round verify <round> --wait 600
imperium round objective <round> met --note "..."
imperium round accept <round>      # refused unless VERIFIED and the workspace is unchanged
```

Then, for the director (a Claude Code session):

```
imperium plugin write ~/imperium-plugin       # skill, MCP server, hooks; validated with `claude plugin validate`
claude --plugin-dir ~/imperium-plugin
imperium --as owner director claim            # run once inside that session
```

More:

```
imperium status / events / ack <seq> / show <seq>     # what happened; your feed and its bookmark
imperium send coding --message "..." --key k1          # a message outside rounds
imperium queue / msg show <id> / msg resolve <id> wait|cancel|resend --confirm-may-run-twice
imperium approvals / approve <id> / deny <id>          # permission asks; `approvals --auto`: what your rules answered
imperium rule add bash "git status*" --allow           # owner: rules used only while the director is present
imperium questions / answer <id> --choice "A"
imperium round diff <round> / round message <round> --message "..." / round reject <round>
imperium check list / check approve <id> (owner) / check retire <id> (owner)
imperium gate                      # enough free memory for heavy work? (`send --needs-resources` waits for it)
imperium dashboard                 # read-only view in your browser
imperium watch --consumer director # new headlines as they arrive (for a monitor); never acknowledges
imperium stop-all / resume-all     # the emergency brake (anyone) and its release (owner)
imperium backup / restore <file> / verify-journal / export --out f.jsonl / doctor / down
```

Every command takes `--json`. Exit codes: `0` ok, `1` error, `2` usage, `3` service not running, `4` refused, `5` integrity or doctor failure. Inside a Claude Code session the CLI acts as the director and never falls back to the owner's credential; the owner adds `--as owner` there. Configuration: `~/.imperium/imperium.toml` (unknown keys are rejected).

## How a round is verified

1. When the round opens, its brief (objective, nonce, how to report, when to escalate) is queued and the code is snapshotted just before the brief is sent.
2. The builder reports `ready` through its tool (or a claim file), quoting the round's nonce and current generation. A claim is evidence, not acceptance.
3. `round verify` snapshots the code again and, for each trusted check: refuses to trust it if a file it depends on changed (only the owner can approve new versions); runs it in a fresh copy of the snapshot with a minimal environment and a timeout; runs it on the starting snapshot too when it must fail there (a check that passes without the change does not test the change); records the executable and its hash, the environment names, the exit code and the output.
4. A person or the director records whether the objective is met. Only then is the round VERIFIED.
5. Accept is refused if the workspace changed since the verified snapshot. A repair message starts a new generation and voids VERIFIED.

The details, and what each guarantee does *not* cover, are in [docs/SPEC.md](docs/SPEC.md).

## Roadmap

| Stage | Content | State |
|---|---|---|
| 1 | Store, journal and integrity chain, retention, backup and restore, feeds, call audit, service, CLI | **done** |
| 2 | Watching OpenCode builders: turns, messages, permissions, questions, status, catch-up after downtime | **done** |
| 3 | Queue, dispatcher and the delivery state machine, recovery | **done** |
| 4 | Rounds, claims, escalation, trusted checks on snapshots, builder MCP | **done** |
| 5 | Approvals with presence lease and the automatic-answer report, questions, path rules | **done** |
| 6 | Director MCP server, `watch`, the full local API | **done** |
| 7 | Liveness (sub-agent aware) and the resource gate | **done** |
| 8 | Claude Code plugin and the director's playbook | **done** |
| 9 | Read-only dashboard | **done** |

Next: more builder adapters (Claude Code, Codex, ACP), owner notifications, dashboard actions.

## Testing

The core uses only the Python standard library (Python 3.11 or newer). The tests are part of the product: crash and fault cases, tampering, restore, idempotency, history catch-up and a fake OpenCode server with its known quirks. Run them with:

```
PYTHONPATH=src python -m unittest discover -s tests
```

One test starts and stops a real background service.

## Licence

MIT. See [LICENSE](LICENSE).
