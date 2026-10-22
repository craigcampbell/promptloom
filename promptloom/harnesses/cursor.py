"""Cursor agent chats: ~/.cursor/projects/<folder>/agent-transcripts/<id>/<id>.jsonl

Transcripts carry no timestamps, so a prompt is dated by the file's last write.
The folder name is the project path with its slashes turned into dashes; it is
matched back to a real folder on disk.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..extract import norm_ts, urls
from .base import JsonlHarness, text_of

_NUDGE = "Briefly inform the user about the task result"
_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def folder_of(slug: str) -> str:
    """Users-me-my-app -> /Users/me/my-app, trying dashes as separators or as part of a name."""
    parts = slug.split("-")

    def walk(base: Path, i: int) -> Path | None:
        if i == len(parts):
            return base
        for j in range(len(parts), i, -1):  # longest name first
            candidate = base / "-".join(parts[i:j])
            if candidate.exists() and (found := walk(candidate, j)):
                return found
        return None

    found = walk(Path("/"), 0)
    return str(found) if found else "/" + slug.replace("-", "/")


class Cursor(JsonlHarness):
    type = "cursor"
    label = "Cursor"

    def sources(self):
        root = Path(self.cfg.get("root", "~/.cursor/projects")).expanduser()
        for path in root.glob("*/agent-transcripts/*/*.jsonl"):
            if path.stem == path.parent.name:  # skip subagents/
                yield path

    def record(self, rec, path, cursor, get):
        role = rec.get("role")
        if role not in ("user", "assistant"):
            return None
        sid = path.stem
        session = get(sid)
        if not session.context:
            session.context = folder_of(path.parents[2].name)
        ts = norm_ts(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
        text = text_of((rec.get("message") or {}).get("content"))
        if role == "user":
            match = _QUERY.search(text)
            # Other user turns, and the follow-up nudge sent after a subagent
            # finishes, are text Cursor injected.
            if match and not match.group(1).startswith(_NUDGE):
                session.add_prompt(ts, match.group(1))
        else:
            session.add_reply(ts, text, urls(text))
        return sid
