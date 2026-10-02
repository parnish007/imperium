"""Write the Claude Code plugin for this installation (DESIGN §11.5).

The plugin is generated, not shipped as fixed files, so its hook and MCP commands carry the absolute path of the
interpreter Imperium is installed in and the runtime directory in use: they do not depend on PATH [V-34].
Contents: the `imperium` skill (the director's playbook), the director MCP server, a SessionStart hook (start the
daemon, summarise what needs the director), a PostToolUse hook (presence), and an optional feed monitor that
starts when the skill is first used.
"""
import json
import os
import shutil
import sys

from . import __version__, fsutil

DATA = os.path.join(os.path.dirname(__file__), "plugin_data")


def _cmd(home, *args):
    exe = sys.executable.replace("\\", "/")
    return f'"{exe}" -m imperium --home "{home.replace(chr(92), "/")}" ' + " ".join(args)


def write(target, home):
    target = os.path.abspath(target)
    os.makedirs(os.path.join(target, ".claude-plugin"), exist_ok=True)
    manifest = {
        "name": "imperium",
        "displayName": "Imperium",
        "version": __version__,
        "description": "Direct coding builders through Imperium: delivery with proof, an acknowledged event feed, "
                       "approvals, and rounds verified by trusted checks.",
        "author": {"name": "parnish007"},
        "repository": "https://github.com/parnish007/imperium",
        "license": "MIT",
        "keywords": ["agents", "orchestration", "verification", "opencode"],
    }
    fsutil.atomic_write(os.path.join(target, ".claude-plugin", "plugin.json"), json.dumps(manifest, indent=2) + "\n")
    skills = os.path.join(target, "skills")
    if os.path.isdir(skills):
        shutil.rmtree(skills)
    shutil.copytree(os.path.join(DATA, "skills"), skills)
    exe = sys.executable
    mcp = {"mcpServers": {"imperium": {"command": exe, "args": ["-m", "imperium", "--home", home, "mcp"]}}}
    fsutil.atomic_write(os.path.join(target, ".mcp.json"), json.dumps(mcp, indent=2) + "\n")
    hooks = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": _cmd(home, "hook", "session-start"),
                                     "timeout": 30}]}],
        "PostToolUse": [{"hooks": [{"type": "command", "command": _cmd(home, "hook", "post-tool-use"),
                                    "timeout": 10}]}],
    }}
    os.makedirs(os.path.join(target, "hooks"), exist_ok=True)
    fsutil.atomic_write(os.path.join(target, "hooks", "hooks.json"), json.dumps(hooks, indent=2) + "\n")
    monitors = [{"name": "imperium-feed", "command": _cmd(home, "watch", "--consumer", "director"),
                 "description": "Imperium events that need the director", "when": "on-skill-invoke:imperium"}]
    os.makedirs(os.path.join(target, "monitors"), exist_ok=True)
    fsutil.atomic_write(os.path.join(target, "monitors", "monitors.json"), json.dumps(monitors, indent=2) + "\n")
    return {"plugin": target, "files": [".claude-plugin/plugin.json", "skills/imperium/SKILL.md", ".mcp.json",
                                        "hooks/hooks.json", "monitors/monitors.json"],
            "next": f"claude --plugin-dir \"{target}\"  (or add it to a local marketplace); then, in the session "
                    "that will direct: imperium --as owner director claim"}
