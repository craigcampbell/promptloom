"""Starting a prompt in one of your tools, and weaving a draft into a better one.

A tool is a program that takes a prompt on its command line (Claude Code,
Codex, the Copilot CLI, Hermes, pi, OpenClaw and whatever you add in config).
Promptloom opens a terminal, goes to the project and starts the tool with the
prompt and the model you chose; the tool's own session log then brings the
prompt back into your history as usual. A tool without a command line (VS
Code, Cursor) is opened on the project with the prompt on the clipboard.

Weaving sends a draft to a model you already use for summaries (local Ollama,
or a hosted provider with a stored key) and asks for a stronger prompt back.
"""

from __future__ import annotations

import shlex
import sys
import time
from collections import Counter
from pathlib import Path

from . import enrich, extract
from .desktop import (DesktopError, copy_to_clipboard, open_terminal, search_path, spawn,
                      terminal, which)

# {prompt} is the prompt, {prompt_file} a file holding it, {dir} the project.
# "model" is spliced in after the program when a model was chosen.
TOOLS = {
    "claude": {"label": "Claude Code", "harness": "claude-code",
               "cmd": ["claude", "{prompt}"], "model": ["--model", "{model}"],
               "models": ["opus", "sonnet", "haiku", "claude-fable-5-1", "claude-opus-5-5",
                          "claude-sonnet-5-5", "claude-haiku-5-5"]},
    "codex": {"label": "Codex", "harness": "codex",
              "cmd": ["codex", "{prompt}"], "model": ["-m", "{model}"],
              "models": ["gpt-6.1-sol", "gpt-6-astra", "gpt-5.5", "o5"]},
    "copilot": {"label": "Copilot CLI", "harness": "copilot",
                "cmd": ["copilot", "-i", "{prompt}"], "model": ["--model", "{model}"],
                "models": ["claude-opus-5-5", "claude-sonnet-5-5", "gpt-6.1-sol", "gpt-5.5"]},
    "hermes": {"label": "Hermes", "harness": "hermes",
               "cmd": ["hermes", "chat", "--query-file", "{prompt_file}"],
               "model": ["-m", "{model}"],
               "models": ["anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5",
                          "openai/gpt-6.1-sol", "deepseek/deepseek-v4"]},
    "pi": {"label": "pi", "harness": "pi",
           "cmd": ["pi", "{prompt}"], "model": ["--model", "{model}"],
           "models": ["anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5",
                      "openai/gpt-6.1-sol"]},
    "openclaw": {"label": "OpenClaw", "harness": "openclaw",
                 "cmd": ["openclaw", "agent", "--message", "{prompt}"],
                 "model": ["--model", "{model}"], "models": []},
    "vscode": {"label": "VS Code (Copilot)", "harness": "copilot", "gui": True,
               "cmd": ["code", "{dir}"], "models": []},
    "cursor": {"label": "Cursor", "harness": "cursor", "gui": True,
               "cmd": ["cursor", "{dir}"], "models": []},
}
MAX_MODELS = 12


class LaunchError(Exception):
    pass


def settings(cfg: dict) -> dict:
    return cfg.get("tools", {})


def tools(cfg: dict, records=()) -> list[dict]:
    """Every tool, installed or not, with the models it has been seen using."""
    custom = settings(cfg).get("agents", {})
    seen: dict[str, Counter] = {}
    for record in records:
        counts = seen.setdefault(record.session.harness, Counter())
        counts.update(p.model for p in record.session.prompts if p.model)
    out = []
    for name in [*TOOLS, *(n for n in custom if n not in TOOLS)]:
        spec = {**TOOLS.get(name, {"label": name, "harness": name, "models": []}),
                **custom.get(name, {})}
        program = spec.get("cmd", [""])[0]
        used = [m for m, _ in seen.get(spec["harness"], Counter()).most_common()]
        models = list(dict.fromkeys(used + list(spec.get("models", []))))[:MAX_MODELS]
        out.append({"name": name, "label": spec["label"], "harness": spec["harness"],
                    "gui": bool(spec.get("gui")), "available": bool(program and which(program)),
                    "program": program, "models": models, "used": used[:1],
                    "takes_model": bool(spec.get("model"))})
    return out


