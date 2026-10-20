"""Configuration: built-in defaults overlaid with ~/.config/promptloom/config.toml."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "promptloom"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "promptloom"
CONFIG_FILE = CONFIG_DIR / "config.toml"
PLUGIN_DIR = CONFIG_DIR / "harnesses"
PROVIDER_FILE = CONFIG_DIR / "provider.toml"  # written by `promptloom provider`

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
# `promptloom ui` opens it. It listens on this machine only.
enabled = true
port = 8765

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


def write_default() -> bool:
    """Create the config file if missing. Returns True when it was created."""
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(DEFAULT_TOML)
    return True
