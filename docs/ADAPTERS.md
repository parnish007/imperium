# Adapter capabilities

Protocol compatibility and empirical validation are different. Only the two OpenCode demonstrations described
in [benchmarks.md](benchmarks.md) have published real-agent results. Gemini CLI, Claude Code and Codex through ACP
remain protocol-compatible targets; they are not established as tested integrations by those demonstrations.

| Capability | OpenCode HTTP | ACP v1 |
|---|---|---|
| Published real-agent version | OpenCode 1.18.32 | OpenCode 1.18.32 via ACP |
| Delivery evidence | Caller-supplied message ID observed in history | Active prompt response or turn activity for the exact session |
| Recovery evidence | History reconciliation by message ID | Session replay when the agent supports loading; otherwise uncertainty/context loss |
| Readiness scope | Parent plus observed descendants (depth limit 8) | Current session/active prompt only |
| Subagent visibility | Observed through OpenCode's session relationships | Unavailable; exposed as unknown, never an empty known list |
| Progress | Message identity and growing reply | Monotonic counter of matching-session turn updates |
| Permission requests | Supported | Supported, matched to session and supplied options |
| Questions | Supported | Unsupported; no elicitation implementation |
| Cancellation | Abort endpoint, then observe session idle | `session/cancel`; confirmation requires prompt response `stopReason=cancelled` |
| Detached child processes stopped | Not guaranteed | Not guaranteed |
| Untrusted version | Observe-only until owner allows | Protocol v1 required; product-specific compatibility is unproven |

ACP notifications carry a session ID but no universal prompt ID. Imperium enforces one prompt in flight and
rejects other-session updates. It still relies on an agent respecting turn order within that session. This is
weaker than OpenCode's history identity check and is not a claim of cryptographic proof or agent honesty.

The engine uses bounded concurrent adapter workers (`opencode.max_workers`, default 4, range 2–32), with at most
one poll per builder. A slow builder does not block a free worker from polling another. Saturating all workers
still causes queueing; the poll interval is a scheduling interval, not a maximum event-delivery latency.

## Pause and cancellation

`imperium stop-all` pauses new prompts and positive approval replies. Running work continues.
`imperium abort-all` also records cancellation requests for all registered builders and cancels active/queued
verification jobs. `imperium status --json` reports each builder's request:

| State | Meaning |
|---|---|
| `pending` / `sending` | A durable request is queued / an attempt has been recorded |
| `requested` | ACP cancellation notification sent; no confirmation yet |
| `acknowledged` | HTTP abort returned; work cessation not yet observed |
| `confirmed` | ACP active prompt explicitly answered `cancelled` |
| `idle_observed` | The session was observed idle; no claim about detached processes |
| `process_exited` | Managed ACP process is not running; descendants are unknown |
| `uncertain` | Transport failure, restart during cancellation, or no confirmation within 30 seconds |

Only the owner resumes. Resume refuses while requests are pending/in progress. Uncertain outcomes require owner
inspection; an explicit new `abort-all` makes another recorded attempt. Neither command claims to kill every OS
process. ACP processes that are stopped are not automatically restarted while dispatch is paused.
