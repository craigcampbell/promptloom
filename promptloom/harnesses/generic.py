"""Config-driven adapter for any harness that logs one JSON object per line."""

from __future__ import annotations

import glob
from pathlib import Path

from ..extract import norm_ts, urls
from .base import JsonlHarness, text_of


def dig(rec, path: str):
    """Follow a dotted path through nested dicts; None when any step is missing."""
    for part in path.split("."):
        if not isinstance(rec, dict):
            return None
        rec = rec.get(part)
    return rec


class GenericJsonl(JsonlHarness):
    type = "jsonl"

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        if not cfg.get("glob"):
            raise ValueError("a jsonl harness needs a 'glob'")

    def sources(self):
        pattern = str(Path(self.cfg["glob"]).expanduser())
        return (Path(p) for p in glob.glob(pattern, recursive=True))

    def _field(self, rec: dict, name: str, path: Path):
        spec = self.cfg.get(name)
        if not spec:
            return None
        if spec == "@stem":
            return path.stem
        return dig(rec, spec)

    def record(self, rec, path, cursor, get):
        sid = self._field(rec, "session_id", path) or cursor.get("sid") or path.stem
        sid = cursor["sid"] = str(sid)
        session = get(sid)
        ts = norm_ts(self._field(rec, "timestamp", path))

        cwd = self._field(rec, "cwd", path)
        if isinstance(cwd, str) and cwd:
            session.context = cwd
        title = self._field(rec, "title", path)
        if isinstance(title, str) and title:
            session.title = title

        role = self._field(rec, "role", path)
        text = text_of(self._field(rec, "text", path))
        model = self._field(rec, "model", path)
        model = model if isinstance(model, str) else ""
        if role == self.cfg.get("user_value", "user"):
            session.set_model(model)
            session.add_prompt(ts, text)
        elif role == self.cfg.get("assistant_value", "assistant") and (text or model):
            session.add_reply(ts, text, urls(text), model)
        else:
            session.touch(ts)
        return sid
