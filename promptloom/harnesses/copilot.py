"""GitHub Copilot Chat in VS Code:
<user data>/User/workspaceStorage/<hash>/chatSessions/<session-id>.json[l]

Each workspace directory has a workspace.json naming the folder it belongs to.
Older VS Code versions write a session as one JSON document; newer ones append
a log of changes (.jsonl): a full snapshot (kind 0), then values set (kind 1)
or appended to lists (kind 2) at a key path. Both are replayed into one
document and read whole, so a session's prompts are rebuilt on every change.

Written against the documented storage on synthetic files; not yet run
against a real VS Code install.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..extract import norm_ts, urls
from .base import Harness

_EDITIONS = ("Code", "Code - Insiders", "VSCodium")


def default_roots() -> list[Path]:
    if sys.platform == "darwin":
        base = Path("~/Library/Application Support").expanduser()
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser()
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return [base / edition / "User" / "workspaceStorage" for edition in _EDITIONS]


def replay(lines: list[str]) -> dict:
    """Rebuild a session document from VS Code's append-only change log."""
    doc: dict = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind, keys, value = entry.get("kind"), entry.get("k") or [], entry.get("v")
        if kind == 0:
            doc = value if isinstance(value, dict) else {}
            continue
        if not keys:
            continue
        parent = doc
        try:
            for key in keys[:-1]:
                parent = parent[key]
            last = keys[-1]
            if kind == 1:
                parent[last] = value
            elif kind == 2:
                target = parent.setdefault(last, []) if isinstance(parent, dict) else parent[last]
                if isinstance(entry.get("i"), int):
                    del target[entry["i"]:]  # the log truncates before appending
                target.extend(value if isinstance(value, list) else [value])
        except (KeyError, IndexError, TypeError):
            continue
    return doc


def folder_of(workspace_dir: Path) -> str:
    try:
        data = json.loads((workspace_dir / "workspace.json").read_text())
    except (OSError, ValueError):
        return ""
    uri = data.get("folder") or data.get("workspace") or ""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    path = unquote(parsed.path)
    if sys.platform == "win32" and path[:1] == "/" and path[2:3] == ":":
        path = path[1:]
    return path


def response_text(response) -> str:
    parts = []
    for item in response if isinstance(response, list) else []:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            parts.append(item["value"])
    return "".join(parts)


class Copilot(Harness):
    type = "copilot"
    label = "Copilot (VS Code)"

    def roots(self) -> list[Path]:
        if self.cfg.get("root"):
            return [Path(self.cfg["root"]).expanduser()]
        return default_roots()

    def sources(self):
        for root in self.roots():
            yield from root.glob("*/chatSessions/*.json")
            yield from root.glob("*/chatSessions/*.jsonl")

    def read(self, path, cursor, get):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".jsonl":
            doc = replay(text.splitlines())
        else:
            try:
                doc = json.loads(text)
            except ValueError:
                return set()
        if not isinstance(doc, dict):
            return set()
        sid = str(doc.get("sessionId") or path.stem)
        requests = [r for r in doc.get("requests") or [] if isinstance(r, dict)]
        if not requests:
            return set()
        session = get(sid)
        session.prompts.clear()  # the whole document is re-read each time
        session.context = folder_of(path.parent.parent) or session.context
        if doc.get("customTitle"):
            session.title = str(doc["customTitle"])
        session.touch(norm_ts(doc.get("creationDate")))
        for request in requests:
            message = request.get("message") or {}
            prompt = message.get("text") if isinstance(message, dict) else message
            ts = norm_ts(request.get("timestamp"))
            model = str(request.get("modelId") or "").removeprefix("copilot/")
            session.set_model(model)
            if isinstance(prompt, str):
                session.add_prompt(ts, prompt)
            reply = response_text(request.get("response"))
            if reply or model:
                session.add_reply(ts, reply, urls(reply), model)
        session.touch(norm_ts(doc.get("lastMessageDate")))
        return {sid}
