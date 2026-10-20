"""Claude Code: ~/.claude/projects/<project>/<session-id>.jsonl"""

from __future__ import annotations

import re
from pathlib import Path

from ..extract import norm_ts, urls
from .base import JsonlHarness, text_of

# Content the CLI writes into user turns that the user did not type.
_INJECTED = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|local-command-stderr|"
    r"command-name|command-message|command-args|task-notification|bash-input|bash-stdout|"
    r"bash-stderr|user-prompt-submit-hook)>.*?</\1>",
    re.S,
)
_NOISE_PREFIXES = ("[Request interrupted", "Caveat: The messages below")


def clean_prompt(text: str) -> str:
    text = _INJECTED.sub("", text).strip()
    if text.startswith(_NOISE_PREFIXES):
        return ""
    return text


class ClaudeCode(JsonlHarness):
    type = "claude-code"
    label = "Claude Code"
    resume = "claude --resume {id}"

    def sources(self):
        root = Path(self.cfg.get("root", "~/.claude/projects")).expanduser()
        return root.glob("*/*.jsonl")

    def record(self, rec, path, cursor, get):
        sid = rec.get("sessionId")
        if not sid or rec.get("isSidechain"):
            return None
        kind = rec.get("type")

        if kind == "custom-title":
            get(sid).title = rec.get("customTitle") or ""
            return sid
        if kind == "ai-title":
            session = get(sid)
            session.title = session.title or rec.get("aiTitle") or ""
            return sid
        if kind == "attachment":
            # A message typed while the assistant was still working.
            queued = rec.get("attachment") or {}
            origin = queued.get("origin") or {}
            if queued.get("type") != "queued_command" or origin.get("kind") != "human":
                return None
            return self._prompt(rec, sid, str(queued.get("prompt") or ""), get)
        if kind not in ("user", "assistant"):
            return None

        message = rec.get("message") or {}
        content = message.get("content")
        ts = norm_ts(rec.get("timestamp"))

        if kind == "assistant":
            text = text_of(content)
            model = message.get("model") or ""
            if model.startswith("<"):  # "<synthetic>": written by the CLI, not a model
                model = ""
            if not text and not model:
                return None
            get(sid).add_reply(ts, text, urls(text), model)
            return sid

        if rec.get("isMeta"):
            return None
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return None
        return self._prompt(rec, sid, text_of(content), get)

    @staticmethod
    def _prompt(rec, sid, text, get):
        text = clean_prompt(text)
        if not text:
            return None
        session = get(sid)
        session.context = session.context or rec.get("cwd") or ""
        session.branch = rec.get("gitBranch") or session.branch
        # A queued message can also be delivered as an ordinary turn afterwards.
        if all(p.text != text for p in session.prompts[-3:]):
            session.add_prompt(norm_ts(rec.get("timestamp")), text)
        return sid
