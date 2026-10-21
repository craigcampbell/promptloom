"""One sync cycle: read harnesses, summarise what changed, write the vault and status."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config, connect, enrich, extract, harnesses, prs
from .model import Session
from .store import Record, Store, write_atomic
from .vault import Vault, obsidian_problem

RETRY_SECONDS = 600
MIN_SESSIONS_FOR_INSIGHTS = 3
MAX_INSIGHT_SESSIONS = 150


class Loom:
    def __init__(self, cfg: dict | None = None, state_dir: Path | None = None,
                 plugin_dir: Path | None = None):
        self.cfg = cfg or config.load()
        self.store = Store(state_dir or config.STATE_DIR)
        self.harnesses, self.errors = harnesses.build(self.cfg, plugin_dir or config.PLUGIN_DIR)
        for harness in self.harnesses:
            if harness.type == "api" and "path" not in harness.cfg:
                harness.cfg = dict(harness.cfg, path=str(self.store.dir / "ingest.jsonl"))
        self.workdir = self.store.dir / "enrich-workdir"
        self.waiting = ""  # why summarising is paused, shown in status
        try:
            self.backend = enrich.backend(self.cfg, str(self.workdir))
            self._backend_error = ""
        except enrich.EnrichError as exc:  # e.g. a provider with no key stored
            self.backend = None
            self._backend_error = str(exc)
        self._read = False  # whether the last scan read any source
        self._status: dict = {}
        self._insights_retry_at = 0.0
        info = {h.name: {"label": h.label, "resume": h.resume} for h in self.harnesses}
        self.vault = Vault(self.store, self.cfg, info)
        self.labels = {name: i["label"] for name, i in info.items()}

    # capture

    def scan(self) -> set[str]:
        """Read whatever is new in every harness. Returns the changed session keys."""
        # The command backend may itself be a harness; never capture its sessions.
        ignored = set(self.cfg["ignore_contexts"]) | {str(self.workdir)}
        changed: set[str] = set()
        self._read = False
        for harness in self.harnesses:
            try:
                paths = list(harness.sources())
            except OSError:
                continue
            for path in paths:
                source_key = f"{harness.name}|{path}"
                source = self.store.sources.setdefault(source_key, {"fp": None, "cursor": {}})
                try:
                    fingerprint = harness.fingerprint(path)
                    if fingerprint == source["fp"]:
                        continue

                    def get(session_id: str, as_harness: str = "", _h=harness,
                            _p=path) -> Session:
                        name = as_harness or _h.name
                        key = f"{name}:{session_id}"
                        if key not in self.store.records:
                            self.store.records[key] = Record(
                                Session(name, session_id, source=str(_p)))
                        return self.store.records[key].session

                    touched = harness.read(path, source["cursor"], get)
                    source["fp"] = fingerprint
                    self._read = True
                except Exception as exc:  # one unreadable file must not stop the rest
                    self.errors.append(f"{harness.name}: {path.name}: {exc}")
                    continue
                changed.update(f"{harness.name}:{sid}" for sid in touched)
        for key in [k for k, r in self.store.records.items() if r.session.context in ignored]:
            del self.store.records[key]
            changed.discard(key)
        return changed

    def reread(self) -> None:
        """Read every harness file again from the start, keeping summaries and to-dos.

        Run once when the state predates a field the adapters now record (the
        model behind each prompt). Sessions whose files are gone keep what they had.
        """
        for record in self.store.records.values():
            session = record.session
            if session.source and Path(session.source).exists():
                session.prompts.clear()
                session.reply_urls.clear()
        self.store.sources.clear()

    # summarising

    def pending(self, force: bool = False) -> list[Record]:
        """Sessions with prompts the model has not seen, most recent first."""
        now = time.time()
        first = self.cfg["enrich"].get("idle_seconds", 60)
        again = self.cfg["enrich"].get("refresh_idle_seconds", 900)
        due = []
        for record in self.store.by_recency():
            seen = record.enrich.get("prompt_count", 0)
            if len(record.session.prompts) <= seen:
                continue
            if not force:
                if record.retry_at > now:
                    continue
                # A first summary comes quickly so the session gets a title; after
                # that, wait until the work has moved on before redoing it.
                updated = extract.parse_ts(record.session.updated)
                if updated and now - updated.timestamp() < (again if seen else first):
                    continue
            due.append(record)
        return due

    def enrich_one(self, record: Record) -> bool:
        session = record.session
        if self._backend_error:
            # Misconfigured, not switched off: leave sessions pending for later.
            self.waiting = self._backend_error
            return False
        if self.backend is None:
            for text in extract.heuristic_actions([p.text for p in session.prompts]):
                self._add_action(record, text)
            record.enrich["prompt_count"] = len(session.prompts)
            return True
        self.waiting = self.backend.ready()
        if self.waiting:
            return False
        recorded = [a["text"] for a in record.actions]
        prompt = enrich.session_prompt(session, self.store.known_themes()[:60], recorded)
        try:
            data = self.backend.complete(prompt, enrich.SESSION_SCHEMA)
        except enrich.EnrichError as exc:
            self.waiting = str(exc)
            record.retry_at = time.time() + RETRY_SECONDS
            return False
        title = data.get("title")
        # Keep the first title: a note that keeps renaming itself is hard to find.
        if isinstance(title, str) and title.strip() and not record.enrich.get("title"):
            record.enrich["title"] = title.strip().strip('"').rstrip(".")[:80]
        if isinstance(data.get("summary"), str):
            record.enrich["summary"] = " ".join(data["summary"].split())
        record.enrich["themes"] = enrich.clean_themes(data.get("themes"))
        record.enrich["prompt_count"] = len(session.prompts)
        record.enrich["at"] = extract.now_ts()
        for text in data.get("action_items") or []:
            if isinstance(text, str):
                self._add_action(record, text)
        return True

    @staticmethod
    def _add_action(record: Record, text: str) -> None:
        text = " ".join(text.split()).strip("-* ")
        if not text:
            return
        action_id = "a-" + extract.item_id(record.session.key + " " + text)
        if all(a["id"] != action_id for a in record.actions):
            record.actions.append(
                {"id": action_id, "text": text, "done": False, "ts": extract.now_ts()})

    def write_insights(self, force: bool = False) -> bool:
        """Ask the model what connects the sessions. Returns True when rewritten."""
        if self.backend is None:
            self.waiting = self._backend_error
            return False
        records = [r for r in self.store.by_recency() if r.summary][:MAX_INSIGHT_SESSIONS]
        if len(records) < MIN_SESSIONS_FOR_INSIGHTS:
            return False
        last = self.store.insights
        if not force:
            if time.time() < self._insights_retry_at:
                return False
            newest = max(r.enrich.get("at", "") for r in records)
            if newest <= last.get("at", ""):
                return False  # nothing summarised since the last run
            previous = extract.parse_ts(last.get("at", ""))
            gap = self.cfg.get("insights_hours", 6) * 3600
            if previous and time.time() - previous.timestamp() < gap:
                return False
        self.waiting = self.backend.ready()
        if self.waiting:
            return False
        refs, lines = {}, []
        for number, record in enumerate(records, 1):
            ref = f"S{number}"
            refs[ref] = record.session.key
            lines.append(" | ".join([
                ref, record.session.project, record.session.context, record.session.harness,
                record.title, ", ".join(record.themes), record.summary]))
        ref_of = {key: ref for ref, key in refs.items()}
        web = connect.Web(self.store.by_recency(), self.vault.term_cache)
        hints = [f"{ref_of[l.a.session.key]} + {ref_of[l.b.session.key]}: {', '.join(l.words)}"
                 for l in web.across()
                 if l.hidden and l.a.session.key in ref_of and l.b.session.key in ref_of][:12]
        try:
            data = self.backend.complete(enrich.insights_prompt(lines, hints),
                                         enrich.INSIGHTS_SCHEMA)
        except enrich.EnrichError as exc:
            self.waiting = str(exc)
            self._insights_retry_at = time.time() + RETRY_SECONDS
            return False
        self.store.insights = {
            "at": extract.now_ts(), "data": data, "refs": refs, "session_count": len(records)}
        return True

    # output

    def capture(self, text: str, todo: bool = False, context: str = "") -> dict:
        """Queue a quick capture, attached to the active session when there is one."""
        return capture(self.store, text, todo, context)

    def ui_url(self) -> str:
        ui = self.cfg.get("ui", {})
        return f"http://127.0.0.1:{ui.get('port', 8765)}/" if ui.get("enabled", True) else ""

    def read_status(self) -> dict:
        return read_status(self.store)

    def status(self) -> dict:
        records = self.store.by_recency()
        now = time.time()
        window = self.cfg.get("active_minutes", 30) * 60

        def entry(record: Record) -> dict:
            session = record.session
            return {
                "key": session.key,
                "title": record.title,
                "harness": session.harness,
                "harness_label": self.labels.get(session.harness, session.harness),
                "session_id": session.session_id,
                "project": session.project,
                "context": session.context,
                "updated": session.updated,
                "prompts": len(session.prompts),
                "model": session.model,
                "note": str(self.vault.file(record.note))
                        if record.note and self.vault.enabled else "",
            }

        active = None
        if records:
            updated = extract.parse_ts(records[0].session.updated)
            if updated and now - updated.timestamp() < window:
                active = entry(records[0])
        open_actions = sum(1 for r in records for a in r.actions if not a.get("done"))
        open_actions += sum(
            1 for c in self.store.captures if c.get("todo") and not c.get("done"))
        folder = self.vault.root / self.vault.folder
        obsidian = self.vault.enabled

        def note(name: str) -> str:
            return str(folder / name) if obsidian else ""

        return {
            "updated": extract.now_ts(),
            "active": active,
            "recent": [entry(r) for r in records[:12]],
            "sessions": len(records),
            "open_actions": open_actions,
            "summarising": len(self.pending(force=True)),
            "waiting": self.waiting,
            "ui": self.ui_url(),
            "vault": str(self.vault.root) if obsidian else "",
            "home": note("Home.md"),
            "actions": note("Action Items.md"),
            "insights": note("Insights.md"),
            "inbox": note("Inbox.md"),
            "connections": note("Connections.md"),
            "map": note("Map.canvas"),
            "vault_problem": obsidian_problem(self.vault.root, self.vault.folder)
                             if obsidian else "",
            "prs": prs.summary(self.store.dir, self.cfg),
            "errors": self.errors[-5:],
        }

    def _publish(self, render: bool, save: bool) -> dict:
        """Write the vault (when asked), the state and the status file."""
        if render:
            self.vault.render()
        if render or save:
            self.store.save()
        status = self.status()
        # Rewriting an unchanged status would wake the top bar for nothing.
        comparable = {k: v for k, v in status.items() if k != "updated"}
        if comparable != self._status or not self.store.status_path.exists():
            write_atomic(self.store.status_path, json.dumps(status, ensure_ascii=False))
            self._status = comparable
        return status

    def sync(self, enrich_limit: int = 1, force_enrich: bool = False, progress=None) -> dict:
        """Run one full cycle under the lock. Returns a summary of what happened.

        `progress`, if given, is called with each record after it is summarised.
        """
        with self.store.lock():
            self.store.load()
            if self.store.version < 2:
                self.reread()
            self.errors = self.errors[-20:]
            changed = self.scan()
            captured = self.store.drain_spool()
            checks = self.vault.absorb_checks()
            # Publish what was captured before the model runs: a summary can
            # take minutes, and the top bar should not wait for it.
            dirty = bool(changed or captured or checks)
            first = self.vault.enabled and not self.store.notes
            status = self._publish(dirty or bool(self.vault.edited()) or first,
                                   save=self._read or captured > 0)

            enriched = 0
            self.waiting = ""
            for record in self.pending(force_enrich)[:enrich_limit]:
                if not self.enrich_one(record):
                    break
                enriched += 1
                if enrich_limit > 1:
                    self.store.save()  # a long backfill should survive being interrupted
                if progress:
                    progress(record)
            insights = False
            if enriched == 0 and not self.pending():
                insights = self.write_insights()
            if enriched or insights or self.waiting != status["waiting"]:
                status = self._publish(bool(enriched or insights), save=False)
        return {"changed": len(changed), "captured": captured, "enriched": enriched,
                "insights": insights, "status": status}


def read_status(store: Store) -> dict:
    try:
        return json.loads(store.status_path.read_text())
    except (OSError, ValueError):
        return {}


def capture(store: Store, text: str, todo: bool = False, context: str = "") -> dict:
    """Queue a quick capture, attached to the active session when there is one."""
    text = " ".join(text.split())
    lowered = text.lower()
    for prefix in ("todo:", "todo ", "- [ ]"):
        if lowered.startswith(prefix):
            todo, text = True, text[len(prefix):].strip()
            break
    active = read_status(store).get("active") or {}
    item = {
        "id": "c-" + extract.item_id(f"{time.time()} {text}"),
        "ts": extract.now_ts(),
        "text": extract.redact(text),
        "todo": todo,
        "done": False,
        "context": context or active.get("context", ""),
        "session": "" if context else active.get("key", ""),
    }
    store.spool_capture(item)
    return item
