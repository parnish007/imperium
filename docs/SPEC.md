# Imperium specification

This document says what Imperium promises, what it does not, and how each part behaves. It describes the code in
this repository; where the code and this text disagree, that is a bug in one of them.

## 1. Actors

| Actor | What it is | What it may do |
|---|---|---|
| **Owner** | the human in charge | everything; some actions only the owner may take (marked *owner*) |
| **Director** | an AI session that plans and reviews (a Claude Code session) | send messages, open and decide rounds, define new checks, answer approvals and questions |
| **Builder** | a coding agent doing the work (an OpenCode session) | report on its rounds and escalate, through its own tool; nothing else |
| **Dashboard** | a read-only browser view | read |

Each actor has its own bearer token. A builder token reaches only the builder routes; a dashboard token only
read routes. The director token belongs to one Claude Code session (`CLAUDE_CODE_SESSION_ID`); the CLI never falls
back from it to the owner's token.

**Limit, stated plainly:** by default every actor runs as the same operating-system user. A process that ignores
the protocol (a builder running shell commands) can read the token files and the database. "Owner-only" therefore
holds against software that follows the protocol, not against a hostile builder. **Isolation mode** (Windows, see
[ISOLATION.md](ISOLATION.md)) runs builders under their own account: the API is reached through named pipes whose
access lists admit only the configured accounts, the operating system identifies the account at the other end of
each connection, a builder token is accepted only on the builder pipe and every other token only on the owner pipe,
and the TCP port accepts only the read-only dashboard.

## 2. Guarantees

| ID | Guarantee | Holds when | Does not hold |
|---|---|---|---|
| E1 | Every message ends in a recorded outcome: **ran** (ADMITTED), **delivered** but not yet run, **uncertain**, **stranded**, **cancelled**, **rejected**, or **replaced** by an explicit resend | always; nothing is dropped silently | — |
| E2 | Imperium never posts a message twice on its own | always | a principal's confirmed resend may make the builder run it twice; if both run, a CRITICAL `DUPLICATE_RAN` event says so |
| E3 | A message counts as delivered only when its own id is in the builder's history, and as run only when the builder replies to that id | the builder keeps its history | OpenCode runs its database with `synchronous=NORMAL`: a power loss can make it forget a message it had saved |
| F1 | Every event at or above a feed's floor is shown to that feed at least once, in order, and is never marked read without having been shown | always | — |
| V1 | `VERIFIED` means: the builder claimed done for the current generation, every required trusted check passed on a snapshot of that generation, the checks' own files were unchanged, and a principal judged the objective met | the builder cannot edit Imperium's store or the files outside its workspace that held-out checks use | under one OS user a hostile builder can do both (§1) |
| A1 | A round is accepted only by a named principal's decision, on the verified snapshot, with an unchanged workspace | always; the owner may override, and the override is journaled | — |
| J1 | The journal is hash-chained; a break found at start-up, on demand, or by the periodic check quarantines Imperium | the chain is an integrity check | a same-user process can recompute the chain: it detects accidents and naive edits, not a determined forger |

## 3. Delivery

### 3.1 States

```
QUEUED → DISPATCHING → POSTED → DELIVERED → ADMITTED
              │            └→ UNKNOWN ─┐        └→ STRANDED
              ├→ UNKNOWN ─────────────┤
              ├→ REJECTED             └→ UNCERTAIN → (decision) CANCELLED | SUPERSEDED
              └→ QUEUED (refused before OpenCode looked: 401, 403, 429)
```
Terminal: ADMITTED, REJECTED, CANCELLED, SUPERSEDED. UNCERTAIN and STRANDED wait for a principal's decision.
CANCELLED and SUPERSEDED messages stay watched: if one runs later, `LATE_ADMISSION` is reported.

- **DISPATCHING** is committed before the request. After a crash it becomes UNKNOWN, never posted again.
- **Naming.** Imperium generates the message id in OpenCode's own format before sending, and OpenCode keeps a
  caller-given id. The text starts with `[imperium msg=<id> builder=<name>]` (plus `round=` and `gen=` for round
  messages).
