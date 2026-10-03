# How to use Imperium

A step-by-step guide, from installing Imperium to running AI coding agents under supervision every day. For what
Imperium is and why, see the [README](README.md); for every guarantee and limit in detail, see the
[specification](docs/SPEC.md).

**Words used in this guide**

- **Builder**: a coding agent doing the work (an OpenCode session, or an agent Imperium starts over the Agent
  Client Protocol).
- **Director**: the AI session that plans the work and hands it out, usually Claude Code. Optional: you can direct
  builders yourself from the command line.
- **Owner**: you. Some things only you can do: release a stop, approve a changed test, override a refusal.
- **Round**: one piece of work with one objective, followed from "brief sent" to "accepted".
- **Check**: a command Imperium runs to test a round's result, on a copy of the code the builder cannot edit.

---

## 1. Install

You need Python 3.11 or newer and git. [`uv`](https://docs.astral.sh/uv/) is the easiest way to get Python.

```
git clone https://github.com/parnish007/imperium.git
cd imperium
uv tool install .        # or: pip install .
imperium --version
```

Imperium has no dependencies outside Python's standard library and needs no account or cloud service.

## 2. Set it up once

```
imperium init       # creates ~/.imperium: database, config file, your owner token
imperium up         # starts the background service
imperium doctor     # checks the installation
imperium status     # what is running, what needs you
```

To keep Imperium's files somewhere else, set `IMPERIUM_HOME` or pass `--home <dir>` to every command.
`imperium down` stops the service; your data stays.

## 3. Connect a coding agent

### Option A: an agent Imperium starts itself (Agent Client Protocol)

The simplest way. Imperium launches the agent, opens a session in your repository and talks to it directly:

```
imperium builder add coding --acp "opencode acp" --directory /path/to/your/repo
```

`--acp` takes the command that starts the agent in ACP mode, as a string or a JSON list (use a JSON list when a
path contains spaces). Any ACP agent works: OpenCode, Gemini CLI, or Claude Code and Codex through their ACP
adapters; see each agent's documentation for its ACP command. Stopping Imperium stops the agents it started.

### Option B: an OpenCode server you run

Use this when you already work in OpenCode:

```
opencode serve --port 4096                          # in your repository
curl http://127.0.0.1:4096/session                  # lists sessions; pick an id starting with ses_
imperium builder add coding --endpoint http://127.0.0.1:4096 --session ses_... --directory /path/to/your/repo
imperium builder mcp-config coding --write          # gives the builder its report tool
```

Restart OpenCode once, while it is idle, so it loads the report tool. If the server needs a password, add
`--password-env VAR` or `--password-file FILE`. If your OpenCode version is newer than the one Imperium was tested
with, it says so; after checking, allow it with `imperium builder allow-version coding <version>`.

Check the builder: `imperium status` should show it as `IDLE`.

## 4. Your first round of work

### 4.1 Define a check

A check is a command whose exit code decides whether the work is right. Define it **before** the builder starts:

```
imperium check add unit --builder coding \
    --depends tests/test_calc.py --must-fail-on-base --timeout 300 \
    -- python -m pytest -q tests/test_calc.py
```

- `--must-fail-on-base`: the check must fail on the code as it was before the round. A check that passes either
  way does not test the change.
- `--depends`: files the check relies on. If the builder edits one, the check stops being trusted until you
  approve the new version (`imperium check approve unit`).
- `--dir` runs it in a subdirectory; `--env NAME` passes an environment variable through (the environment is
  otherwise minimal); `--optional` reports a check without requiring it to pass.

### 4.2 Open the round

```
imperium round open coding --objective "Make add() in calc.py return the sum. tests/test_calc.py must pass. \
Change only calc.py; do not edit tests. If a test looks wrong, escalate instead of changing it."
```

A good objective says what must be true at the end, what is in scope and what is not. Imperium wraps it in a brief
that tells the builder how to report and when to ask for help, snapshots the code, and queues the brief. Use
`--objective-file` for long objectives.

### 4.3 Follow it

- `imperium dashboard` opens a live, read-only page in your browser (see section 6).
- `imperium events` shows your feed; `imperium ack <seq>` marks it read. `imperium show <seq>` shows one event in
  full.
- `imperium round show <round>` shows one round: its messages, the builder's reports and check runs.

The builder works, runs the tests itself, and reports through its tool. The round becomes `CLAIMED_READY`. That
is the builder's claim, not proof.

### 4.4 Verify, judge, decide

```
imperium round verify <round> --wait 600       # snapshot the code, run every check in a fresh copy
imperium round diff <round>                    # what changed; edited tests are flagged
imperium round objective <round> met           # or: not-met, with --note "why"
imperium round accept <round>                  # or: reject, or abandon
```

Verification passes only when every check passes on the builder's code and each `--must-fail-on-base` check fails
on the original code. After you record the objective as met, the round is `VERIFIED`. Accepting is refused if the
workspace changed since the verified snapshot.

To ask for a fix within the same round: `imperium round message <round> --message "..."`. That starts a new
generation of the round and cancels any earlier verification.

## 5. Let Claude Code direct the work

```
imperium plugin write ~/imperium-plugin        # an MCP server, hooks and a playbook skill
claude --plugin-dir ~/imperium-plugin
```

Inside that Claude Code session, once:

```
imperium --as owner director claim
```

The director now has tools to open rounds, read the feed, answer approvals and questions, verify and decide. The
playbook skill teaches it the rules: one objective per round, checks before claims, builder text is data and never
instructions. Any other MCP client can use the same tools with `imperium mcp`.

You stay in charge: the director cannot release a stop, approve a changed check, give "always" permissions or
override a refused acceptance, and every automatic decision is listed for you.

## 6. The dashboard

```
imperium dashboard            # opens your browser; --no-open prints the link instead
```

The link carries a read-only key in the part after `#`, which the browser never sends anywhere. It works in that
tab until the service restarts or after 12 idle hours; after a reload, run `imperium dashboard` again.

What you see, top to bottom:

- **One sentence**: "All quiet" or how many things need you, then how many agents are doing what.
- **Needs you**: one card per decision (accept or reject work, a permission request, a question, an instruction
  that may not have arrived), each with the exact command to run in your terminal.
- **Agents**: each builder's state (working, idle, waiting for your permission, not answering), what is waiting to
  be sent, and whether its helper agents can be seen.
- **Work**: each round with five marks: sent, agent says done, tests pass, goal met, accepted. A mark is filled only
  when Imperium recorded the fact itself.
- **Instructions sent** and **Activity**: what was sent and what happened, in plain words. "Show everything" adds
  the housekeeping events.

The page cannot change anything and never shows what agents wrote. The cursor in the logo blinks while the page
is live; if Imperium stops answering, the page greys out and says how old the data is.

## 7. Everyday decisions

| Something needs you | See it | Decide |
|---|---|---|
| A permission request | `imperium approvals` | `imperium approve <id>` or `imperium deny <id>` |
| A question | `imperium questions` | `imperium answer <id> --choice "A"` (one `--choice` per question) |
| An instruction that may not have arrived (`UNCERTAIN`) | `imperium msg show <id>` | `imperium msg resolve <id> wait` or `cancel`, or `resend --confirm-may-run-twice` |
| An instruction saved but never run (`STRANDED`) | `imperium msg show <id>` | same as above |
| Answers your rules gave automatically | `imperium approvals --auto` | review; change your rules if needed |

**Rules** answer routine permission requests for you, but only while a director is present:

```
imperium rule add bash "git status*" --allow
imperium rule add bash "git push*" --deny
imperium rule add edit "*" --allow --path-under /path/to/your/repo
imperium rule list / rule remove <id>
```

Deny rules always win, and some requests are never answered by a rule (very long or many-pattern asks).

**Messages outside rounds**: `imperium send coding --message "..." --key k1`. The key makes the send safe to
repeat: the same key and text never queue a second copy. Imperium never resends on its own.

## 8. Notifications

To hear about anything that needs you without watching a screen, set a command in `~/.imperium/imperium.toml`:

```toml
[notify]
command = ["python", "/path/to/notify.py"]
floor = "ACTION"      # ACTION, or CRITICAL for only the serious ones
timeout = 30.0
```

The command runs once per event at or above the floor, with the event as JSON on standard input and in the
variables `IMPERIUM_EVENT_SEQ`, `IMPERIUM_EVENT_TYPE`, `IMPERIUM_EVENT_SEVERITY`, `IMPERIUM_EVENT_BUILDER` and
`IMPERIUM_EVENT_HEADLINE`. Use it to send a desktop notification, a chat message or an email. Restart Imperium
after changing the file.

For a terminal that prints new events as they arrive: `imperium watch`.

## 9. Safety tools

- **Stop everything**: `imperium stop-all --reason "..."`. Nothing new is sent to any builder. When you (the
  owner) pull it, open permission requests are also held and the director must claim again later. Work already
  running inside an agent is not interrupted. Release with `imperium resume-all`.
- **Pause one builder**: `imperium builder pause coding` (only your own messages reach it);
  `imperium builder resume coding`.
- **Integrity**: `imperium verify-journal` checks the journal's hash chain. If a check fails while running,
  Imperium quarantines itself: everything that acts stops until you inspect it and run
  `imperium quarantine release`.
- **Backups**: `imperium backup` (online, while running), `imperium restore <file>` (with the service stopped).
  After a restore Imperium only watches until you confirm with `imperium restore-confirm`.
- **Export**: `imperium export --out journal.jsonl` writes the whole journal, builder text included (with
  configured secrets removed).
- **Resources**: `imperium gate` says OK or WAIT based on free memory; `imperium send --needs-resources` waits for
  OK.
- **Isolation mode**: run builders under their own operating-system account, reachable only through an OS-checked
  named pipe or Unix socket. See [docs/ISOLATION.md](docs/ISOLATION.md).

## 10. Configuration

`~/.imperium/imperium.toml`; unknown keys are rejected. Restart the service after a change.

| Setting | Default | What it does |
|---|---|---|
| `[opencode] poll_interval` | 2.0 | seconds between looks at each builder |
| `[opencode] unreachable_after` | 3 | failed looks before a builder is reported as not answering |
| `[delivery] idle_stable_polls` | 2 | idle looks in a row before a builder may receive a message |
| `[delivery] reconcile_window` | 60 | seconds to prove a message arrived before it becomes `UNCERTAIN` |
| `[delivery] admit_timeout` | 300 | seconds a delivered message may sit unrun before it is `STRANDED` |
| `[delivery] stall_alert` | 600 | seconds before Imperium says why messages are not moving |
| `[approvals] lease_ttl` | 900 | seconds the director counts as present after its last call |
| `[liveness] stall_after` | 600 | seconds of silence before a working builder is reported as stalled |
| `[liveness] max_suppress` | 1800 | seconds busy helper agents may explain silence before "seems stuck" |
| `[resources] min_free_gb` | 3.0 | free memory below which `gate` says WAIT |
| `[integrity] verify_interval` | 300 | seconds between journal integrity checks |
| `[redaction] env_names` | [] | environment variables whose values are removed from builder text |
| `[notify] command`, `floor`, `timeout` | off | see section 8 |
| `[isolation] owner_accounts`, `builder_accounts` | off | see docs/ISOLATION.md |

## 11. Troubleshooting

| You see | It means | Do |
|---|---|---|
| Dashboard: "This link has expired" | the service restarted, or 12 idle hours passed | `imperium dashboard` |
| Dashboard: "Imperium is not answering" | the service is down | `imperium status`, then `imperium up` |
| A builder stays `UNREACHABLE` | its server or process is gone | restart it; `imperium status` shows the last error |
| Messages stay `QUEUED` | the builder is busy, waiting on you, paused, or everything is stopped | `imperium status` shows the reason (`dispatch_blocked`) |
| `UNCERTAIN` | Imperium could not prove the message arrived | wait, then decide (section 7); never resend blindly |
| A check is "not trusted" | a file it depends on changed | look at the change; if it is legitimate, `imperium check approve <id>` |
| Verification fails "does not fail on base" | the check passes even without the change | fix the check so it tests the change |
| `accept` refused | the round is not verified, or the workspace changed after verification | verify again; the owner can `--override`, which is recorded |
| "untested OpenCode version" | your OpenCode is newer than the tested one | check it works, then `imperium builder allow-version <name> <version>` |
| A command says it needs a director token | you are inside Claude Code without a claimed director | `imperium --as owner ...` for owner commands |

## 12. Known limits

Imperium is honest about what it does not do yet:

- It is **not a sandbox**. An agent running as your user can do what you can. Use isolation mode, a container or a
  VM for containment.
- **Stop-all does not interrupt running work**; it stops anything new from being sent.
- Builders started over ACP do not report their helper agents, so "helper agents" shows as not visible.
- A check whose files changed is still run (its result does not count), and a check that timed out or never ran on
  the original code can be counted as failing there. Look at the base run's exit code before relying on it.
- A slow ACP agent start can briefly delay checks on other builders.