def tool(cfg: dict, name: str) -> dict:
    custom = settings(cfg).get("agents", {})
    if name not in TOOLS and name not in custom:
        raise LaunchError(f"unknown tool {name!r}")
    spec = {**TOOLS.get(name, {"label": name, "harness": name}), **custom.get(name, {})}
    program = spec.get("cmd", [""])[0]
    if not program or not which(program):
        raise LaunchError(f"{spec['label']} is not installed (no {program!r} on PATH)")
    return spec


def command(spec: dict, prompt: str, model: str, directory: str, prompt_file: str) -> list[str]:
    """The argv that starts the tool with the prompt."""
    argv = []
    for i, part in enumerate(spec["cmd"]):
        if "{prompt_file}" in part:
            argv.append(part.replace("{prompt_file}", prompt_file))
        elif "{prompt}" in part:
            argv.append(part.replace("{prompt}", prompt))
        else:
            argv.append(part.replace("{dir}", directory))
        if i == 0 and model and spec.get("model"):
            argv += [m.replace("{model}", model) for m in spec["model"]]
    return argv


def script(spec: dict, prompt_file: Path, model: str, directory: str) -> str:
    """The shell script a terminal runs: go to the project, start the tool."""
    q = shlex.quote
    argv = command(spec, '"$(cat ' + q(str(prompt_file)) + ')"', model, directory,
                   str(prompt_file))
    words = []
    for arg in argv:
        if arg.startswith('"$(cat '):
            words.append(arg)
        else:
            words.append(q(arg))
    lines = ["#!/bin/sh", "# Written by promptloom to start one prompt; safe to delete.",
             f"export PATH={q(search_path())}",
             f"cd {q(directory)} || exit 1",
             " ".join(words),
             "echo; echo 'promptloom: the tool has exited; this terminal stays open.'",
             'exec "${SHELL:-/bin/sh}"']
    return "\n".join(lines) + "\n"


def start(cfg: dict, state_dir: Path, name: str, prompt: str, model: str = "",
          directory: str = "") -> dict:
    """Open the tool on the prompt. Returns what happened, for the page to say."""
    prompt = prompt.strip()
    if not prompt:
        raise LaunchError("the prompt is empty")
    spec = tool(cfg, name)
    directory = str(Path(directory).expanduser()) if directory else str(Path.home())
    if not Path(directory).is_dir():
        raise LaunchError(f"{directory} is not a folder")
    folder = state_dir / "launches"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time())}-{extract.item_id(prompt)}"
    prompt_file = folder / f"{stamp}.prompt"
    prompt_file.write_text(prompt)
    if spec.get("gui"):
        copied = copy_to_clipboard(prompt)
        spawn(command(spec, prompt, model, directory, str(prompt_file)), cwd=directory)
        return {"tool": spec["label"], "dir": directory, "how": "gui", "copied": copied}
    if sys.platform == "win32":
        argv = command(spec, prompt, model, directory, str(prompt_file))
        spawn([*terminal(cfg), *argv], cwd=directory)
        return {"tool": spec["label"], "dir": directory, "how": "terminal"}
    runner = folder / f"{stamp}.sh"
    runner.write_text(script(spec, prompt_file, model, directory))
    runner.chmod(0o700)
    try:
        spawn([*terminal(cfg), str(runner)], cwd=directory)
    except DesktopError as exc:
        raise LaunchError(str(exc)) from exc
    return {"tool": spec["label"], "dir": directory, "how": "terminal"}


# weaving: a draft becomes a better prompt

WEAVE_SCHEMA = {
    "type": "object",
    "properties": {"prompt": {"type": "string"}, "notes": {"type": "string"}},
    "required": ["prompt", "notes"],
}

WEAVE_INSTRUCTIONS = """\
Someone is about to give the prompt below to an AI coding assistant ({tool}) in the \
project {project}. Rewrite it into the prompt they should give instead: one that gets \
the result they want in a single try.

Keep their intent, their words for things, and every detail they gave. Add what the \
assistant will need and they left out: the goal behind the task, what "done" looks like, \
constraints, what must not change, how to check the work. Where something is ambiguous \
and you cannot know the answer, have the prompt tell the assistant to ask before acting \
rather than guessing. Do not pad it: a short clear prompt beats a long one. Write it in \
the first person, as they would. Do not address them; the result is the prompt itself.

Reply with a JSON object: "prompt", the rewritten prompt, and "notes", one to three \
sentences on what you changed and why, or what they should still decide.
"""


