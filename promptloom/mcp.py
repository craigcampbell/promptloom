"""A Model Context Protocol server on stdio, so an assistant (Claude Code,
Codex, Copilot in VS Code, ...) can look things up in your prompt history:

    claude mcp add promptloom -- promptloom mcp

It reads the same state as the app and never writes, except `capture_note`,
which queues a capture exactly as `promptloom capture` does.
"""

from __future__ import annotations

import json
import sys

from . import __version__, analysis, brief, config, connect, engine
from .store import Store
from .userdata import UserData

PROTOCOL = "2025-06-18"

TOOLS = [
    {
        "name": "search_prompts",
        "description": "Search every prompt the user has typed into any AI coding tool, and "
                       "their notes. Words must all match. Filters can go in the query: "
                       "project:NAME model:NAME harness:NAME tag:NAME theme:NAME "
                       "after:YYYY-MM-DD before:YYYY-MM-DD. Newest first.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 20}}, "required": ["query"]},
    },
    {
        "name": "project_history",
        "description": "How a project came together: its sessions in order with summaries, "
                       "which models and tools worked on it, when the work moved between "
                       "models, and its open action items.",
        "inputSchema": {"type": "object", "properties": {
            "project": {"type": "string", "description": "directory name of the project"}},
            "required": ["project"]},
    },
    {
        "name": "project_brief",
        "description": "What to know before working on a project: where things stand, rules "
                       "and preferences the user has stated, what went wrong before, what is "
                       "still open. Read it first when starting work in a project the user "
                       "has used AI tools in before.",
        "inputSchema": {"type": "object", "properties": {
            "project": {"type": "string", "description": "directory name of the project"}},
            "required": ["project"]},
    },
    {
        "name": "related_work",
        "description": "Other projects and sessions that share unusual words with this "
                       "project, i.e. work that may be worth reusing or connecting.",
        "inputSchema": {"type": "object", "properties": {
            "project": {"type": "string"}}, "required": ["project"]},
    },
    {
        "name": "list_projects",
        "description": "Every project with prompts captured, with the models used on each.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "capture_note",
        "description": "Save a note or to-do to the user's promptloom inbox.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string"}, "todo": {"type": "boolean", "default": False},
            "context": {"type": "string", "description": "project directory"}},
            "required": ["text"]},
    },
]


class Tools:
    def __init__(self):
        self.store = Store(config.STATE_DIR)
        self.user = UserData(config.STATE_DIR)

    def _load(self):
        self.store.load()
        self.user.load()
        return self.store.by_recency()

    def search_prompts(self, query: str, limit: int = 20):
        result = analysis.search(self._load(), self.user, query, max(1, min(limit, 100)))
        lines = [f"{result['total']} matching prompts" +
                 (f" (showing {len(result['hits'])})" if result["total"] > len(result["hits"])
                  else "")]
        for hit in result["hits"]:
            lines.append(f"- {hit['ts'][:16]} [{hit['project']} · {hit['harness']} · "
                         f"{hit['model'] or 'unknown model'}] {hit['title']}: {hit['text']}")
        for note in result["notes"]:
            lines.append(f"- note \"{note['title']}\": {note['text']}")
        return "\n".join(lines)

    def project_history(self, project: str):
        records = self._load()
        if not any(r.session.project == project for r in records):
            names = sorted({r.session.project for r in records})
            return f"No project named {project}. Known: {', '.join(names)}"
        data = analysis.timeline(records, project, self.user)
        info = data["info"]
        own = self.user.briefs.get(project, {}).get("text", "")
        lines = [f"# {project}", f"{info['sessions']} sessions, {info['prompts']} prompts, "
                 f"{info['first'][:10]} to {info['last'][:10]}",
                 "Models: " + ", ".join(f"{m} ({n})" for m, n in info["models"].items()),
                 "Tools: " + ", ".join(f"{h} ({n})" for h, n in info["harnesses"].items()),
                 "", *([own, ""] if own else []), "## Sessions"]
        for s in data["sessions"]:
            lines.append(f"- {s['started'][:10]} {s['title']} [{s['harness']}; "
                         f"{', '.join(s['models']) or 'model unknown'}]"
                         + (f": {s['summary']}" if s["summary"] else ""))
        if data["handoffs"]:
            lines += ["", "## Model changes"]
            lines += [f"- {h['at'][:10]} {h['from']} → {h['to']} ({h['title']})"
                      for h in data["handoffs"]]
        open_actions = [a for a in data["actions"] if not a.get("done")]
        if open_actions:
            lines += ["", "## Open action items"] + [f"- {a['text']}" for a in open_actions]
        return "\n".join(lines)

    def project_brief(self, project: str):
        records = self._load()
        if not any(r.session.project == project for r in records):
            names = sorted({r.session.project for r in records})
            return f"No project named {project}. Known: {', '.join(names)}"
        own = self.user.briefs.get(project, {})
        return brief.render_brief(brief.draft_brief(project, records, own.get("hidden", [])),
                                  own.get("text", ""))

    def related_work(self, project: str):
        records = self._load()
        web = connect.Web(records)
        lines = []
        for link in web.projects():
            if project in (link.a, link.b):
                other = link.b if link.a == project else link.a
                lines.append(f"- {other}: shares {', '.join(link.words)}")
                for session_link in link.links[:2]:
                    lines.append(f"    {session_link.a.title} ↔ {session_link.b.title}")
        return "\n".join(lines) or f"Nothing found that links {project} to other projects."

    def list_projects(self):
        lines = []
        for p in analysis.projects(self._load()):
            lines.append(f"- {p['name']} ({p['sessions']} sessions, last {p['last'][:10]}): "
                         + ", ".join(p["models"]))
        return "\n".join(lines)

    def capture_note(self, text: str, todo: bool = False, context: str = ""):
        item = engine.capture(self.store, text, todo, context)
        return ("To-do saved: " if item["todo"] else "Note saved: ") + item["text"]


def handle(tools: Tools, message: dict) -> dict | None:
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # a notification
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": asked, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "promptloom", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if name not in {t["name"] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            text = getattr(tools, name)(**(params.get("arguments") or {}))
            result = {"content": [{"type": "text", "text": text}]}
        except Exception as exc:  # report to the assistant, keep serving
            result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def run() -> int:
    tools = Tools()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = handle(tools, message) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
