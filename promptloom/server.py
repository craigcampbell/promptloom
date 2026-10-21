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

from . import (__version__, analysis, brief, config, connect, desktop, engine, extract,
               gitlocal, harnesses, launch, prs, schedules)
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
        self.checking: threading.Thread | None = None  # a GitHub check started from the page
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
            "drafts": sum(1 for d in app.user.drafts if not d.get("sent")),
            "prs": prs.summary(app.state_dir, app.cfg),
            "open_actions": sum(1 for r in records for a in r.actions if not a.get("done")),
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

    def _brief(self, project: str) -> dict:
        app = self.app
        records = app.records()
        if not any(r.session.project == project for r in records):
            raise LookupError("no such project")
        own = app.user.briefs.get(project, {})
        auto = brief.draft_brief(project, records, own.get("hidden", []))
        return {"auto": auto, "own": own.get("text", ""), "own_updated": own.get("updated", ""),
                "text": brief.render_brief(auto, own.get("text", "")),
                "prompt": brief.for_prompt(auto, own.get("text", ""))}

    def get_brief(self, q):
        return self._brief(q.get("project", ""))

    def get_threads(self, q):
        app = self.app
        state = prs.load(app.state_dir)
        labels = {n: i["label"] for n, i in app.info.items()}
        found = brief.threads(app.records(), app.web(), app.user.drafts, state["prs"],
                              {"threads": app.user.threads})
        out = []
        for thread in found:
            if q.get("project") and thread.project != q["project"]:
                continue
            item = thread.to_dict(labels)
            item["pinned"] = bool(app.user.threads.get(thread.id, {}).get("pinned"))
            out.append(item)
        return out

    def get_handoff(self, q):
        """A prompt that continues a session elsewhere, for the composer."""
        record = self.app.store.records.get(q.get("key", ""))
        if record is None:
            raise LookupError("no such session")
        tool = next((t["label"] for t in launch.tools(self.app.cfg)
                     if t["name"] == q.get("tool")), "another tool")
        text = self._brief(record.session.project)["prompt"]
        return {"prompt": brief.handoff_prompt(record, text, tool),
                "context": record.session.context, "project": record.session.project}

    def get_tidbits(self, q):
        """Instant, model-free notes on a draft: what the history says about its words."""
        app = self.app
        context = q.get("context", "")
        project = next((r.session.project for r in app.records() if r.session.context == context),
                       Path(context).name if context else "")
        hidden = app.user.briefs.get(project, {}).get("hidden", [])
        return {"project": project,
                "items": brief.tidbits(q.get("q", ""), project, app.records(), app.web(), hidden)}

    def get_tools(self, q):
        """What "New prompt" can start, the models each has used, and where."""
        app = self.app
        records = app.records()
        contexts: dict[str, dict] = {}
        for record in records:
            context = record.session.context
            if context.startswith("/") or (len(context) > 2 and context[1] == ":"):
                entry = contexts.setdefault(context, {"context": context,
                                                      "project": record.session.project,
                                                      "last": ""})
                entry["last"] = max(entry["last"], record.session.updated)
        return {
            "tools": launch.tools(app.cfg, records),
            "contexts": sorted(contexts.values(), key=lambda c: c["last"], reverse=True),
            "weavers": launch.weavers(app.cfg),
            "stats": analysis.tools_overview(records, {n: i["label"] for n, i in app.info.items()}),
        }

    def get_drafts(self, q):
        return sorted(self.app.user.drafts, key=lambda d: d["updated"], reverse=True)

    def get_draft(self, q):
        draft = self.app.user.draft(q.get("id", ""))
        if draft is None:
            raise LookupError("no such draft")
        return draft

    def get_notes(self, q):
        notes = sorted(self.app.user.notes, key=lambda n: n["updated"], reverse=True)
        return [{**n, "next_run": schedules.next_run(n)} for n in notes]

    def get_status(self, q):
        return self.app.status()

    # pull requests and worktrees

    def get_prs(self, q):
        cfg = self.app.cfg
        state = prs.load(self.app.state_dir)
        runs = [{k: v for k, v in run.items() if k != "output"} | {"has_output": bool(
            run["kind"] == "review" and run["status"] == "done")} for run in state["runs"]]
        return {
            "settings": {k: prs.settings(cfg).get(k) for k in
                         ("enabled", "notify", "agent", "worktrees", "poll_minutes")},
            "agents": [{"name": name, "label": a["label"], "available": a["available"]}
                       for name, a in prs.agents(cfg).items()],
            "login": state.get("login", ""), "checked": state.get("checked", ""),
            "checking": bool(self.app.checking and self.app.checking.is_alive()),
            "error": state.get("error", ""), "prs": state["prs"], "runs": runs[::-1],
        }

    def get_pr_run(self, q):
        run = next((r for r in prs.load(self.app.state_dir)["runs"] if r["id"] == q.get("id")),
                   None)
        if run is None:
            raise LookupError("no such run")
        text = ""
        if run["kind"] == "review" and run["status"] == "done":
            try:
                text = Path(run["output"]).read_text()
            except OSError:
                text = ""
        return {**run, "text": text}

    def get_pr_prompt(self, q):
        """The prompt a review or fix would start with, before the diff is added."""
        state = prs.load(self.app.state_dir)
        try:
            pr = prs.find(state, q.get("url", ""))
        except prs.PRError as exc:
            raise LookupError(str(exc)) from exc
        extra = q.get("extra", "")
        if q.get("kind") == "fix":
            worktree = q.get("worktree", "1") == "1" and bool(pr["checkout"])
            review = prs.latest_review(self.app.state_dir, pr["url"])
            return {"prompt": prs.fix_prompt(pr, review, worktree, extra)}
        try:
            _, where = prs.review_place(pr)
        except gitlocal.GitError as exc:
            raise ValueError(str(exc)) from exc
        return {"prompt": prs.review_prompt(pr, "[the pull request's description]",
                                            "[the diff, fetched when the review starts]",
                                            where, extra)}

    def get_worktrees(self, q):
        paths = prs.local_checkouts(self.app.cfg, self.app.records())
        listing = gitlocal.overview(paths)
        return {"checkouts": sorted(({"name": i["name"], "checkout": i["checkout"],
                                      "branch": i["branch"]} for i in listing),
                                     key=lambda c: c["name"].lower()),
                "repos": [i for i in listing if i["worktrees"]]}

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

    def post_drafts(self, body):
        with self.app.user.editing() as user:
            if body.get("delete"):
                if not user.delete_draft(str(body["delete"])):
                    raise LookupError("no such draft")
                return {"deleted": body["delete"]}
            return user.put_draft(body)

    def post_brief(self, body):
        project = str(body.get("project") or "")
        if not any(r.session.project == project for r in self.app.records()):
            raise LookupError("no such project")
        with self.app.user.editing() as user:
            user.put_brief(project, None if body.get("text") is None else str(body["text"]),
                           str(body.get("hide") or ""), str(body.get("unhide") or ""))
        return self._brief(project)

    def post_thread(self, body):
        thread_id = str(body.get("id") or "")
        if not thread_id.startswith("t-"):
            raise ValueError("which thread?")
        fields = {k: body[k] for k in ("name", "closed", "pinned") if k in body}
        if "name" in fields:
            fields["name"] = str(fields["name"]).strip()[:120]
        with self.app.user.editing() as user:
            return user.set_thread(thread_id, **fields)

    def post_note_run(self, body):
        """Run a note as a prompt now, the way its schedule would."""
        app = self.app
        note = app.user.note(str(body.get("id") or ""))
        if note is None:
            raise LookupError("no such note")
        try:
            return schedules.run_note(app.cfg, app.state_dir, app.user, note, app.records(),
                                      how="by hand")
        except (launch.LaunchError, desktop.DesktopError) as exc:
            raise ValueError(str(exc)) from exc

    def post_launch(self, body):
        """Start a tool on a prompt, in a terminal; mark the draft as sent."""
        name = str(body.get("tool") or "")
        prompt = str(body.get("prompt") or "")
        try:
            done = launch.start(self.app.cfg, self.app.state_dir, name, prompt,
                                str(body.get("model") or ""), str(body.get("context") or ""))
        except (launch.LaunchError, desktop.DesktopError) as exc:
            raise ValueError(str(exc)) from exc
        if body.get("draft") or body.get("note"):
            with self.app.user.editing() as user:
                if body.get("draft"):
                    user.mark_sent(str(body["draft"]), done["tool"])
                if body.get("note"):
                    user.note_ran(str(body["note"]), "from the composer")
        return done

    def post_hints(self, body):
        """A cheap model's notes on a draft in progress."""
        app = self.app
        choice = str(body.get("weaver") or "")
        if not choice:
            raise ValueError("pick a model for hints")
        context = str(body.get("context") or "")
        project = next((r.session.project for r in app.records() if r.session.context == context),
                       Path(context).name if context else "")
        brief_text = ""
        try:
            brief_text = self._brief(project)["prompt"] if project else ""
        except LookupError:
            pass
        tool_label = next((t["label"] for t in launch.tools(app.cfg)
                           if t["name"] == body.get("tool")), "")
        workdir = app.state_dir / "enrich-workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            return launch.hints(app.cfg, str(workdir), choice, str(body.get("body") or ""),
                                tool_label, project, brief_text)
        except launch.LaunchError as exc:
            raise ValueError(str(exc)) from exc

    def post_weave(self, body):
        """Ask a model for a stronger version of a draft; keep both."""
        app = self.app
        draft_id = str(body.get("draft") or "")
        text = str(body.get("body") or "")
        choice = str(body.get("weaver") or "")
        if not choice:
            found = launch.weavers(app.cfg)
            if not found:
                raise ValueError("no model to weave with: start Ollama, or store a provider "
                                 "key with `promptloom provider set`")
            choice = next((w["id"] for w in found if w["default"]), found[0]["id"])
        tool_label = next((t["label"] for t in launch.tools(app.cfg)
                           if t["name"] == body.get("tool")), "")
        context = str(body.get("context") or "")
        project = Path(context).name if context else ""
        workdir = app.state_dir / "enrich-workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            woven = launch.weave(app.cfg, str(workdir), choice, text,
                                 tool_label, project, launch.recent_context(app.records(), context))
        except launch.LaunchError as exc:
            raise ValueError(str(exc)) from exc
        by = next((w["label"] for w in launch.weavers(app.cfg) if w["id"] == choice), choice)
        with app.user.editing() as user:
            if not draft_id or user.draft(draft_id) is None:
                draft_id = user.put_draft({"body": text, "tool": body.get("tool", ""),
                                           "model": body.get("model", ""),
                                           "context": context})["id"]
            draft = user.weave_draft(draft_id, woven["prompt"], by, woven["notes"])
        return draft

    def post_open_terminal(self, body):
        path = str(body.get("context") or "")
        if not Path(path).expanduser().is_dir():
            raise ValueError("not a folder")
        try:
            launch.open_project(self.app.cfg, str(Path(path).expanduser()))
        except desktop.DesktopError as exc:
            raise ValueError(str(exc)) from exc
        return {"opened": path}

    def post_pr_settings(self, body):
        allowed = {"enabled": bool, "notify": bool, "worktrees": bool, "agent": str}
        values = {k: kind(body[k]) for k, kind in allowed.items() if k in body}
        if "agent" in values and values["agent"] not in prs.agents(self.app.cfg):
            raise ValueError(f"unknown agent {values['agent']!r}")
        saved = config.save_settings("prs", values)
        self.app.cfg.setdefault("prs", {}).update(values)
        return saved

    def post_pr_check(self, body):
        """Ask GitHub in the background; the page polls /api/prs until it is done."""
        app = self.app
        if not (app.checking and app.checking.is_alive()):
            records = app.records()
            app.checking = threading.Thread(
                target=prs.refresh, args=(app.cfg, app.state_dir, records), daemon=True)
            app.checking.start()
        return {"checking": True}

    def post_pr_review(self, body):
        try:
            return prs.start_review(self.app.cfg, self.app.state_dir, str(body.get("url", "")),
                                    str(body.get("agent") or ""), str(body.get("extra") or ""))
        except prs.PRError as exc:
            raise ValueError(str(exc)) from exc

    def post_pr_fix(self, body):
        try:
            return prs.start_fix(self.app.cfg, self.app.state_dir, str(body.get("url", "")),
                                 str(body.get("agent") or ""), str(body.get("extra") or ""),
                                 bool(body.get("worktree", True)))
        except prs.PRError as exc:
            raise ValueError(str(exc)) from exc

    def _checkout(self, path: str) -> Path:
        """A checkout promptloom knows about; nothing else is touched."""
        for checkout in prs.local_checkouts(self.app.cfg, self.app.records()):
            if str(checkout) == path:
                return checkout
        raise LookupError("not a checkout promptloom knows")

    def post_worktree_add(self, body):
        branch = str(body.get("branch") or "").strip()
        if not branch:
            raise ValueError("name the branch")
        try:
            path = gitlocal.add(self._checkout(str(body.get("checkout", ""))), branch,
                                str(body.get("base") or "").strip())
        except gitlocal.GitError as exc:
            raise ValueError(str(exc)) from exc
        return {"path": str(path)}

    def post_worktree_clean(self, body):
        only = body.get("paths")
        if not isinstance(only, list) or not only:
            raise ValueError("say which worktrees to remove")
        paths = prs.local_checkouts(self.app.cfg, self.app.records())
        return {"results": gitlocal.clean(paths, [str(p) for p in only],
                                          bool(body.get("delete_branches")))}

    def post_worktree_terminal(self, body):
        path = str(body.get("path", ""))
        known = {i["checkout"] for i in gitlocal.overview(
            prs.local_checkouts(self.app.cfg, self.app.records()))}
        known |= {t["path"] for i in gitlocal.overview([Path(k) for k in known])
                  for t in i["worktrees"]}
        if path not in known:
            raise LookupError("not a checkout or worktree promptloom knows")
        try:
            desktop.open_terminal(self.app.cfg, path)
        except desktop.DesktopError as exc:
            raise ValueError(str(exc)) from exc
        return {"opened": path}


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
