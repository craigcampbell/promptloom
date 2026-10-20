"""Adapter interface. A harness turns its own session storage into Sessions.

To add a harness, subclass `Harness` (or `JsonlHarness` for line-delimited
JSON), set `type`, and drop the file in ~/.config/promptloom/harnesses/.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable

from ..model import Session

# get(session_id) returns the session, created on first use. A harness that
# relays other tools' sessions (the ingest API) passes their name as a second
# argument so they are filed under it.
GetSession = Callable[..., Session]


class Harness:
    type = ""  # value of `type` in config; defaults to the harness name
    label = ""  # display name
    resume = ""  # shell command to resume a session; "{id}" is replaced

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.label = cfg.get("label") or self.label or name
        self.resume = cfg.get("resume", self.resume)

    def sources(self) -> Iterable[Path]:
        """Files holding session data. Missing files mean the harness is absent."""
        raise NotImplementedError

    def fingerprint(self, path: Path) -> list:
        """Changes whenever `path` may hold new data."""
        st = path.stat()
        return [st.st_mtime_ns, st.st_size]

    def read(self, path: Path, cursor: dict, get: GetSession) -> set[str]:
        """Fold new data from `path` into sessions obtained through `get`.

        `cursor` persists between calls for this path. Returns the ids of the
        sessions that were touched.
        """
        raise NotImplementedError


class JsonlHarness(Harness):
    """Reads append-only JSONL incrementally, one `record()` call per line."""

    def read(self, path: Path, cursor: dict, get: GetSession) -> set[str]:
        size = path.stat().st_size
        offset = cursor.get("offset", 0)
        if size < offset:  # truncated or rewritten: start over
            cursor.clear()
            offset = 0
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        end = data.rfind(b"\n") + 1  # leave a partially written line for next time
        touched: set[str] = set()
        for line in data[:end].splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                sid = self.record(rec, path, cursor, get)
                if sid:
                    touched.add(sid)
        cursor["offset"] = offset + end
        return touched

    def record(self, rec: dict, path: Path, cursor: dict, get: GetSession) -> str | None:
        """Handle one JSON object. Return the session id it belongs to, if any."""
        raise NotImplementedError


def text_of(content) -> str:
    """Flatten the usual message-content shapes (str, or a list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""
