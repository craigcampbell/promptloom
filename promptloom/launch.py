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
from urllib.parse import urlencode

from . import enrich, extract
from .desktop import (DesktopError, copy_to_clipboard, open_terminal, open_uri, search_path,
                      spawn, terminal, which)

URL_PROMPT_CHARS = 1800  # longer prompts go on the clipboard instead of in the URL

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
    # Desktop apps and web apps take the prompt in a URL. "needs" is a program
    # that must be installed for the entry to be offered; without one, the web
    # version is always there.
    # "needs": any of these programs or app bundles present means the app is installed.
    "claude-desktop": {"label": "Claude (desktop app)", "harness": "claude-desktop",
                       "url": "claude://claude.ai/new", "params": {"q": "{prompt}"},
                       "needs": ["claude-desktop", "/Applications/Claude.app",
                                 "~/AppData/Local/AnthropicClaude/claude.exe"], "models": []},
    "claude-web": {"label": "Claude (claude.ai)", "harness": "claude-web",
                   "url": "https://claude.ai/new", "params": {"q": "{prompt}"}, "models": []},
    "chatgpt-desktop": {"label": "ChatGPT (desktop app)", "harness": "chatgpt",
                        "url": "https://chatgpt.com/", "params": {"q": "{prompt}", "model": "{model}"},
                        "needs": ["chatgpt", "/Applications/ChatGPT.app"],
                        "open_with": {"linux": ["chatgpt"], "darwin": ["open", "-a", "ChatGPT"]},
                        "models": ["gpt-5.5", "o5"]},
    "chatgpt": {"label": "ChatGPT (chatgpt.com)", "harness": "chatgpt",
                "url": "https://chatgpt.com/", "params": {"q": "{prompt}", "model": "{model}"},
                "models": ["gpt-5.5", "o5"]},
    "codex-web": {"label": "Codex (cloud, chatgpt.com)", "harness": "codex",
                  "url": "https://chatgpt.com/codex", "clipboard": True, "models": []},
    "vscode": {"label": "VS Code (Copilot)", "harness": "copilot", "gui": True,
               "cmd": ["code", "{dir}"], "models": []},
    "cursor": {"label": "Cursor", "harness": "cursor", "gui": True,
               "cmd": ["cursor", "{dir}"], "models": []},
}
MAX_MODELS = 12
# A cheap model per provider, for hints while typing.
CHEAP_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-5.5-mini",
                "deepseek": "deepseek-chat", "openrouter": "anthropic/claude-haiku-5-5",
                "groq": "llama-4-scout", "gemini": "gemini-3-flash"}


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
        if spec.get("url"):
            available = not spec.get("needs") or bool(installed(spec["needs"]))
            kind = "app" if spec.get("needs") else "web"
        else:
            available = bool(program and which(program))
            kind = "gui" if spec.get("gui") else "terminal"
        out.append({"name": name, "label": spec["label"], "harness": spec["harness"],
                    "kind": kind, "gui": kind in ("gui", "app", "web"), "available": available,
                    "program": installed(spec.get("needs")) or program, "models": models,
                    "used": used[:1],
                    "takes_model": bool(spec.get("model"))
                    or "{model}" in spec.get("params", {}).values(),
                    "clipboard": bool(spec.get("clipboard") or kind == "gui")})
    return out


def installed(needs) -> str:
    """The first of the programs or app bundles that is present, or ""."""
    for item in [needs] if isinstance(needs, str) else list(needs or []):
        path = Path(item).expanduser()
        if ("/" in item or "\\" in item) and path.exists():
            return str(path)
        if "/" not in item and which(item):
            return item
    return ""


def opener(spec: dict) -> list[str]:
    """How to hand a URL to a desktop app on this platform, else [] for the default."""
    open_with = spec.get("open_with")
    if isinstance(open_with, dict):
        key = "darwin" if sys.platform == "darwin" else "win32" if sys.platform == "win32" else "linux"
        open_with = open_with.get(key, [])
    argv = [open_with] if isinstance(open_with, str) else list(open_with or [])
    return argv if argv and which(argv[0]) else []


def tool(cfg: dict, name: str) -> dict:
    custom = settings(cfg).get("agents", {})
    if name not in TOOLS and name not in custom:
        raise LaunchError(f"unknown tool {name!r}")
    spec = {**TOOLS.get(name, {"label": name, "harness": name}), **custom.get(name, {})}
    if spec.get("url"):
        if spec.get("needs") and not installed(spec["needs"]):
            raise LaunchError(f"{spec['label']} is not installed")
        return spec
    program = spec.get("cmd", [""])[0]
    if not program or not which(program):
        raise LaunchError(f"{spec['label']} is not installed (no {program!r} on PATH)")
    return spec


