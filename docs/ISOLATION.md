# Isolation mode

Without isolation every actor runs as your Windows account, so a builder that runs shell commands can read
Imperium's tokens and database and act as you. Isolation mode runs each builder under its **own Windows account**
and lets it reach Imperium only through a named pipe that admits that account, with a token good only for the
builder's own routes (report, escalate, list its rounds).

What it gives you:
- the builder's account cannot read your runtime folder (`%USERPROFILE%\.imperium`: tokens, database, logs);
- the builder pipe admits only the builder accounts you list, and only builder tokens; the owner pipe admits only
  your account; Windows tells Imperium which account is at the other end of each connection;
- the TCP port accepts only the read-only dashboard token;
- nothing Imperium runs in the builder's repository executes the builder's git configuration (filters, diff
  drivers, hooks, fsmonitor, signing programs); checks run on a raw copy of the snapshot.

What it does not give you:
- the director (Claude Code) still runs as your account, so owner and director are separated by tokens, not by the
  operating system;
- an **ACP builder** (`builder add --acp`) is the daemon's child process and runs as the daemon's account unless
  its command switches account (for example `runas` or `sudo -u imperium-builder ...`); Imperium records an
  ACTION event when one is added in isolation mode. For a real boundary, run ACP agents through such a command,
  or use OpenCode over HTTP started under the builder account;
- the builder can still change anything in its own workspace; that is why claims are verified on snapshots and why
  a check's files are hashed;
- checks default to restricted Linux containers. Isolation mode refuses `unsafe-local` verification. Held-out
  checks protect test integrity, but an unchanged test may still import hostile candidate code; the container
  boundary is required separately. See [VERIFICATION.md](VERIFICATION.md) for image setup and kernel/runtime limits.

## Setup on Windows

Run these in an administrator PowerShell where marked.

1. **Create the builder account** (administrator):
   ```
   net user imperium-builder * /add
   ```
2. **Find the account ids (SIDs)**:
   ```
   whoami /user
   ([System.Security.Principal.NTAccount]'imperium-builder').Translate([System.Security.Principal.SecurityIdentifier]).Value
   ```
3. **Configure Imperium** (`%USERPROFILE%\.imperium\imperium.toml`), then `imperium down` and `imperium up`:
   ```
   [isolation]
   owner_accounts = ["S-1-5-21-...-1001"]     # yours, from whoami /user
   builder_accounts = ["S-1-5-21-...-1002"]   # imperium-builder
   ```
   `imperium status` now reports `"isolation": true` and `"transport": "pipe:owner"`.
4. **Give the builder its workspace**: the repository it works in must be writable by `imperium-builder`
   (`icacls <repo> /grant imperium-builder:(OI)(CI)M`). Python and OpenCode must be installed where that account can
   run them.
5. **Write the builder's own OpenCode config** (it holds a new builder token and the pipe name, nothing else):
   ```
   imperium builder mcp-config coding --isolated C:\Users\imperium-builder\imperium-opencode.json
   icacls C:\Users\imperium-builder\imperium-opencode.json /inheritance:r /grant imperium-builder:R
   ```
6. **Start the builder's OpenCode as that account**, with that config:
   ```
   runas /user:imperium-builder "cmd /c set OPENCODE_CONFIG=C:\Users\imperium-builder\imperium-opencode.json&& set OPENCODE_SERVER_PASSWORD=...&& opencode serve --port 4100"
   ```
   then register it as usual (`imperium builder add coding --endpoint http://127.0.0.1:4100 --session ...`).
7. **Check the boundary** from the builder's account: reading `%USERPROFILE%\.imperium\tokens\owner` of your account
   must fail with "Access is denied", and opening the owner pipe must fail.

## Setup on Linux and macOS

The channels are Unix sockets in `/tmp/imperium-<your uid>-<tag>/`, a directory others may enter but not list or
write; Imperium refuses to start if it exists with other permissions or another owner. The owner socket is `0600`;
the builder socket is open to connect, and every connection is checked against the configured uids
(`SO_PEERCRED` on Linux, `getpeereid` on macOS).

1. `sudo useradd -m imperium-builder` (macOS: create a standard user in System Settings).
2. `id -u` (yours) and `id -u imperium-builder`.
3. In `~/.imperium/imperium.toml`, then `imperium down` and `imperium up`:
   ```
   [isolation]
   owner_accounts = ["1000"]
   builder_accounts = ["1001"]
   ```
4. Give `imperium-builder` write access to the builder's repository (a shared group, or ACLs).
5. `imperium builder mcp-config coding --isolated /home/imperium-builder/imperium-opencode.json`, then
   `sudo chown imperium-builder: /home/imperium-builder/imperium-opencode.json` and `sudo chmod 600` it.
6. Start the builder's OpenCode as that account with that config:
   `sudo -u imperium-builder env OPENCODE_CONFIG=/home/imperium-builder/imperium-opencode.json OPENCODE_SERVER_PASSWORD=... opencode serve --port 4100`,
   then `imperium builder add coding --endpoint http://127.0.0.1:4100 --session ...`.
7. From the builder's account, reading your `~/.imperium/tokens/owner` must fail, and connecting to the owner
   socket must fail.
