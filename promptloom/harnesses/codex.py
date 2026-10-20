"""OpenAI Codex CLI: ~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl"""

from __future__ import annotations

import re
from pathlib import Path

from ..extract import norm_ts, urls
from .base import JsonlHarness, text_of

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Codex(JsonlHarness):
    type = "codex"
    label = "Codex"
    resume = "codex resume {id}"

    def sources(self):
        root = Path(self.cfg.get("root", "~/.codex/sessions")).expanduser()
        return root.rglob("rollout-*.jsonl")

    def record(self, rec, path, cursor, get):
        kind = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else rec
        ts = norm_ts(rec.get("timestamp") or payload.get("timestamp"))

        # The first line names the session: {"type": "session_meta", "payload": {...}}
        # in current versions, a bare {"id": ..., "timestamp": ...} in older ones.
        if kind == "session_meta" or (kind is None and "id" in rec):
            cursor["sid"] = payload.get("id") or cursor.get("sid")
            session = get(self._sid(cursor, path))
            session.context = payload.get("cwd") or session.context
            git = payload.get("git")
            if isinstance(git, dict):
                session.branch = git.get("branch") or session.branch
            session.touch(ts)
            return session.session_id

        sid = self._sid(cursor, path)
        ptype = payload.get("type")
        if kind == "event_msg" and ptype == "user_message":
            get(sid).add_prompt(ts, str(payload.get("message") or ""))
            return sid
        if kind == "event_msg" and ptype == "agent_message":
            text = str(payload.get("message") or "")
            get(sid).add_reply(ts, text, urls(text))
            return sid
        if ptype == "message":
            text = text_of(payload.get("content"))
            role = payload.get("role")
            # Codex wraps injected context (environment, instructions) in tags.
            if role == "user" and not text.lstrip().startswith("<"):
                get(sid).add_prompt(ts, text)
                return sid
            if role == "assistant" and text:
                get(sid).add_reply(ts, text, urls(text))
                return sid
        if kind == "turn_context" and (payload.get("cwd") or payload.get("model")):
            session = get(sid)
            session.context = session.context or payload.get("cwd") or ""
            session.set_model(str(payload.get("model") or ""))
            return sid
        return None

    @staticmethod
    def _sid(cursor: dict, path: Path) -> str:
        if not cursor.get("sid"):
            match = _UUID.search(path.name)
            cursor["sid"] = match.group(0) if match else path.stem
        return cursor["sid"]
