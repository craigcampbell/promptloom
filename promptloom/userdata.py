"""What people add in the app: notes, drafts of prompts, and tags on sessions
and single prompts.

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
        # id, title, body, tool, model, context, created, updated, sent, sent_with, and
        # versions: every weave, with the text before and after
        self.drafts: list[dict] = []
        self.briefs: dict[str, dict] = {}  # project -> {"text", "updated"}: what you wrote
        self.threads: dict[str, dict] = {}  # thread id -> {"name", "closed", "pinned"}
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
        self.drafts = data.get("drafts", [])
        self.briefs = data.get("briefs", {})
        self.threads = data.get("threads", {})

    def save(self) -> None:
        data = {"version": 1, "notes": self.notes, "session_tags": self.session_tags,
                "prompt_tags": self.prompt_tags, "drafts": self.drafts,
                "briefs": self.briefs, "threads": self.threads}
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

    # drafts: prompts being worked on before they go to a tool

    def draft(self, draft_id: str) -> dict | None:
        return next((d for d in self.drafts if d["id"] == draft_id), None)

    def put_draft(self, fields: dict) -> dict:
        now = extract.now_ts()
        draft = self.draft(str(fields.get("id") or ""))
        if draft is None:
            draft = {"id": "d-" + extract.item_id(f"{time.time()} {fields.get('body', '')}"),
                     "created": now, "versions": [], "sent": "", "sent_with": ""}
            self.drafts.append(draft)
        body = str(fields.get("body") or "")
        draft["body"] = body
        draft["title"] = (str(fields.get("title") or "").strip()[:120]
                          or extract.fallback_title(body, 80))
        for key in ("tool", "model", "context"):
            if key in fields:
                draft[key] = str(fields.get(key) or "")
        draft["updated"] = now
        return draft

    def weave_draft(self, draft_id: str, prompt: str, by: str, notes: str) -> dict:
        """Make the woven prompt the draft's text, keeping what it said before."""
        draft = self.draft(draft_id)
        if draft is None:
            raise LookupError("no such draft")
        now = extract.now_ts()
        draft["versions"].append({"at": now, "by": by, "notes": notes,
                                  "before": draft.get("body", ""), "after": prompt})
        draft["versions"] = draft["versions"][-20:]
        draft["body"] = prompt
        draft["updated"] = now
        return draft

    def mark_sent(self, draft_id: str, tool_label: str) -> dict | None:
        draft = self.draft(draft_id)
        if draft is not None:
            draft["sent"] = extract.now_ts()
            draft["sent_with"] = tool_label
        return draft

    def delete_draft(self, draft_id: str) -> bool:
        before = len(self.drafts)
        self.drafts = [d for d in self.drafts if d["id"] != draft_id]
        return len(self.drafts) != before

    # briefs and threads

    def put_brief(self, project: str, text: str | None = None, hide: str = "",
                  unhide: str = "") -> dict:
        entry = self.briefs.setdefault(project, {"text": "", "updated": "", "hidden": []})
        if text is not None:
            entry["text"] = text.rstrip()
            entry["updated"] = extract.now_ts()
        hidden = [h for h in entry.get("hidden", []) if h != unhide]
        if hide and hide not in hidden:
            hidden.append(hide)
        entry["hidden"] = hidden
        if not entry["text"] and not hidden:
            self.briefs.pop(project, None)
        return entry

    def set_thread(self, thread_id: str, **fields) -> dict:
        entry = self.threads.setdefault(thread_id, {})
        for key in ("name", "closed", "pinned"):
            if key in fields:
                entry[key] = fields[key]
        if not any(entry.values()):
            self.threads.pop(thread_id, None)
        return entry

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
