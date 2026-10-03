# Validation and remaining evidence

This repository's tests can establish specific implementation properties. They cannot by themselves establish
that Imperium improves developer productivity or safely completes arbitrary tasks unattended.

## Review regression map

| Finding | Change | Automated evidence |
|---|---|---|
| Modified check executed before rejection | Dependency mismatch refuses the entire job before execution | `TestVerification.test_changed_checker_never_executes_and_no_other_check_runs` |
| Held-out tests still execute candidate code as owner | Default Docker backend; explicit unsafe local mode refused with isolation | `test_container_verification.py` on Linux, plus policy tests |
| Wrong-session ACP activity proved delivery | Session validation for activity and permission requests | `TestACP` in `test_reliability_review.py` |
| ACP progress caused false stalls | Monotonic turn progress and explicit unknown subagent visibility | Active-stream, silence and non-turn-noise regressions |
| One slow builder delayed all polling | Bounded concurrent workers; no overlapping polls per builder | Blocked-builder/healthy-builder regression |
| Setup errors satisfied baseline failure | Explicit expected exit codes and execution-error classification | Real check execution plus injected infrastructure outcomes |
| Stop command implied active work ceased | Separate pause and durable cancellation operations | HTTP cancellation outcomes, ACP subprocess cancellation and check cancellation |

The cross-platform CI matrix still runs the existing fault suite. A separate Linux job pulls a test image,
records its immutable ID and runs real container access, network, timeout and held-out import checks. A skipped
container test is not containment evidence. Image downloads occur in CI setup, never inside verification.

## Real-agent evaluation protocol

The published evidence remains two small OpenCode demonstrations and fake-server delivery tests. This change
does not manufacture additional real-agent runs or comparative performance numbers. Before broad reliability
or productivity claims, run paired trials with a direct-agent baseline and Imperium:

1. Freeze repository commit, task, model/version, agent/version, checks and budget. Use fresh isolated workspaces.
2. Include a multi-file bug fix, feature addition, test-suite regression, dependency/build failure, and interrupted
   long-running task. Include a changed-check attempt and a held approval. Use at least 20 paired trials per task
   class as an initial evaluation, with randomized treatment order; do not present that count as proof of adequacy.
3. Have independent checks/reviewers judge the final result. Record incorrect acceptance even if all visible tests
   passed. Keep unsuccessful, cancelled and timed-out trials in the denominator.
4. Record task success, false acceptance, silent duplicate effects, manual interventions, supervision minutes,
   elapsed time, token/cost usage, recovery time, and p50/p95 observation latency under one unhealthy builder.
5. Publish raw trial records, environment details, uncertainty intervals and failures. Define the primary metric
   before running the experiment. Avoid selecting only successful runs or treating message accounting as task success.

For adapter acceptance, exercise startup, permissions, questions (or explicit unsupported behavior), streaming,
subagents (or explicit unknown visibility), crash/restart, session replay, cancellation and a complete verified
round for each exact supported product/version. Docker Desktop and native Windows verification require their own
validation; Linux container results do not transfer automatically.

## Remaining limits

- No comparative task-quality or productivity result is claimed by this PR.
- No new real model/provider runs were performed by the automated code review.
- Containers are not VMs; image/runtime/kernel trust remains.
- Same-user owner/director credentials remain a cooperative boundary.
- An adapter's evidence relies on the underlying agent faithfully implementing its protocol.
- The supervisor still owns protocol and OS integration maintenance; zero Python dependencies does not remove it.

New interface features should wait until this evaluation produces reproducible evidence about existing guarantees.
