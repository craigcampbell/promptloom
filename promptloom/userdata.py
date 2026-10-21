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


COLORS = ("red", "orange", "yellow", "green", "teal", "blue", "purple", "pink", "gray")
DAY_SETS = {"daily": "0123456", "weekdays": "01234", "weekends": "56"}
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def clean_schedule(raw) -> dict:
    """{"time": "08:00", "days": "0123456"} (weekday numbers, Monday 0), or {} for none."""
    if not isinstance(raw, dict):
        return {}
    time_text = str(raw.get("time") or "").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", time_text):
        return {}
    hour, minute = time_text.split(":")
    days = raw.get("days", "daily")
    if isinstance(days, list):
        days = "".join(str(DAY_NAMES.index(d)) if d in DAY_NAMES else str(d) for d in days)
    days = DAY_SETS.get(str(days), str(days))
    days = "".join(sorted({c for c in days if c in "0123456"}))
    if not days:
        return {}
    return {"time": f"{int(hour):02d}:{minute}", "days": days}


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
        self.tag_colors: dict[str, str] = {}  # tag -> colour name
        # id, name, color, items (prompt refs "<session key>#<index>"), created, updated
        self.collections: list[dict] = []
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
        self.tag_colors = data.get("tag_colors", {})
        self.collections = data.get("collections", [])

    def save(self) -> None:
        data = {"version": 1, "notes": self.notes, "session_tags": self.session_tags,
                "prompt_tags": self.prompt_tags, "drafts": self.drafts,
                "briefs": self.briefs, "threads": self.threads,
                "tag_colors": self.tag_colors, "collections": self.collections}
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
        if "prompt" in fields:  # a note on one prompt: "<session key>#<index>"
            note["prompt"] = str(fields.get("prompt") or "")
        # A note can be a prompt: which tool runs it, and when, if on a schedule.
        for key in ("tool", "model", "context"):
            if key in fields:
                note[key] = str(fields.get(key) or "")
        if "schedule" in fields:
            note["schedule"] = clean_schedule(fields.get("schedule"))
        note.setdefault("runs", 0)
        note.setdefault("last_run", "")
        note["updated"] = now
        return note

    def note_ran(self, note_id: str, how: str = "", at: str = "") -> dict | None:
        note = self.note(note_id)
        if note is not None:
            note["runs"] = note.get("runs", 0) + 1
            note["last_run"] = at or extract.now_ts()
            note["last_run_how"] = how
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

    # tag colours and collections

    def set_tag_color(self, tag: str, color: str) -> dict[str, str]:
        tag = clean_tag(tag)
        if color in COLORS and tag:
            self.tag_colors[tag] = color
        else:
            self.tag_colors.pop(tag, None)
        return self.tag_colors

    def collection(self, collection_id: str) -> dict | None:
        return next((c for c in self.collections if c["id"] == collection_id), None)

    def put_collection(self, fields: dict) -> dict:
        now = extract.now_ts()
        found = self.collection(str(fields.get("id") or ""))
        name = str(fields.get("name") or "").strip()[:80]
        if found is None:
            found = next((c for c in self.collections
                          if name and c["name"].lower() == name.lower()), None)
        if found is None:
            found = {"id": "c-" + extract.item_id(f"{time.time()} {name}"), "items": [],
                     "created": now, "color": ""}
            self.collections.append(found)
        if name:
            found["name"] = name
        found.setdefault("name", "Untitled")
        if fields.get("color") in COLORS or fields.get("color") == "":
            found["color"] = fields.get("color", found.get("color", ""))
        found["updated"] = now
        return found

    def collect(self, collection_id: str, refs: list[str], remove: bool = False) -> dict:
        found = self.collection(collection_id)
        if found is None:
            raise LookupError("no such collection")
        refs = [str(r) for r in refs if isinstance(r, str) and "#" in r]
        if remove:
            found["items"] = [r for r in found["items"] if r not in refs]
        else:
            found["items"] += [r for r in refs if r not in found["items"]]
        found["updated"] = extract.now_ts()
        return found

    def delete_collection(self, collection_id: str) -> bool:
        before = len(self.collections)
        self.collections = [c for c in self.collections if c["id"] != collection_id]
        return len(self.collections) != before

    def add_tags(self, ref: str, tags: list[str], remove: bool = False) -> list[str]:
        """Add (or take away) tags on one prompt, keeping the others."""
        current = self.prompt_tags.get(ref, [])
        wanted = clean_tags(tags)
        tags = [t for t in current if t not in wanted] if remove else \
            current + [t for t in wanted if t not in current]
        if tags:
            self.prompt_tags[ref] = tags
        else:
            self.prompt_tags.pop(ref, None)
        return tags

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
