"""Model-written titles, summaries, themes and action items; cross-session insights."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

from .model import Session

PROMPT_BUDGET = 12000
PROMPT_CLIP = 1500

SESSION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "themes", "action_items"],
}

INSIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "sessions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "sessions"],
            },
        },
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "sessions": {"type": "array", "items": {"type": "string"}},
                    "prompt": {"type": "string"},
                },
                "required": ["title", "detail", "sessions", "prompt"],
            },
        },
    },
    "required": ["themes", "opportunities"],
}

SESSION_INSTRUCTIONS = """\
You are indexing one person's conversations with AI coding assistants so they can \
find and connect them later. Below are the prompts the person typed in a single \
session. Reply with a JSON object:

- "title": 3 to 7 words naming what the session was about. Specific, no quotes, no \
trailing period.
- "summary": one or two sentences on what the person was trying to do, and the \
outcome if it is evident. Start with a verb or the subject matter; do not write \
"the user" or "the person".
- "themes": 2 to 4 broad, reusable topic labels, lowercase, 1 to 3 words each. A \
theme should plausibly apply to other sessions too, so reuse a known theme whenever \
one fits and only invent a new one when none does.
- "action_items": concrete follow-ups the person still has to do themselves: things \
they said they would do later, open questions, steps left unfinished. Use an empty \
list when there are none. Do not repeat items that are already recorded.
"""

INSIGHTS_INSTRUCTIONS = """\
Below is an index of one person's sessions with AI coding assistants, across several \
projects and tools. Each line is: id | project | directory | harness | title | themes | \
summary.

Find what connects them. Reply with a JSON object:

- "themes": the overarching themes that run through several sessions. For each give \
a short "name", a one or two sentence "description", and the "sessions" ids it covers.
- "opportunities": specific chances to share knowledge between sessions or projects: \
a solution found in one place that applies to another, repeated work that could \
become a shared tool or note, ideas that combine. For each give a short "title", a \
"detail" of two or three sentences saying what to do and why, the "sessions" ids \
involved, and a "prompt": what the person could paste into a coding assistant started \
in one of those directories to act on it, written in the first person, naming the other \
directories so the assistant can read them. Only include opportunities that span at \
least two sessions. Look hardest for links between projects that seem unrelated.

