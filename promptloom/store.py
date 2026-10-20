"""Persistent state: captured sessions, what the model said about them, read cursors."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .extract import fallback_title
from .model import Session

STATE_VERSION = 2  # 2: prompts carry the model that answered them

if sys.platform == "win32":
    import msvcrt

    def _flock(fh, blocking: bool) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)

    def _funlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _flock(fh, blocking: bool) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _funlock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        _flock(fh, blocking)
        try:
            yield
        finally:
            _funlock(fh)


@dataclass
class Record:
    """A session plus everything promptloom has layered on top of it."""

    session: Session
    enrich: dict = field(default_factory=dict)  # title, summary, themes, prompt_count, at
    actions: list[dict] = field(default_factory=list)  # id, text, done, ts
    note: str = ""  # vault-relative path of the session note, without ".md"
    retry_at: float = 0.0

    @property
    def title(self) -> str:
        if self.session.title:
            return self.session.title
        if self.enrich.get("title"):
            return self.enrich["title"]
        first = self.session.prompts[0].text if self.session.prompts else ""
        return fallback_title(first)

    @property
    def summary(self) -> str:
        return self.enrich.get("summary", "")

    @property
    def themes(self) -> list[str]:
        return self.enrich.get("themes", [])

    def to_dict(self) -> dict:
        return {
            "session": self.session.to_dict(),
            "enrich": self.enrich,
            "actions": self.actions,
            "note": self.note,
            "retry_at": self.retry_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        return cls(
            session=Session.from_dict(data["session"]),
            enrich=data.get("enrich", {}),
            actions=data.get("actions", []),
            note=data.get("note", ""),
            retry_at=data.get("retry_at", 0.0),
        )


class Store:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.spool = state_dir / "captures.jsonl"
        self.status_path = state_dir / "status.json"
        self.records: dict[str, Record] = {}
        self.sources: dict[str, dict] = {}  # "<harness>|<path>" -> {"fp": [...], "cursor": {...}}
        self.captures: list[dict] = []  # id, ts, text, todo, done, context, session
        self.insights: dict = {}
        self.notes: dict[str, int] = {}  # note path -> mtime_ns as promptloom last wrote it
        self.version = STATE_VERSION
        self._loaded = None  # mtime of the state file as last read or written

    def load(self) -> None:
        if not self.path.exists():
            return
        mtime = self.path.stat().st_mtime_ns
        if mtime == self._loaded:
            return  # nobody else has written since
        self._loaded = mtime
        data = json.loads(self.path.read_text())
        self.version = data.get("version", 1)
        self.records = {k: Record.from_dict(v) for k, v in data.get("sessions", {}).items()}
        self.sources = data.get("sources", {})
        self.captures = data.get("captures", [])
        self.insights = data.get("insights", {})
        self.notes = data.get("notes", {})

    def save(self) -> None:
        data = {
            "version": STATE_VERSION,
            "sessions": {k: r.to_dict() for k, r in self.records.items()},
            "sources": self.sources,
            "captures": self.captures,
            "insights": self.insights,
            "notes": self.notes,
        }
        write_atomic(self.path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        self._loaded = self.path.stat().st_mtime_ns
        self.version = STATE_VERSION

    @contextmanager
    def lock(self, blocking: bool = True):
        """Serialise whole sync cycles between the daemon and one-off commands."""
        with file_lock(self.dir / "lock", blocking):
            yield

    def spool_capture(self, capture: dict) -> None:
        """Queue a capture without taking the lock; the next sync picks it up."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.spool, "a") as fh:
            fh.write(json.dumps(capture, ensure_ascii=False) + "\n")

    def drain_spool(self) -> int:
        taken = self.spool.with_suffix(".taken")
        lines = taken.read_text().splitlines() if taken.exists() else []  # interrupted drain
        if self.spool.exists():
            os.replace(self.spool, taken)
            lines += taken.read_text().splitlines()
        if not taken.exists():
            return 0
        count = 0
        for line in lines:
            try:
                capture = json.loads(line)
            except ValueError:
                continue
            if all(capture.get("id") != c.get("id") for c in self.captures):
                self.captures.append(capture)
                count += 1
        taken.unlink()
        return count

    def by_recency(self) -> list[Record]:
        records = [r for r in self.records.values() if r.session.prompts]
        return sorted(records, key=lambda r: r.session.updated, reverse=True)

    def known_themes(self) -> list[str]:
        counts: dict[str, int] = {}
        for record in self.records.values():
            for theme in record.themes:
                counts[theme] = counts.get(theme, 0) + 1
        return sorted(counts, key=lambda t: (-counts[t], t))


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
