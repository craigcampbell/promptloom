"""The built-in app: a small HTTP server on this machine only, serving the page
in promptloom/ui and a JSON API over the captured sessions.

It runs inside `promptloom watch`, or on its own with `promptloom ui`. It reads
state.json (written by the daemon) and reads and writes user.json (notes and
tags). Quick captures and prompts sent to /api/ingest are queued in files the
daemon picks up on its next cycle.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, analysis, config, connect, engine, extract, harnesses
from .store import Store
from .userdata import UserData

UI_DIR = Path(__file__).parent / "ui"
MAX_BODY = 2_000_000


class App:
    """Shared, cached view of the state for every request."""

    def __init__(self, cfg: dict, state_dir: Path, plugin_dir: Path | None = None):
        self.cfg = cfg
        self.state_dir = state_dir
        self.store = Store(state_dir)
        self.user = UserData(state_dir)
        found, _ = harnesses.build(cfg, plugin_dir or config.PLUGIN_DIR)
        self.info = {h.name: {"label": h.label, "resume": h.resume} for h in found}
        self.lock = threading.Lock()
        self._web = None
        self._web_stamp = None
        self._terms: dict = {}

    def refresh(self) -> None:
        self.store.load()
        self.user.load()

    def records(self):
        return self.store.by_recency()

    def web(self) -> connect.Web:
        stamp = self.store._loaded
        if self._web is None or stamp != self._web_stamp:
            self._web = connect.Web(self.records(), self._terms)
            self._web_stamp = stamp
        return self._web

    def label(self, harness: str) -> str:
        return self.info.get(harness, {}).get("label", harness)

    def status(self) -> dict:
        return engine.read_status(self.store)

    def ingest(self, items: list[dict]) -> int:
        """Queue prompts from another tool for the api harness."""
        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not isinstance(item.get("prompt"), str) and not isinstance(item.get("reply"), str):
                continue
            item = {k: item[k] for k in ("harness", "session", "context", "prompt", "reply",
                                         "model", "ts", "title", "branch") if k in item}
            item.setdefault("ts", extract.now_ts())
            lines.append(json.dumps(item, ensure_ascii=False))
        if lines:
            append_ingest(self.state_dir, lines)
        return len(lines)


def append_ingest(state_dir: Path, lines: list[str]) -> None:
    path = state_dir / "ingest.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))


class Handler(BaseHTTPRequestHandler):
    app: App  # set on the class made by serve()
    server_version = f"promptloom/{__version__}"

    def log_message(self, fmt, *args):  # quiet: the daemon's log is for the daemon
        pass

    # plumbing

    def _allowed_host(self) -> bool:
        """Refuse requests addressed to any other name (DNS rebinding)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # not a browser: curl, a script, an editor extension
        return urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1")

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request too large")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            # Browsers cannot send JSON across origins without asking first,
            # so this keeps other web pages from writing here.
            raise ValueError("send Content-Type: application/json")
        return json.loads(self.rfile.read(length) or b"{}")

    # routes

    def do_GET(self):
        if not self._allowed_host():
            return self._error(HTTPStatus.FORBIDDEN, "unknown host")
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._static(url.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        route = getattr(self, "get_" + url.path[5:].strip("/").replace("-", "_"), None)
        if route is None:
            return self._error(HTTPStatus.NOT_FOUND, "no such endpoint")
        try:
            with self.app.lock:
                self.app.refresh()
                data = route(query)
        except LookupError as exc:
            return self._error(HTTPStatus.NOT_FOUND, str(exc))
        self._json(data)

    def do_POST(self):
        if not self._allowed_host() or not self._same_origin():
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        url = urlparse(self.path)
        route = getattr(self, "post_" + url.path[5:].strip("/").replace("-", "_"), None)
        if not url.path.startswith("/api/") or route is None:
            return self._error(HTTPStatus.NOT_FOUND, "no such endpoint")
        try:
            body = self._body()
            with self.app.lock:
                data = route(body)
        except (ValueError, TypeError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except LookupError as exc:
            return self._error(HTTPStatus.NOT_FOUND, str(exc))
        self._json(data)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        file = (UI_DIR / name).resolve()
        if not file.is_relative_to(UI_DIR.resolve()) or not file.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind.endswith("javascript"):
            kind += "; charset=utf-8"
        self._send(200, file.read_bytes(), kind)

    # reading

    def get_overview(self, q):
        app = self.app
        records = app.records()
        tags = {}
        for record in records:
            for tag in analysis.session_tags(record, app.user):
                tags[tag] = tags.get(tag, 0) + 1
        status = app.status()
        return {
            "version": __version__,
            "sessions": len(records),
            "prompts": sum(len(r.session.prompts) for r in records),
            "notes": len(app.user.notes),
            "harnesses": {h: app.label(h) for h in sorted({r.session.harness for r in records})},
            "labels": {name: i["label"] for name, i in app.info.items()},
            "tags": dict(sorted(tags.items(), key=lambda t: -t[1])[:200]),
            "recent": [analysis.session_summary(r, app.user) for r in records[:15]],
            "captures": app.store.captures[-50:][::-1],
            "active": status.get("active"),
            "waiting": status.get("waiting", ""),
            "errors": status.get("errors", []),
            "obsidian": bool(status.get("vault")),
        }

    def get_search(self, q):
        return analysis.search(self.app.records(), self.app.user, q.get("q", ""),
                               int(q.get("limit", 200)))

    def get_sessions(self, q):
        out = []
        for record in self.app.records():
            summary = analysis.session_summary(record, self.app.user)
            if q.get("project") and summary["project"] != q["project"]:
                continue
            if q.get("harness") and summary["harness"] != q["harness"]:
                continue
            if q.get("model") and q["model"] not in summary["models"]:
                continue
            out.append(summary)
        return out

    def get_session(self, q):
        record = self.app.store.records.get(q.get("key", ""))
        if record is None:
            raise LookupError("no such session")
        resume = self.app.info.get(record.session.harness, {}).get("resume", "")
        return analysis.session_detail(record, self.app.user, self.app.web(), resume)

    def get_projects(self, q):
        return analysis.projects(self.app.records())

    def get_project(self, q):
        name = q.get("name", "")
        if not any(r.session.project == name for r in self.app.records()):
            raise LookupError("no such project")
        data = analysis.timeline(self.app.records(), name, self.app.user)
        data["links"] = [
            {"other": l.b if l.a == name else l.a, "words": l.words, "score": round(l.score, 3)}
            for l in self.app.web().projects() if name in (l.a, l.b)][:10]
        return data

    def get_models(self, q):
        return analysis.models_overview(self.app.records())

    def get_graph(self, q):
        return analysis.graph(self.app.records(), self.app.user, self.app.web(),
                              sessions=q.get("sessions") == "1")

    def get_connections(self, q):
        return analysis.connections(self.app.store, self.app.web())

    def get_notes(self, q):
        return sorted(self.app.user.notes, key=lambda n: n["updated"], reverse=True)

    def get_status(self, q):
        return self.app.status()

    # writing

    def post_notes(self, body):
        with self.app.user.editing() as user:
            if body.get("delete"):
                if not user.delete_note(str(body["delete"])):
                    raise LookupError("no such note")
                return {"deleted": body["delete"]}
            return user.put_note(body)

    def post_tags(self, body):
        key = str(body.get("key") or "")
        self.app.refresh()
        record = self.app.store.records.get(key)
        if record is None:
            raise LookupError("no such session")
        prompt = body.get("prompt")
        if prompt is not None:
            prompt = int(prompt)
            if not 0 <= prompt < len(record.session.prompts):
                raise LookupError("no such prompt")
        with self.app.user.editing() as user:
            return {"tags": user.set_tags(key, body.get("tags") or [], prompt)}

    def post_capture(self, body):
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("nothing to capture")
        return engine.capture(self.app.store, text, todo=bool(body.get("todo")),
                              context=str(body.get("context") or ""))

    def post_ingest(self, body):
        items = body if isinstance(body, list) else body.get("items", [body])
        return {"queued": self.app.ingest(items)}


def serve(cfg: dict, state_dir: Path | None = None, port: int | None = None,
          plugin_dir: Path | None = None) -> ThreadingHTTPServer:
    """Bind the server; the caller runs serve_forever(), in a thread or not."""
    app = App(cfg, state_dir or config.STATE_DIR, plugin_dir)
    handler = type("BoundHandler", (Handler,), {"app": app})
    port = port if port is not None else cfg.get("ui", {}).get("port", 8765)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd


def start_thread(cfg: dict, state_dir: Path | None = None) -> ThreadingHTTPServer | None:
    try:
        httpd = serve(cfg, state_dir)
    except OSError:
        return None  # the port is taken, most likely by another promptloom
    threading.Thread(target=httpd.serve_forever, name="promptloom-ui", daemon=True).start()
    return httpd
