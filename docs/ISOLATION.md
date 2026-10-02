# Isolation mode (Windows)

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
- the builder can still change anything in its own workspace; that is why claims are verified on snapshots and why
  a check's files are hashed;
- checks run as your account: a visible check runs code from the builder's repository (its tests). Keep the checks
  that matter **held out**: scripts outside the workspace, listed in the check's `depends`, and review the round's
  diff before approving changed check files.
- Unix sockets for Linux and macOS are not implemented yet.

## Setup

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