Use only ids that appear in the index.
"""


class EnrichError(Exception):
    pass


def _clip(text: str, limit: int = PROMPT_CLIP) -> str:
    return text if len(text) <= limit else text[:limit] + " […]"


def session_prompt(session: Session, known_themes: list[str], recorded: list[str]) -> str:
    texts = [_clip(p.text) for p in session.prompts]
    head = texts[:3]
    tail: list[str] = []
    used = sum(len(t) for t in head)
    for text in reversed(texts[3:]):
        if used + len(text) > PROMPT_BUDGET:
            break
        tail.insert(0, text)
        used += len(text)
    omitted = len(texts) - len(head) - len(tail)

    parts = [SESSION_INSTRUCTIONS]
    if known_themes:
        parts.append("Known themes: " + ", ".join(known_themes))
    if recorded:
        parts.append("Already recorded action items:\n" + "\n".join(f"- {a}" for a in recorded))
    parts.append(f"Harness: {session.harness}\nWorking directory: {session.context or 'unknown'}")
    parts.append("Prompts, oldest first:")
    for i, text in enumerate(head, 1):
        parts.append(f"[{i}] {text}")
    if omitted:
        parts.append(f"[… {omitted} prompts omitted …]")
    for i, text in enumerate(tail, len(texts) - len(tail) + 1):
        parts.append(f"[{i}] {text}")
    if session.last_reply:
        parts.append("The assistant's most recent reply ended:\n" + _clip(session.last_reply, 800))
    return "\n\n".join(parts)


def insights_prompt(lines: list[str], hints: list[str] | None = None) -> str:
    text = INSIGHTS_INSTRUCTIONS + "\n" + "\n".join(lines)
    if hints:
        text += ("\n\nSessions in different projects whose prompts share unusual words, "
                 "which no theme connects. Some of these may be worth an opportunity:\n"
                 + "\n".join(hints))
    return text


def _json_from(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise EnrichError("model returned no JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise EnrichError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EnrichError("model returned JSON that is not an object")
    return data


def _free_vram_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        total, used = (int(x) for x in out.splitlines()[0].split(","))
        return total - used
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


class Ollama:
    name = "ollama"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url = cfg["url"].rstrip("/")
        self.model = cfg["model"]
        self._think = True  # send "think": false until the server objects
        self._cpu = False  # run this load of the model without the GPU

    def _call(self, path: str, body: dict | None = None, timeout: float = 5) -> dict:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise EnrichError(f"ollama {exc.code}: {detail}") from exc
        except (OSError, ValueError) as exc:
            raise EnrichError(f"ollama unreachable: {exc}") from exc

    def ready(self) -> str:
        """Empty string when a request can go ahead, else the reason to wait."""
        try:
            loaded = [m.get("name") for m in self._call("/api/ps").get("models", [])]
        except EnrichError as exc:
            return str(exc)
        if self.model in loaded:
            return ""
        # Loading the model would compete with whatever else is using the GPU.
        free = _free_vram_mib()
        need = self.cfg.get("min_free_vram_mib", 0)
        self._cpu = False
        if free is not None and free < need:
            if self.cfg.get("gpu_busy", "cpu") != "cpu":
                return f"GPU busy ({free} MiB free, {need} needed to load {self.model})"
            self._cpu = True
        return ""

    def complete(self, prompt: str, schema: dict) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": schema,
            "keep_alive": self.cfg.get("keep_alive", "2m"),
            "options": {"temperature": 0.2, "num_ctx": 16384, **self.cfg.get("options", {})},
        }
        if self._cpu:
            body["options"]["num_gpu"] = 0
        if self._think:
            body["think"] = False
        try:
            reply = self._call("/api/chat", body, self.cfg.get("timeout_seconds", 180))
        except EnrichError as exc:
            if self._think and "think" in str(exc).lower():
                self._think = False
                return self.complete(prompt, schema)
            raise
        return _json_from(reply.get("message", {}).get("content", ""))


class Command:
    """Any CLI that reads a prompt on stdin and prints JSON, e.g. `claude -p`."""

    name = "command"

    def __init__(self, cfg: dict, workdir: str):
        self.cfg = cfg
        self.workdir = workdir

    def ready(self) -> str:
        return ""

    def complete(self, prompt: str, schema: dict) -> dict:
        prompt += "\n\nReply with only a JSON object matching this schema:\n" + json.dumps(schema)
        try:
            result = subprocess.run(
                self.cfg["command"], input=prompt, capture_output=True, text=True,
                timeout=self.cfg.get("timeout_seconds", 180), cwd=self.workdir,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EnrichError(f"command failed: {exc}") from exc
        if result.returncode != 0:
            raise EnrichError(f"command exited {result.returncode}: {result.stderr[:300]}")
        return _json_from(result.stdout)


def backend(cfg: dict, workdir: str):
    kind = cfg["enrich"].get("backend", "none")
    if kind == "ollama":
        return Ollama(cfg["enrich"])
    if kind == "command":
        return Command(cfg["enrich"], workdir)
    if kind == "api":
        from . import providers

        return providers.build(cfg)
    return None


def clean_themes(raw) -> list[str]:
    themes = []
    for theme in raw if isinstance(raw, list) else []:
        if not isinstance(theme, str):
            continue
        theme = " ".join(theme.lower().replace("_", " ").replace("/", " ").split())
        theme = theme.strip(" .#")
        if theme and len(theme) <= 40 and theme not in themes:
            themes.append(theme)
    return themes[:4]
