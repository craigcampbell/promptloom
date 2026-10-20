"""Prompts pushed to promptloom by other tools, through `promptloom log` or the
HTTP API (POST /api/ingest). Both append one JSON object per line to
~/.local/state/promptloom/ingest.jsonl, which this harness reads like any other.

    {"harness": "aider", "session": "abc", "context": "/path/to/project",
     "prompt": "...", "reply": "...", "model": "gpt-5", "ts": "2026-10-20T10:00:00Z",
     "title": "...", "branch": "main"}

Only "prompt" (or "reply") is required. Without "session", prompts from the same
harness and directory on the same day share one session.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import config
from ..extract import item_id, norm_ts, now_ts, urls
from .base import JsonlHarness

_NAME = re.compile(r"[^a-z0-9._-]+")


def harness_name(raw) -> str:
    name = _NAME.sub("-", str(raw or "").strip().lower()).strip("-")
    return name[:40] or "api"


class Api(JsonlHarness):
    type = "api"
    label = "API"

    def path(self) -> Path:
        return Path(self.cfg.get("path", config.STATE_DIR / "ingest.jsonl")).expanduser()

    def sources(self):
        path = self.path()
        return [path] if path.exists() else []

    def record(self, rec, path, cursor, get):
        prompt = rec.get("prompt")
        reply = rec.get("reply")
        if not isinstance(prompt, str) and not isinstance(reply, str):
            return None
        harness = harness_name(rec.get("harness"))
        context = str(rec.get("context") or "")
        ts = norm_ts(rec.get("ts")) or now_ts()
        sid = str(rec.get("session") or "") or item_id(f"{harness} {context} {ts[:10]}")
        session = get(sid, harness)
        session.context = context or session.context
        session.branch = str(rec.get("branch") or "") or session.branch
        if rec.get("title"):
            session.title = str(rec["title"])
        model = str(rec.get("model") or "")
        if isinstance(prompt, str):
            session.set_model(model)
            session.add_prompt(ts, prompt)
        if isinstance(reply, str):
            session.add_reply(ts, reply, urls(reply), model)
        return sid
