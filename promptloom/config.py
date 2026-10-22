"""Configuration: built-in defaults overlaid with ~/.config/promptloom/config.toml."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "promptloom"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "promptloom"
CONFIG_FILE = CONFIG_DIR / "config.toml"
PLUGIN_DIR = CONFIG_DIR / "harnesses"
PROVIDER_FILE = CONFIG_DIR / "provider.toml"  # written by `promptloom provider`
SETTINGS_FILE = CONFIG_DIR / "settings.toml"  # switches flipped in the app; override config.toml

DEFAULT_TOML = """\
# promptloom configuration. Every value shown is the default.

vault = ""                  # Obsidian vault root, e.g. "~/Obsidian"; "" writes no vault
folder = "Promptloom"       # folder inside the vault that promptloom owns
poll_seconds = 5            # how often the daemon looks for new prompts
active_minutes = 30         # a session counts as "active" in the top bar this long
insights_hours = 6          # minimum gap between automatic Insights refreshes
ignore_contexts = []        # working directories whose sessions are never captured

[ui]
# The built-in app: search, tags, notes, the graph and project timelines.
# `promptloom ui` opens it in the browser, `promptloom app` in a window of its
# own. It listens on this machine only.
enabled = true
port = 8765
window = "auto"             # "auto", "desktop" (the Promptloom app), "webview", "chrome", "browser"
tray = true                 # Linux: an icon in the top bar (needs python3-gi; no log-out needed)

# How to open a terminal window: the command, then the program to run in it.
# [] finds one (ptyxis, gnome-terminal, kgx, konsole, kitty, …; Terminal.app;
# Windows Terminal or cmd).
terminal = []

[tools]
# Tools "New prompt" can start. Built in: claude, codex, copilot, hermes, pi,
# openclaw, vscode, cursor; only the installed ones are offered. {prompt} is
# the prompt, {prompt_file} a file holding it, {dir} the project; "model" is
# added after the program when you pick one. Add your own or change the flags:
# [tools.agents.aider]
# label = "Aider"
# harness = "aider"
# cmd = ["aider", "--message", "{prompt}"]
# model = ["--model", "{model}"]
# models = ["gpt-6.1-sol"]

[prs]
# Pull requests: yours and others' in your repositories, per project; reviews
# and fixes by a coding agent; a notification when someone comments on yours.
# Needs the GitHub CLI (gh), signed in. The switch in the app and
# `promptloom pr on|off` turn it on and off.
enabled = false
notify = true               # notify when someone comments on one of your pull requests
poll_minutes = 5
agent = "claude"            # default for reviews and fixes: "claude", "codex" or "copilot"
worktrees = true            # fixes work in <checkout>/.worktrees/<branch>, not the checkout
roots = []                  # folders holding your checkouts; [] = the parents of your projects
repos = []                  # more "owner/name" repositories to show others' pull requests from
review_timeout_minutes = 20
terminal = []               # how to open a terminal, e.g. ["gnome-terminal", "--"]; [] = find one
# Agent commands: {prompt} is replaced by the prompt (otherwise it goes to stdin)
# and {output} by a file the answer is read from (otherwise stdout is read).
# [prs.agents.claude]
# review = ["claude", "-p", "--model", "opus"]
# fix = ["claude", "{prompt}"]

[glean]
# Search your company's knowledge from promptloom: `promptloom glean login`
# (browser sign-in), then `promptloom glean QUERY`, the app's /api/glean and the
# MCP tool glean_search. `glean set --url X --token T` uses an API token instead.
url = ""                    # "" = the one the Glean desktop app is signed in to; or "acme", a full URL
auth_type = ""              # "OAUTH" for user-scoped OAuth tokens
actas = ""                  # email to search as, for global API tokens

[enrich]
# `promptloom provider set <name>` switches summaries to a hosted provider with
# your API key and overrides backend, provider and model here.
backend = "ollama"          # "ollama", "api", "command" or "none"
model = "qwen3.8:27b"
url = "http://localhost:11434"
keep_alive = "2m"           # how long ollama keeps the model in VRAM afterwards
idle_seconds = 60           # first summary once a session has been quiet this long
refresh_idle_seconds = 900  # redo the summary of a session that grew, once quiet this long
min_free_vram_mib = 19000   # free VRAM needed to load the model onto the GPU
gpu_busy = "cpu"            # with less VRAM free: "cpu" runs on CPU (slow), "wait" defers
timeout_seconds = 600
# options = { num_ctx = 8192 }   # extra ollama options
# backend = "command": the prompt goes to stdin, a JSON object is read from stdout.
command = ["claude", "-p"]

# Harnesses. Built-in types: claude-code, codex, hermes, jsonl (generic).
# A harness whose files do not exist is simply skipped.
[harness.claude-code]
enabled = true
# root = "~/.claude/projects"

[harness.codex]
enabled = true
# root = "~/.codex/sessions"

[harness.copilot]
enabled = true              # GitHub Copilot Chat in VS Code
# root = "~/.config/Code/User/workspaceStorage"

[harness.copilot-cli]
enabled = true              # GitHub Copilot CLI
# root = "~/.copilot/session-state"

[harness.cursor]
enabled = true              # Cursor agent chats
# root = "~/.cursor/projects"

[harness.hermes]
enabled = true
# db = "~/.hermes/state.db"

# Prompts other tools send with `promptloom log` or POST /api/ingest.
[harness.api]
enabled = true

# Generic adapter for any harness that writes one JSON object per line.
# Paths are dotted ("message.content"); "@stem" means the file name.
# [harness.deepseek]
# type = "jsonl"
# label = "DeepSeek"
# glob = "~/.deepseek/sessions/*.jsonl"
# session_id = "@stem"
# role = "role"
# user_value = "user"
# assistant_value = "assistant"
# text = "content"
# timestamp = "timestamp"
# cwd = "cwd"
# title = "title"
# model = "model"
"""


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path | None = None, provider_path: Path | None = None) -> dict:
    provider_path = provider_path or (PROVIDER_FILE if path is None else None)
    path = path or CONFIG_FILE
    cfg = tomllib.loads(DEFAULT_TOML)
    if path.exists():
        cfg = _merge(cfg, tomllib.loads(path.read_text()))
    if path == CONFIG_FILE and SETTINGS_FILE.exists():
        cfg = _merge(cfg, tomllib.loads(SETTINGS_FILE.read_text()))
    cfg["keys"] = {}
    if provider_path and provider_path.exists():
        chosen = tomllib.loads(provider_path.read_text())
        cfg["keys"] = chosen.get("keys", {})
        if chosen.get("provider"):
            cfg["enrich"]["backend"] = "api"
            for key in ("provider", "model", "base_url"):
                cfg["enrich"][key] = chosen.get(key, "")
    cfg["vault"] = str(Path(cfg["vault"]).expanduser()) if cfg["vault"] else ""
    cfg["ignore_contexts"] = [str(Path(p).expanduser()) for p in cfg["ignore_contexts"]]
    return cfg


def save_settings(section: str, values: dict, path: Path | None = None) -> dict:
    """Change switches in settings.toml, keeping the rest. Returns the section."""
    path = path or SETTINGS_FILE
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data.setdefault(section, {}).update(values)
    lines = ["# Written by promptloom when you flip a switch in the app; overrides config.toml."]
    for name, table in data.items():
        lines.append(f"\n[{name}]")
        lines += [f"{key} = {json.dumps(value)}" for key, value in table.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return data[section]


def write_default() -> bool:
    """Create the config file if missing. Returns True when it was created."""
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(DEFAULT_TOML)
    return True
