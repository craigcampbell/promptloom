"""GitHub Copilot CLI: ~/.copilot/session-state/<session-id>/events.jsonl

One event per line; user.message holds what was typed, assistant.message the
reply and the model that wrote it, session.start and session.resume the folder
and branch. workspace.yaml beside it names the session.
"""

from __future__ import annotations

from pathlib import Path

from ..extract import norm_ts, urls
from .base import JsonlHarness


class CopilotCli(JsonlHarness):
    type = "copilot-cli"
    label = "Copilot CLI"
    resume = "copilot --resume={id}"

    def sources(self):
        root = Path(self.cfg.get("root", "~/.copilot/session-state")).expanduser()
        return root.glob("*/events.jsonl")

    def record(self, rec, path, cursor, get):
        kind = rec.get("type") or ""
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        sid = path.parent.name
        ts = norm_ts(rec.get("timestamp"))

        if kind in ("session.start", "session.resume"):
            session = get(sid)
            where = data.get("context") or {}
            session.context = where.get("cwd") or session.context
            session.branch = where.get("branch") or session.branch
            session.set_model(data.get("selectedModel") or "")
            if not session.title:
                session.title = self._name(path.parent / "workspace.yaml")
            session.touch(ts)
            return sid
        if kind == "session.model_change":
            get(sid).set_model(data.get("newModel") or "")
            return sid
        if kind == "user.message":
            get(sid).add_prompt(ts, str(data.get("content") or ""))
            return sid
        if kind == "assistant.message":
            text = str(data.get("content") or "")
            model = str(data.get("model") or "")
            if not text and not model:
                return None
            get(sid).add_reply(ts, text, urls(text), model)
            return sid
        return None

    @staticmethod
    def _name(file: Path) -> str:
        try:
            for line in file.read_text().splitlines():
                if line.startswith("name:"):
                    return line[5:].strip().strip("'\"")
        except OSError:
            pass
        return ""
