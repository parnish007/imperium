# Verification runner and upgrade guide

Verification has two separate responsibilities: protect the definition of a check, and contain the candidate
code it executes. Hashing a test or placing it outside the repository addresses only the first. An unchanged
test can import arbitrary candidate code.

## Default: a restricted Linux container

Checks now default to Docker. There is **no host fallback** when Docker, the image, or its prerequisites are
missing. The daemon, feeds and delivery can still operate; verification reports an execution error.

Prepare a trusted Linux image containing `python3` (for Imperium's bootstrap), your language runtime, and your
test dependencies. Build or pull it yourself, then record its immutable ID:

```sh
docker pull python:3.13-slim
docker image inspect python:3.13-slim --format '{{.Id}}'
```

That example supports standard-library Python tests. For pytest, compilers or other dependencies, build an image
with them installed. Verification never downloads dependencies or pulls images. In `imperium.toml`:

```toml
[verification]
backend = "docker"
image = "sha256:REPLACE_WITH_THE_64_HEX_DIGIT_IMAGE_ID"
memory_mb = 1024
cpus = 2.0
pids_limit = 128
writable_mb = 256
env_allowlist = []
```

Restart the daemon after changing configuration. `imperium doctor` reports missing runner configuration, and
`imperium status --json` names the selected backend. A repository digest (`name@sha256:...`) is also accepted.
Use a local Docker engine, or Docker Desktop configured for Linux containers. Images must not declare `VOLUME`.

Each check gets:

- a non-root UID/GID, all Linux capabilities dropped and `no-new-privileges`;
- no external network, host PID namespace, runtime folder, owner token, live workspace or Docker socket;
- a read-only image, snapshot input and runner input;
- a fresh writable copy in size-limited tmpfs, plus bounded memory, CPU and process counts;
- bounded output, a timeout and explicit cancellation; the named container is forcibly removed afterward.

The image and Docker daemon are trusted. Containers share a kernel; use a VM if your threat model includes kernel
exploits. Linux containers do not validate native Windows application behavior. Do not place credentials in the
image. This runner does not isolate the director or builders themselves; see [ISOLATION.md](ISOLATION.md).

Check commands name executables **inside the image**, for example `python3`, not a host virtualenv path.
Relative command arguments refer to the snapshot. Absolute held-out file arguments must be separately listed
in `--depends`; Imperium copies those exact, hash-checked files and remaps exact argv elements into the container.
List helper files too. Embedded strings such as `--config=/host/file` are not remapped: use separate arguments.
The held-out directory layout is preserved within each drive. No whole host directory is mounted.

Requested host environment variables must also be in the owner's `env_allowlist`. Values are passed in the
read-only payload; audit records contain names and value hashes. An image's own environment is covered by its
immutable image ID. Container provenance records the image ID, argv, working directory and Docker client hash;
the host Docker executable's hash is not represented as the candidate executable's hash.

## Check integrity and baseline outcomes

If **any** check dependency changed, no check in that verification job runs. The owner must inspect the change
and explicitly approve the new check version. Held-out dependencies are copied and rechecked immediately before
container execution, closing the gap between the initial hash comparison and use.

`--must-fail-on-base` requires the configured test-failure exit code, default **1**. Other expected assertion
codes can be set with repeated `--base-failure-code` options (API/MCP: `base_failure_codes`). Codes 125 and above,
signals, timeouts, missing programs/directories, container errors, cancellation and output exhaustion cannot
satisfy the baseline condition.

An exit code is a contract with the test runner, not semantic proof. If your runner uses the same code for an
assertion failure and an import/setup error, supply a wrapper that distinguishes them. For intentionally testing
a timeout, the check itself must classify the expected behavior and exit with its assertion-failure code;
Imperium's outer timeout always means execution failure. A new test absent from the base needs a held-out check
that runs against both revisions; inability to launch a missing base test is not useful evidence.

## Explicitly unsafe local development

For trusted repositories on machines without Docker, the owner can deliberately configure:

```toml
[verification]
backend = "unsafe-local"
```

This runs candidate code with the daemon's OS permissions. Minimal environment variables, snapshots and process
cleanup do **not** make it a sandbox. Isolation mode refuses this backend, including at the execution boundary.
Automated fixtures choose it only for test programs supplied by this repository. Production has no automatic
switch to it.

## Upgrade behavior

Back up before upgrading. Schema version 8 adds baseline codes (existing checks get `[1]`), structured execution
provenance and cancellation records. Previously verified live rounds lose their verification evidence and must
be verified again under the new policy. Historical accepted/rejected/abandoned decisions are retained. A daemon
event records invalidation when needed. Older binaries cannot open the upgraded database; restore a pre-upgrade
backup to roll back.

## Validation

`tests/test_container_verification.py` exercises the actual Docker boundary in the Linux CI job. Other platforms
run the unit, protocol and fault suite; that does not establish Docker Desktop containment on those platforms.
See [VALIDATION.md](VALIDATION.md) for what remains unmeasured.

References: [Docker container options](https://docs.docker.com/reference/cli/docker/container/run/),
[ACP cancellation](https://agentclientprotocol.com/protocol/v1/prompt-turn).
