"""What people add in the app: notes, and tags on sessions and single prompts.

Kept apart from state.json, which the daemon rewrites and holds locked while a
summary runs, so an edit in the app is saved at once.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

from . import extract
from .store import file_lock, write_atomic

_TAG = re.compile(r"[^\w:./+-]+")


def clean_tag(tag: str) -> str:
    return _TAG.sub("-", str(tag).strip().lower().lstrip("#")).strip("-")[:48]


def clean_tags(tags) -> list[str]:
    out: list[str] = []
    for tag in tags if isinstance(tags, list) else []:
        tag = clean_tag(tag)
        if tag and tag not in out:
            out.append(tag)
    return out


class UserData:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "user.json"
        self.lock_path = state_dir / "user.lock"
        self.notes: list[dict] = []  # id, title, body, tags, session, project, created, updated
        self.session_tags: dict[str, list[str]] = {}  # session key -> tags
        self.prompt_tags: dict[str, list[str]] = {}  # "<session key>#<prompt index>" -> tags
        self._loaded = None

    def load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._loaded:
            return
        data = json.loads(self.path.read_text())
        self._loaded = mtime
        self.notes = data.get("notes", [])
        self.session_tags = data.get("session_tags", {})
        self.prompt_tags = data.get("prompt_tags", {})

    def save(self) -> None:
        data = {"version": 1, "notes": self.notes, "session_tags": self.session_tags,
                "prompt_tags": self.prompt_tags}
        write_atomic(self.path, json.dumps(data, ensure_ascii=False, indent=1))
        self._loaded = self.path.stat().st_mtime_ns

    @contextmanager
    def editing(self):
        """Load, let the caller change things, and save, under the lock."""
        with file_lock(self.lock_path):
            self.load()
            yield self
            self.save()

    # notes

    def note(self, note_id: str) -> dict | None:
        return next((n for n in self.notes if n["id"] == note_id), None)

    def put_note(self, fields: dict) -> dict:
        now = extract.now_ts()
        note = self.note(str(fields.get("id") or ""))
        if note is None:
            note = {"id": "n-" + extract.item_id(f"{time.time()} {fields.get('title', '')}"),
                    "created": now}
            self.notes.append(note)
        note["title"] = str(fields.get("title") or "").strip()[:200] or "Untitled"
        note["body"] = str(fields.get("body") or "")
        note["tags"] = clean_tags(fields.get("tags"))
        note["session"] = str(fields.get("session") or "")
        note["project"] = str(fields.get("project") or "")
        note["updated"] = now
        return note

    def delete_note(self, note_id: str) -> bool:
        before = len(self.notes)
        self.notes = [n for n in self.notes if n["id"] != note_id]
        return len(self.notes) != before

    # tags

    def set_tags(self, session_key: str, tags, prompt: int | None = None) -> list[str]:
        table = self.session_tags if prompt is None else self.prompt_tags
        key = session_key if prompt is None else f"{session_key}#{prompt}"
        tags = clean_tags(tags)
        if tags:
            table[key] = tags
        else:
            table.pop(key, None)
        return tags
