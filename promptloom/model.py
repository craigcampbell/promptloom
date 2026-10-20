"""The harness-neutral shape every adapter produces."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_REPLY_CHARS = 1500
MAX_REPLY_URLS = 200


@dataclass
class Prompt:
    ts: str  # ISO 8601, "" when the harness records no time
    text: str
    model: str = ""  # the model that answered it, when the harness says


@dataclass
class Session:
    harness: str
    session_id: str
    context: str = ""  # working directory, or whatever scopes the session
    title: str = ""  # title supplied by the harness itself, if any
    branch: str = ""
    model: str = ""  # the model most recently in use
    started: str = ""
    updated: str = ""
    source: str = ""
    prompts: list[Prompt] = field(default_factory=list)
    last_reply: str = ""
    reply_urls: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.harness}:{self.session_id}"

    @property
    def project(self) -> str:
        """Short name for the context: the directory name, or the context itself."""
        if not self.context:
            return "no-context"
        if self.context.startswith(("/", "~")):
            return Path(self.context).name or "root"
        return self.context

    def touch(self, ts: str) -> None:
        if not ts:
            return
        if not self.started or ts < self.started:
            self.started = ts
        if ts > self.updated:
            self.updated = ts

    def add_prompt(self, ts: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.prompts and self.prompts[-1].text == text:
            return
        self.prompts.append(Prompt(ts, text, self.model))
        self.touch(ts)

    def set_model(self, model: str) -> None:
        """The model about to answer: for harnesses that name it before the prompt."""
        if model:
            self.model = model

    def add_reply(self, ts: str, text: str, urls: list[str], model: str = "") -> None:
        if model:
            # The reply names the model that actually handled the last prompt.
            self.model = model
            if self.prompts:
                self.prompts[-1].model = model
        text = text.strip()
        if text:
            self.last_reply = text[-MAX_REPLY_CHARS:]
        for url in urls:
            if url not in self.reply_urls and len(self.reply_urls) < MAX_REPLY_URLS:
                self.reply_urls.append(url)
        self.touch(ts)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        data = dict(data)
        data["prompts"] = [Prompt(**p) for p in data.get("prompts", [])]
        return cls(**data)
