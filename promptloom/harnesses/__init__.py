"""Harness registry: built-in adapters plus plugins from the config directory."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .api import Api
from .base import Harness, JsonlHarness
from .claude_code import ClaudeCode
from .codex import Codex
from .copilot import Copilot
from .generic import GenericJsonl
from .hermes import Hermes

__all__ = ["Harness", "JsonlHarness", "build", "types"]

_BUILTIN = [ClaudeCode, Codex, Copilot, Hermes, GenericJsonl, Api]


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def load_plugins(plugin_dir: Path) -> list[str]:
    """Import every .py file in the plugin directory. Returns error messages."""
    errors = []
    if not plugin_dir.is_dir():
        return errors
    for file in sorted(plugin_dir.glob("*.py")):
        name = f"promptloom_plugin_{file.stem}"
        if name in sys.modules:
            continue
        try:
            spec = importlib.util.spec_from_file_location(name, file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # a broken plugin must not stop the daemon
            sys.modules.pop(name, None)
            errors.append(f"{file.name}: {exc}")
    return errors


def types() -> dict[str, type[Harness]]:
    return {cls.type: cls for cls in _subclasses(Harness) if cls.type}


def build(cfg: dict, plugin_dir: Path) -> tuple[list[Harness], list[str]]:
    """Instantiate every enabled harness in the config."""
    errors = load_plugins(plugin_dir)
    known = types()
    harnesses = []
    entries = dict(cfg.get("harness", {}))
    # A plugin needs no config entry: its type doubles as a harness name.
    configured = {hcfg.get("type", name) for name, hcfg in entries.items()}
    for kind, cls in known.items():
        if cls.__module__.startswith("promptloom_plugin_") and kind not in configured:
            entries[kind] = {}
    for name, hcfg in entries.items():
        if not hcfg.get("enabled", True):
            continue
        kind = hcfg.get("type", name)
        if kind not in known:
            errors.append(f"harness '{name}': unknown type '{kind}'")
            continue
        try:
            harnesses.append(known[kind](name, hcfg))
        except Exception as exc:
            errors.append(f"harness '{name}': {exc}")
    return harnesses, errors