def url_for(spec: dict, prompt: str, model: str) -> tuple[str, bool]:
    """The URL that opens a web or desktop app on the prompt, and whether the
    prompt had to go on the clipboard instead (too long, or the app has no way)."""
    via_clipboard = bool(spec.get("clipboard")) or len(prompt) > URL_PROMPT_CHARS
    values = {"{prompt}": "" if via_clipboard else prompt, "{model}": model}
    params = {k: values.get(v, v) for k, v in spec.get("params", {}).items()}
    params = {k: v for k, v in params.items() if v}
    return spec["url"] + ("?" + urlencode(params) if params else ""), via_clipboard


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
    if spec.get("url"):
        url, via_clipboard = url_for(spec, prompt, model)
        copied = copy_to_clipboard(prompt) if via_clipboard else False
        argv = opener(spec)
        if argv:
            spawn([*argv, url])
        else:
            open_uri(url)
        return {"tool": spec["label"], "dir": directory, "how": "url", "copied": copied,
                "via_clipboard": via_clipboard}
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
        cheap = CHEAP_MODELS.get(provider)
        if cheap and cheap != model:
            out.append({"id": f"api:{provider}:{cheap}", "label": f"{providers.label(provider)}"
                        f" · {cheap} (cheap)", "provider": provider, "model": cheap,
                        "default": False})
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


HINT_SCHEMA = {
    "type": "object",
    "properties": {
        "decide": {"type": "array", "items": {"type": "string"}},
        "add": {"type": "array", "items": {"type": "string"}},
        "watch": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decide", "add", "watch"],
}

HINT_INSTRUCTIONS = """\
Someone is typing a prompt for an AI coding assistant ({tool}) in the project {project}. It \
is not finished. Read what they have so far, with what is known about the project, and reply \
with a JSON object of short notes to show beside the text as they type, each under 15 words:

- "decide": questions the assistant would have to guess at unless the prompt answers them \
(at most 3).
- "add": details from the project context worth putting in the prompt: a rule they stated, \
something that broke before, an open item this touches (at most 3; only if truly relevant).
- "watch": one warning if the prompt asks for something risky or contradicts a stated rule, \
else an empty list.

Empty lists are fine. Never restate the prompt. Do not write the prompt for them.
"""


def hint_prompt(draft: str, tool_label: str, project: str, brief_text: str) -> str:
    text = HINT_INSTRUCTIONS.format(tool=tool_label or "a coding assistant",
                                    project=project or "they are in")
    if brief_text.strip():
        text += "\nWhat is known about the project:\n" + brief_text.strip()[:3000] + "\n"
    return text + "\nWhat they have typed so far:\n\n" + draft.strip()


def hints(cfg: dict, workdir: str, choice: str, draft: str, tool_label: str = "",
          project: str = "", brief_text: str = "") -> dict:
    """A cheap model's notes on a draft in progress: {"decide", "add", "watch"}."""
    if len(draft.strip()) < 12:
        return {"decide": [], "add": [], "watch": []}
    try:
        backend = weaver(cfg, choice, workdir)
        waiting = backend.ready()
        if waiting:
            raise LaunchError(waiting)
        data = backend.complete(hint_prompt(draft, tool_label, project, brief_text), HINT_SCHEMA)
    except enrich.EnrichError as exc:
        raise LaunchError(str(exc)) from exc
    out = {}
    for key in ("decide", "add", "watch"):
        items = data.get(key) if isinstance(data, dict) else None
        out[key] = [" ".join(i.split()) for i in items if isinstance(i, str) and i.strip()][:3] \
            if isinstance(items, list) else []
    return out


ORGANIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "actions": {"type": "array", "items": {
            "type": "object",
            "properties": {"op": {"type": "string", "enum": ["tag", "untag", "collect"]},
                           "ref": {"type": "string"}, "value": {"type": "string"},
                           "why": {"type": "string"}},
            "required": ["op", "ref", "value"]}},
    },
    "required": ["summary", "actions"],
}

ORGANIZE_INSTRUCTIONS = """\
You are tidying one person's history of prompts to AI coding assistants. Below is an index: \
each line is "ref | date | project | tool | tags | the prompt (cut short)". Follow the \
instruction, proposing changes only; a person approves each one.

Reply with a JSON object: "summary", one sentence on what you propose, and "actions", a \
list of {{"op", "ref", "value", "why"}} where op is "tag" (add the tag in value, lowercase, \
hyphens for spaces, no #), "untag" (take it away) or "collect" (put the prompt in the \
collection named in value, creating it if needed), ref is a ref from the index exactly as \
written, and why is a few words. Only propose what the instruction asks for. At most 60 \
actions; prefer the clearest cases.

Instruction: {instruction}
"""


def organize(cfg: dict, workdir: str, choice: str, instruction: str, rows: list[str]) -> dict:
    """Ask a model to propose tags and collections. Returns {"summary", "actions"}."""
    if not instruction.strip():
        raise LaunchError("say what to organise")
    if not rows:
        raise LaunchError("nothing in view to organise")
    prompt = ORGANIZE_INSTRUCTIONS.format(instruction=instruction.strip()) + "\n" + "\n".join(rows)
    try:
        backend = weaver(cfg, choice, workdir)
        waiting = backend.ready()
        if waiting:
            raise LaunchError(waiting)
        data = backend.complete(prompt, ORGANIZE_SCHEMA)
    except enrich.EnrichError as exc:
        raise LaunchError(str(exc)) from exc
    actions = []
    known = {row.split(" | ", 1)[0] for row in rows}
    for item in data.get("actions", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get("op") not in ("tag", "untag", "collect"):
            continue
        ref, value = str(item.get("ref", "")).strip(), str(item.get("value", "")).strip()
        if ref in known and value:
            actions.append({"op": item["op"], "ref": ref, "value": value,
                            "why": str(item.get("why", ""))[:120]})
    summary = data.get("summary") if isinstance(data, dict) else ""
    return {"summary": summary if isinstance(summary, str) else "", "actions": actions[:60]}


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