def weavers(cfg: dict) -> list[dict]:
    """Models a draft can be woven with: local Ollama models, then stored providers."""
    out = []
    enrich_cfg = cfg.get("enrich", {})
    if enrich_cfg.get("url"):
        ollama = enrich.Ollama({**enrich_cfg})
        try:
            names = [m.get("name", "") for m in ollama._call("/api/tags", timeout=2)
                     .get("models", [])]
        except enrich.EnrichError:
            names = []
        for name in names:
            out.append({"id": f"ollama:{name}", "label": f"{name} (local)",
                        "default": name == enrich_cfg.get("model")})
    for provider, key in (cfg.get("keys") or {}).items():
        if not key:
            continue
        from . import providers

        model = enrich_cfg.get("model", "") if enrich_cfg.get("provider") == provider else ""
        if provider == "anthropic":
            model = model or providers.ANTHROPIC_DEFAULT_MODEL
        out.append({"id": f"api:{provider}:{model}", "label": f"{providers.label(provider)}"
                    + (f" · {model}" if model else ""), "provider": provider, "model": model,
                    "default": enrich_cfg.get("backend") == "api"
                    and enrich_cfg.get("provider") == provider})
    if enrich_cfg.get("backend") == "command":
        argv = list(enrich_cfg.get("command", []))
        out.append({"id": "command", "label": " ".join([Path(argv[0]).name, *argv[1:]])
                    if argv else "command", "default": True})
    if not any(w["default"] for w in out) and out:
        out[0]["default"] = True
    return out


def weaver(cfg: dict, choice: str, workdir: str):
    """A backend for the chosen weaver id."""
    kind, _, rest = choice.partition(":")
    enrich_cfg = dict(cfg.get("enrich", {}))
    if kind == "ollama" and rest:
        return enrich.Ollama({**enrich_cfg, "model": rest})
    if kind == "api":
        provider, _, model = rest.partition(":")
        from . import providers

        overlay = {**cfg, "enrich": {**enrich_cfg, "backend": "api", "provider": provider}}
        if model:
            overlay["enrich"]["model"] = model
        elif enrich_cfg.get("provider") != provider:
            overlay["enrich"]["model"] = ""
        return providers.build(overlay)
    if kind == "command":
        return enrich.Command(enrich_cfg, workdir)
    raise LaunchError(f"unknown model choice {choice!r}")


def weave_prompt(draft: str, tool_label: str, project: str, context: list[str]) -> str:
    text = WEAVE_INSTRUCTIONS.format(tool=tool_label or "a coding assistant",
                                     project=project or "they are in")
    if context:
        text += "\nWhat they have done in this project lately:\n" + "\n".join(
            f"- {line}" for line in context[:8])
    return text + "\nThe prompt:\n\n" + draft.strip()


def weave(cfg: dict, workdir: str, choice: str, draft: str, tool_label: str = "",
          project: str = "", context: list[str] | None = None) -> dict:
    """Ask the chosen model for a stronger prompt. Returns {"prompt", "notes"}."""
    if not draft.strip():
        raise LaunchError("nothing to weave yet")
    try:
        backend = weaver(cfg, choice, workdir)
        waiting = backend.ready()
        if waiting:
            raise LaunchError(waiting)
        data = backend.complete(weave_prompt(draft, tool_label, project, context or []),
                                WEAVE_SCHEMA)
    except enrich.EnrichError as exc:
        raise LaunchError(str(exc)) from exc
    prompt = data.get("prompt") if isinstance(data, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise LaunchError("the model returned no prompt")
    notes = data.get("notes") if isinstance(data.get("notes"), str) else ""
    return {"prompt": prompt.strip(), "notes": " ".join(notes.split())}


def recent_context(records, directory: str, limit: int = 6) -> list[str]:
    """Titles and summaries of recent sessions in the folder, for the weaver."""
    lines = []
    for record in records:
        if record.session.context != directory:
            continue
        line = record.title + (f": {record.summary}" if record.summary else "")
        lines.append(line[:300])
        if len(lines) == limit:
            break
    return lines


def open_project(cfg: dict, directory: str) -> None:
    """Just a terminal in the folder (the "no tool" choice)."""
    open_terminal(cfg, directory)