- **Proof.** POSTED means OpenCode answered 2xx, which proves nothing (OpenCode answers before saving). Proof is
  finding the id: by the history reader, or by a direct lookup each cycle. Our header under a *different* id is
  `REPLAY_SEEN` (for example a copy made by compaction) and is never proof.
- **Uncertainty.** Not found within `reconcile_window` (60 s) → UNCERTAIN (ACTION). Imperium never resends on its
  own; a resend needs `confirm_may_run_twice` and links the new message to the original.
- **Stranded.** Delivered but not run while the builder is idle for `admit_timeout` (300 s) → STRANDED (ACTION).
  Nothing else is sent to that builder until it is decided.
- **Idempotent send.** `client_key` per builder: the same key and text return the same message; the same key with
  other text is refused (409).

### 3.2 When a message may be sent

All of these must hold, decided on the poll that has just completed (never on stale state):
not stop-all; not observe-only after a restore; the session attached and history fully read; the OpenCode version
tested, or allowed by the owner for this builder; the permission list readable and empty; no question pending;
status idle for `idle_stable_polls` polls; no reply or user message still being written; no busy sub-agent session
(children and deeper); no message in flight to this builder (one at a time); not paused (a paused builder takes
only the owner's messages); free resources if the message asked for them. Messages from the owner go first.
A queue blocked for `stall_alert` (600 s) raises one `DISPATCH_STALLED` ACTION event saying why.

## 4. Rounds

```
PENDING → OPEN → CLAIMED_READY | CLAIMED_INCOMPLETE → VERIFIED → ACCEPTED | REJECTED | ABANDONED
```
- **Opening** queues a brief: the objective, the round's nonce and generation, how to report, the escalation policy.
  The brief is also written to `.imperium/rounds/<round>.md` in the workspace. Just before it is sent, the code is
  snapshotted (**base**).
- **Claims** come from the builder's MCP tool (`report_status`) or a claim file `.imperium/claims/<round>.json`;
  both go through one validator. A claim must quote the nonce and the current generation; an older generation is
  `STALE_CLAIM` and changes nothing; a wrong nonce is `CLAIM_REJECTED`. At most one report per 10 s per round.
- **Escalation** (`escalate`) is a flag, not a state; a director message within the round answers it.
- **Generations.** A repair message within the round, once the builder takes it in, starts a new generation: the
  round is OPEN again and VERIFIED is voided.
- **Verification** (asynchronous): holding the builder's reservation, snapshot the code (**candidate**), then for
  every check that applies:
  1. compare each file the check depends on with the hash recorded when the check was defined: a difference makes
     the round's checks **untrusted** (ACTION) and VERIFIED impossible until the *owner* approves the new versions;
  2. run it in a fresh worktree of the snapshot (never in the live workspace), argv only, no shell, a minimal
     environment plus the check's allow-list, with its timeout (the whole process tree is killed);
  3. if `must_fail_on_base`, run it on the base too: passing there means it does not test the change;
  4. record provenance: check id and version, argv, resolved executable and its hash, environment names and value
     hashes, snapshot tree, exit code, duration, output hash, the redacted output file.
  Changed test files are flagged (`TEST_FILES_CHANGED`).
- **Objective.** A principal records `met` or `not met` for the current generation. VERIFIED needs it.
- **Decision.** The first committed decision wins. Accept needs VERIFIED and a workspace whose tree still equals the
  verified snapshot; only the owner can override, and the override is journaled. A decision cancels the round's
  queued messages.

Snapshots are built in a temporary git index (uncommitted and unstaged work included, the builder's index
untouched), kept under `refs/imperium/...` so garbage collection keeps them. `.imperium/` is never in a snapshot.
The builder controls its repository's `.git/config` and `.gitattributes`, so nothing Imperium runs there may execute
them: files are hashed raw (`hash-object --no-filters`, never `git add`), a check's copy is written from raw object
contents (never a checkout or `git archive`, which apply smudge filters), diffs run with `--no-ext-diff
--no-textconv`, commits are never signed, hooks and fsmonitor are off. Plain `git push` does not send
`refs/imperium/*`; `git push --mirror` would.

## 5. Approvals and questions

- OpenCode's own permission configuration decides first; only its `ask` cases reach Imperium.
- **Rules** (*owner*): permission glob, pattern glob, optional `path_under` directory, allow or deny, optional
  builder. Deny wins. An allow rule must cover every pattern of the ask; a deny rule fires on any one.
- **Automatic answers** happen only while the registered director is **present**: one of its own calls (reading or
  acknowledging its feed, deciding something, or its PostToolUse hook) within `lease_ttl` (15 min), in this daemon
  run. The owner's calls do not count. A matching ask without a present director is **HELD**; a held ask is
  released only by a decision by hand. Every automatic answer is journaled with its rule; `imperium approvals --auto`
  lists them and `status` counts the ones not yet reviewed.
- **By hand:** `once` or `reject` by the director or owner; `always` is the owner's. The first decision wins.
- **Replies are operations:** committed with the decision, sent, retried every cycle (also after a restart) until
  OpenCode confirms or no longer lists the ask. An ask OpenCode stops listing while undecided is EXPIRED.
- **Questions** are answered by hand with OpenCode's structure: one list of chosen labels per question; a
  single-choice question takes one label.
- **Path rules** are a tripwire: while a builder can run shell commands it can reach any path, and a symlink can
  change between the decision and the use. The comparison is case-insensitive on Windows, refuses UNC paths, 8.3
  short names and `~`, resolves existing paths, and respects the separator boundary.

## 6. Feeds

A consumer (a reader's feed) has a floor fixed at creation; its bookmark counts only events at or above it.
`events_since` returns events in order up to a `high_water` captured on the first page; `shown_through` records how
far every event was returned; `ack(seq)` beyond it is refused. A reassigned feed (a new director session) must be
shown again before it can be acknowledged. `watch` prints headlines and never acknowledges. The dashboard and
`export` read the journal without touching any feed.

## 7. Liveness and resources

One operational state per builder: UNREACHABLE, PAUSED, WAITING_APPROVAL, WAITING_QUESTION, WAITING_PROVIDER,
WORKING, IDLE. Only WORKING can stall: no new message and no growth of the reply being written for `stall_after`
(600 s) → `SUSPECTED_STALL` (once per episode). Busy sub-agents suppress that alarm for at most `max_suppress`
(30 min), then `HANG_SUSPECTED`. Imperium reports; it does not kill. `imperium gate` reports free memory against
`min_free_gb` (3 GB); a message sent with `--needs-resources` waits until it is OK.

## 8. Integrity, backup, restore

SQLite in WAL mode with `synchronous=FULL`, one writing process. A database error fails the daemon closed (writes
refused until restart), except a lock timeout, which refuses only that write. The journal chain is verified at
start-up, on demand and every `verify_interval` (300 s). A break quarantines: reads, feed acknowledgements, backups,
stop-all and cancel still work; everything that sends or decides waits for the owner's `quarantine release`, which
records an accepted region; edits before or after that region are still caught. Backups use SQLite's online backup
API. Restore validates the chain, the head and the feed bookmarks, is crash-safe (prepared beside the database,
swapped in one rename), and starts observe-only until the owner confirms.

## 9. Interfaces

- **CLI** `imperium …` with `--json` everywhere; exit codes 0 ok, 1 error, 2 usage, 3 daemon not running,
  4 refused, 5 integrity or doctor failure.
- **Local API** on 127.0.0.1, random port written to `daemon.json`; Host and Origin checks; bearer tokens in a
  header (never cookies); per-principal rate limits; a capped call-audit table.
- **Director MCP** (`imperium mcp`) and **builder MCP** (`imperium builder-mcp`): stdio, JSON-RPC 2.0, standard
  library only. Read-only tools carry `readOnlyHint`.
- **Claude Code plugin** (`imperium plugin write <dir>`): the `imperium` skill (the playbook), the director MCP
  server, SessionStart and PostToolUse hooks, an optional feed monitor. Commands use the absolute interpreter path.
- **Dashboard** (`imperium dashboard`): read-only, a short-lived token passed in the URL fragment and kept in the tab's
  memory, strict Content-Security-Policy, no inline script, no builder text.

## 10. Not yet

Isolation mode on Linux and macOS (Unix sockets with peer credentials); adapters other than OpenCode over HTTP; pulling
work instead of pushing it (needs a fetch that also claims); notifications to the owner outside the feed; dashboard
actions.
