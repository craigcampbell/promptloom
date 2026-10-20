"""Hermes Agent: sessions and messages in the SQLite database ~/.hermes/state.db"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..extract import norm_ts, urls
from .base import Harness


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


class Hermes(Harness):
    type = "hermes"
    label = "Hermes"
    resume = "hermes --resume {id}"

    def _db(self) -> Path:
        return Path(self.cfg.get("db", "~/.hermes/state.db")).expanduser()

    def sources(self):
        db = self._db()
        return [db] if db.exists() else []

    def fingerprint(self, path):
        # In WAL mode new rows land in the -wal file before the main database.
        prints = []
        for candidate in (path, path.with_name(path.name + "-wal")):
            if candidate.exists():
                st = candidate.stat()
                prints += [st.st_mtime_ns, st.st_size]
        return prints

    def read(self, path, cursor, get):
        touched: set[str] = set()
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            scols = _columns(db, "sessions")
            mcols = _columns(db, "messages")
            if not {"id"} <= scols or not {"session_id", "role", "content"} <= mcols:
                return touched
            order = "timestamp, id" if "timestamp" in mcols else "id"
            for srow in db.execute("SELECT * FROM sessions"):
                sid = str(srow["id"])
                session = get(sid)
                session.prompts.clear()  # the whole table is re-read each time
                if "title" in scols and srow["title"]:
                    session.title = srow["title"]
                if "source" in scols and srow["source"]:
                    session.context = f"hermes-{srow['source']}"
                if "model" in scols and srow["model"]:
                    session.set_model(str(srow["model"]))
                if "started_at" in scols:
                    session.touch(norm_ts(srow["started_at"]))
                for mrow in db.execute(
                    f"SELECT * FROM messages WHERE session_id = ? ORDER BY {order}", (srow["id"],)
                ):
                    text = mrow["content"]
                    if not isinstance(text, str) or not text.strip():
                        continue
                    ts = norm_ts(mrow["timestamp"]) if "timestamp" in mcols else ""
                    if mrow["role"] == "user":
                        session.add_prompt(ts, text)
                    elif mrow["role"] == "assistant":
                        model = mrow["model"] if "model" in mcols else ""
                        session.add_reply(ts, text, urls(text), str(model or ""))
                touched.add(sid)
        finally:
            db.close()
        return touched
