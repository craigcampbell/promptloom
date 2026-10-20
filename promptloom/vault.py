"""Writes the Obsidian side: session notes, hub notes that link them, and indexes.

Every note has a generated block between two markers. Anything the user writes
after the block is kept. Checkboxes ticked in Obsidian are read back by block id.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from . import canvas, connect, extract
from .store import Record, Store, write_atomic

BEGIN = "<!-- promptloom:begin (generated: edits inside this block are overwritten) -->"
END = "<!-- promptloom:end -->"
NOTES_TAIL = "\n\n## Notes\n\n"
MAX_PROMPT_CHARS = 6000
MAP_LINKS = 40  # lines drawn between projects on the map
MAP_PER_PROJECT = 12  # most recent sessions per project, plus any with a line
MAP_LEGEND = """\
## Promptloom map

Each box is a project and each card a session. Lines join sessions in different \
projects whose prompts share unusual words, and are labelled with them. **Purple** \
lines are hidden links that no theme connects. The **yellow** cards on the right are \
prompts to try.

Move, resize or add anything: promptloom keeps your layout when it redraws the map."""

_CHECK = re.compile(r"^\s*- \[( |x|X)\] .*\^([ac]-[0-9a-f]{8})\s*$", re.M)
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]')

# Where Obsidian keeps its list of vaults: native or AppImage, Flatpak, Snap.
OBSIDIAN_CONFIGS = [
    Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "obsidian/obsidian.json",
    Path("~/.var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json").expanduser(),
    Path("~/snap/obsidian/current/.config/obsidian/obsidian.json").expanduser(),
]


def safe_name(text: str, limit: int = 70) -> str:
    name = re.sub(r"\s+", " ", _UNSAFE.sub(" ", text)).strip(" .")
    return name[:limit].rstrip(" .") or "untitled"


def escaped(text: str) -> str:
    """Text shown as written in Markdown: no accidental links, tags, emphasis or HTML."""
    return re.sub(r"([\\`*_\[\]#<>|~=^$])", r"\\\1", text)


def fenced(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value in ("", None, []):
            continue
        rendered = value if isinstance(value, int) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    lines.append("---\n")
    return "\n".join(lines)


def obsidian_problem(root: Path, folder: str, configs: list[Path] | None = None) -> str:
    """Why obsidian:// links into the vault would fail, or "" if they should work.

    Obsidian resolves obsidian://open?path= only inside a vault it already
    knows, and answers any other path with a "Vault not found" dialog.
    """
    notes = (Path(root) / folder.strip("/")).resolve()
    listed = False
    for path in OBSIDIAN_CONFIGS if configs is None else configs:
        try:
            vaults = json.loads(path.read_text()).get("vaults", {})
        except (OSError, ValueError, AttributeError):
            continue
        listed = True
        for vault in vaults.values():
            known = vault.get("path")
            if known and notes.is_relative_to(Path(known).resolve()):
                return ""
    if not listed:
        return ""  # no Obsidian config to go on, so do not get in the way
    return (f'Obsidian does not know the vault {root} yet: open it once with '
            '"Open folder as vault".')


class Vault:
    def __init__(self, store: Store, cfg: dict, harness_info: dict[str, dict]):
        self.store = store
        self.enabled = bool(cfg["vault"])  # Obsidian output is optional
        self.root = Path(cfg["vault"] or ".")
        self.folder = cfg["folder"].strip("/")
        self.harness_info = harness_info  # name -> {"label": ..., "resume": ...}
        self.written: set[str] = set()
        self.term_cache: dict = {}  # session key -> words, reused while a session is unchanged

    # paths and links

    def rel(self, *parts: str) -> str:
        return "/".join((self.folder, *parts))

    def file(self, rel: str) -> Path:
        return self.root / (rel + ".md")

    @staticmethod
    def link(rel: str, alias: str) -> str:
        alias = re.sub(r"[\[\]|]", " ", alias).strip()
        return f"[[{rel}|{alias}]]"

    def session_link(self, record: Record) -> str:
        return self.link(record.note, record.title)

    def project_link(self, project: str) -> str:
        return self.link(self.rel("Projects", safe_name(project)), project)

    def theme_link(self, theme: str) -> str:
        return self.link(self.rel("Themes", safe_name(theme)), theme)

    def keyword_link(self, term: str) -> str:
        return self.link(self.rel("Keywords", safe_name(term)), term)

    def harness_link(self, name: str) -> str:
        label = self.harness_info.get(name, {}).get("label", name)
        return self.link(self.rel("Harnesses", safe_name(name)), label)

    # reading and writing notes

    def write(self, rel: str, front: dict | None, body: str, tail: str = "\n") -> None:
        path = self.file(rel)
        old = path.read_text() if path.exists() else None
        if old is not None and END in old:
            tail = old.split(END, 1)[1]
        text = (frontmatter(front) if front else "") + f"{BEGIN}\n{body.rstrip()}\n{END}{tail}"
        if text != old:
            write_atomic(path, text)
        self.written.add(rel)
        self.store.notes[rel] = path.stat().st_mtime_ns

    def edited(self) -> list[str]:
        """Notes changed on disk since promptloom last wrote them."""
        if not self.enabled:
            return []
        changed = []
        for rel, mtime in self.store.notes.items():
            try:
                if self.file(rel).stat().st_mtime_ns != mtime:
                    changed.append(rel)
            except OSError:
                changed.append(rel)  # deleted: rewrite it
        return changed

    def absorb_checks(self) -> bool:
        """Copy checkbox states the user changed in Obsidian back into the store."""
        items = {}
        for record in self.store.records.values():
            for action in record.actions:
                items[action["id"]] = action
        for capture in self.store.captures:
            if capture.get("todo"):
                items[capture["id"]] = capture

        seen: dict[str, list[bool]] = defaultdict(list)
        for rel in self.edited():
            path = self.file(rel)
            if not path.exists():
                continue
            for mark, block in _CHECK.findall(path.read_text()):
                seen[block].append(mark != " ")

        changed = False
        for block, states in seen.items():
            item = items.get(block)
            if item is None:
                continue
            # Only an edited note can disagree with the store, so a differing
            # state is the user's change.
            flipped = [s for s in states if s != bool(item.get("done"))]
            if flipped:
                item["done"] = flipped[0]
                changed = True
        return changed

    # rendering

    def render(self) -> None:
        if not self.enabled:
            return
        self.written = set()
        records = self.store.by_recency()
        self._place(records)
        web = connect.Web(records, self.term_cache)
        keywords = web.keywords()
        tagged: dict[str, list[str]] = defaultdict(list)
        for term, holders in keywords:
            for record in holders:
                tagged[record.session.key].append(term)
        related = self._related(records, web)
        for record in records:
            key = record.session.key
            self._session_note(record, related.get(key, []), tagged.get(key, []))
        self._hubs(records)
        self._keywords(keywords)
        self._action_items(records)
        self._links(records)
        self._inbox()
        self._insights()
        ideas = self._ideas(web)
        self._connections(web, ideas)
        self._map(web, ideas)
        self._home(records)
        self._prune()

    def _place(self, records: list[Record]) -> None:
        """Give each session note its path, moving the file when the title changes."""
        for record in records:
            session = record.session
            day = extract.local(session.started, "%Y-%m-%d") or "undated"
            name = f"{day} {safe_name(record.title)} ({session.session_id[:8]})"
            rel = self.rel("Sessions", safe_name(session.harness), name)
            if record.note and record.note != rel:
                old = self.file(record.note)
                if old.exists():
                    self.file(rel).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old, self.file(rel))
                if record.note in self.store.notes:
                    self.store.notes[rel] = self.store.notes.pop(record.note)
            record.note = rel

    @staticmethod
    def _related(records: list[Record], web: connect.Web) -> dict[str, list[tuple[Record, str]]]:
        """Sessions that share themes, links or unusual words, each with the reason."""
        prints = {}
        for record in records:
            urls = set()
            for prompt in record.session.prompts:
                urls.update(extract.urls(prompt.text))
            prints[record.session.key] = (set(record.themes), urls)
        related = {}
        for record in records:
            themes, urls = prints[record.session.key]
            words = {link.other(record).session.key: link for link in web.similar(record)}
            scored = []
            for other in records:
                if other is record:
                    continue
                other_themes, other_urls = prints[other.session.key]
                shared_themes = sorted(themes & other_themes)
                shared_urls = urls & other_urls
                link = words.get(other.session.key)
                score = 2 * len(shared_themes) + 3 * len(shared_urls)
                score += 8 * link.score if link else 0
                if other.session.project == record.session.project:
                    score -= 1  # the project hub already connects these
                if score >= 2:
                    why = []
                    if shared_themes:
                        why.append("themes: " + ", ".join(shared_themes))
                    if shared_urls:
                        why.append(f"{len(shared_urls)} shared link" + "s" * (len(shared_urls) > 1))
                    if link:
                        why.append("words: " + ", ".join(link.words))
                    scored.append((score, other.session.updated, other, "; ".join(why)))
            scored.sort(key=lambda item: item[:2], reverse=True)
            related[record.session.key] = [(other, why) for _, _, other, why in scored[:6]]
        return related

    def _session_line(self, record: Record, project: bool = True) -> str:
        session = record.session
        bits = [extract.local(session.updated, "%Y-%m-%d")]
        if project:
            bits.append(session.project)
        bits.append(self.harness_info.get(session.harness, {}).get("label", session.harness))
        line = f"- {self.session_link(record)} · {' · '.join(b for b in bits if b)}"
        if record.summary:
            line += f"\n    - {record.summary}"
        return line

    def _session_note(self, record: Record, related: list[tuple[Record, str]],
                      keywords: list[str]) -> None:
        session = record.session
        info = self.harness_info.get(session.harness, {})
        prompt_urls: list[str] = []
        for prompt in session.prompts:
            for url in extract.urls(prompt.text):
                if url not in prompt_urls:
                    prompt_urls.append(url)
        reply_urls = [u for u in session.reply_urls if u not in prompt_urls]

        out = [f"# {record.title}", ""]
        if record.summary:
            out += [f"> {record.summary}", ""]
        out.append(f"- **Harness:** {self.harness_link(session.harness)}")
        context = f" (`{session.context}`)" if session.context.startswith("/") else ""
        out.append(f"- **Project:** {self.project_link(session.project)}{context}")
        if record.themes:
            out.append("- **Themes:** " + ", ".join(self.theme_link(t) for t in record.themes))
        if keywords:
            out.append("- **Keywords:** " + ", ".join(self.keyword_link(k) for k in keywords))
        out.append(f"- **Session ID:** `{session.session_id}`")
        if info.get("resume"):
            out.append(f"- **Resume:** `{info['resume'].format(id=session.session_id)}`")
        span = extract.local(session.started)
        if extract.local(session.updated) != span:
            span += f" → {extract.local(session.updated)}"
        out.append(f"- **When:** {span} · {len(session.prompts)} prompts")

        if record.actions:
            out += ["", "## Action items", ""]
            for action in record.actions:
                mark = "x" if action.get("done") else " "
                out.append(f"- [{mark}] {action['text']} ^{action['id']}")
        if related:
            out += ["", "## Related sessions", ""]
            out += [self._session_line(other) + f"\n    - *Shares {why}*" for other, why in related]
        if prompt_urls or reply_urls:
            out += ["", "## Links", ""]
            out += [f"- {url}" for url in prompt_urls]
            if reply_urls:
                out += ["", "From the assistant's replies:", ""]
                out += [f"- {url}" for url in reply_urls[:40]]

        out += ["", "## Prompts", ""]
        for number, prompt in enumerate(session.prompts, 1):
            text = extract.redact(prompt.text)
            if len(text) > MAX_PROMPT_CHARS:
                text = text[:MAX_PROMPT_CHARS] + f"\n[… {len(text) - MAX_PROMPT_CHARS} more characters]"
            stamp = extract.local(prompt.ts)
            out += [f"**{number}**" + (f" · {stamp}" if stamp else ""), "", fenced(text), ""]

        front = {
            "harness": session.harness,
            "session_id": session.session_id,
            "project": session.project,
            "context": session.context,
            "branch": session.branch,
            "started": extract.local(session.started, "%Y-%m-%dT%H:%M:%S"),
            "updated": extract.local(session.updated, "%Y-%m-%dT%H:%M:%S"),
            "prompts": len(session.prompts),
            "themes": record.themes,
            "tags": ["promptloom/session"],
        }
        self.write(record.note, front, "\n".join(out), NOTES_TAIL)

    def _hubs(self, records: list[Record]) -> None:
        by_project: dict[str, list[Record]] = defaultdict(list)
        by_theme: dict[str, list[Record]] = defaultdict(list)
        by_harness: dict[str, list[Record]] = defaultdict(list)
        for record in records:
            by_project[record.session.project].append(record)
            by_harness[record.session.harness].append(record)
            for theme in record.themes:
                by_theme[theme].append(record)

        for project, group in by_project.items():
            out = [f"# {project}", ""]
            for context in sorted({r.session.context for r in group if r.session.context}):
                out.append(f"`{context}`")
            themes = self._counted(theme for r in group for theme in r.themes)
            if themes:
                out += ["", "**Themes:** " + ", ".join(
                    f"{self.theme_link(theme)} ({count})" for theme, count in themes)]
            open_items = [(a, r) for r in group for a in r.actions if not a.get("done")]
            if open_items:
                out += ["", "## Open action items", ""]
                out += [f"- {a['text']} ({self.session_link(r)})" for a, r in open_items]
            out += ["", f"## Sessions ({len(group)})", ""]
            out += [self._session_line(r, project=False) for r in group]
            self.write(self.rel("Projects", safe_name(project)),
                       {"tags": ["promptloom/project"]}, "\n".join(out))

        for theme, group in by_theme.items():
            projects = self._counted(r.session.project for r in group)
            out = [f"# {theme}", "",
                   "**Projects:** " + ", ".join(
                       f"{self.project_link(p)} ({count})" for p, count in projects),
                   "", f"## Sessions ({len(group)})", ""]
            out += [self._session_line(r) for r in group]
            self.write(self.rel("Themes", safe_name(theme)),
                       {"tags": ["promptloom/theme"]}, "\n".join(out))

        for harness, group in by_harness.items():
            label = self.harness_info.get(harness, {}).get("label", harness)
            out = [f"# {label}", "", f"## Sessions ({len(group)})", ""]
            out += [self._session_line(r) for r in group]
            self.write(self.rel("Harnesses", safe_name(harness)),
                       {"tags": ["promptloom/harness"]}, "\n".join(out))

    @staticmethod
    def _counted(values) -> list[tuple[str, int]]:
        counts: dict[str, int] = defaultdict(int)
        for value in values:
            counts[value] += 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def _keywords(self, keywords: list[tuple[str, list[Record]]]) -> None:
        for term, holders in keywords:
            projects = self._counted(r.session.project for r in holders)
            out = [f"# {term}", "",
                   "Used in " + ", ".join(
                       f"{self.project_link(p)} ({count})" for p, count in projects) + ".",
                   "", f"## Sessions ({len(holders)})", ""]
            for record in holders:
                out.append(f"- {self.session_link(record)} · {record.session.project}")
                quote = connect.snippet(record, term)
                if quote:
                    out.append(f"    - “{escaped(quote)}”")
            self.write(self.rel("Keywords", safe_name(term)),
                       {"tags": ["promptloom/keyword"]}, "\n".join(out))

    def _ideas(self, web: connect.Web, limit: int = 8) -> list[connect.Idea]:
        return connect.ideas(self.store, web, limit)

    def _connections(self, web: connect.Web, ideas: list[connect.Idea]) -> None:
        out = ["# Connections", "",
               "What links your sessions beyond their themes: the unusual words their "
               f"prompts share. {self.link(self.rel('Map.canvas'), 'Open the map')} to see "
               "it drawn, or the graph view, where keywords join the projects that use them.",
               "", "## Prompts to try", ""]
        for idea in ideas:
            out += [f"### {idea.title}", ""]
            bits = []
            if idea.where:
                bits.append(f"Run in `{idea.where}`")
            if idea.sessions:
                bits.append("from " + " and ".join(self.session_link(r) for r in idea.sessions[:4]))
            if idea.words:
                bits.append("shares " + ", ".join(idea.words))
            if bits:
                out += [" · ".join(bits), ""]
            out += [fenced(idea.prompt), ""]
        if not ideas:
            out += ["Nothing yet: ideas appear once sessions in different projects have "
                    "something in common.", ""]

        out += ["## Projects that connect", ""]
        projects = web.projects()[:15]
        for pair in projects:
            filled = max(1, round(5 * pair.score / projects[0].score))
            out.append(f"- {self.project_link(pair.a)} ↔ {self.project_link(pair.b)} "
                       f"{'●' * filled}{'○' * (5 - filled)} — {', '.join(pair.words)}")
            out += [f"    - {self.session_link(l.a)} ↔ {self.session_link(l.b)}"
                    for l in pair.links[:3]]
        if not projects:
            out.append("No two projects share anything unusual yet.")

        out += ["", "## Hidden links", "",
                "Sessions in different projects whose prompts share unusual words, though "
                "no theme connects them.", ""]
        hidden = [link for link in web.across() if link.hidden][:20]
        out += [f"- {self.session_link(l.a)} ({l.a.session.project}) ↔ "
                f"{self.session_link(l.b)} ({l.b.session.project}) — {', '.join(l.words)}"
                for l in hidden] or ["None right now."]
        self.write(self.rel("Connections"), None, "\n".join(out))

    def _card(self, record: Record) -> str:
        session = record.session
        about = record.summary
        if not about and session.prompts:
            first = " ".join(session.prompts[0].text.split())
            about = escaped(extract.redact(first[:200] + ("…" if len(first) > 200 else "")))
        label = self.harness_info.get(session.harness, {}).get("label", session.harness)
        meta = " · ".join(b for b in [extract.local(session.updated, "%Y-%m-%d"), label] if b)
        return f"**{self.session_link(record)}**\n\n{about}\n\n*{meta}*"

    def _map(self, web: connect.Web, ideas: list[connect.Idea]) -> None:
        path = self.root / self.folder / "Map.canvas"
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            old = None
        links = web.across()[:MAP_LINKS]
        shown = {r.session.key for link in links for r in (link.a, link.b)}
        shown |= {r.session.key for idea in ideas for r in idea.sessions}

        def card_id(record: Record) -> str:
            return canvas.node_id("s", record.session.key)

        groups: dict[str, canvas.Group] = {}
        for record in web.records:
            group = groups.setdefault(record.session.project, canvas.Group(record.session.project))
            if record.session.key in shown or len(group.cards) < MAP_PER_PROJECT:
                group.cards.append(canvas.Card(card_id(record), self._card(record)))
        edges = [canvas.Edge(card_id(l.a), card_id(l.b), ", ".join(l.words[:3]),
                             canvas.HIDDEN if l.hidden else "") for l in links]
        cards = []
        for idea in ideas:
            text = [f"### {idea.title}", ""]
            if idea.sessions:
                links = " and ".join(self.session_link(r) for r in idea.sessions[:4])
                text += [f"From {links}", ""]
            if idea.where:
                text += [f"Run in `{idea.where}`", ""]
            text += ["> " + line for line in idea.prompt.splitlines()]
            if idea.words:
                text += ["", "*Shares " + ", ".join(idea.words) + "*"]
            key = idea.title + "|" + "|".join(sorted(r.session.key for r in idea.sessions))
            cards.append(canvas.Card(canvas.node_id("i", key), "\n".join(text)))
        document = canvas.build(list(groups.values()), edges, cards, MAP_LEGEND, old)
        if document != old:
            write_atomic(path, json.dumps(document, indent="\t", ensure_ascii=False))

    def _action_items(self, records: list[Record]) -> None:
        open_by_project: dict[str, list[str]] = defaultdict(list)
        done = []
        for record in records:
            for action in record.actions:
                line = f"{action['text']} ({self.session_link(record)}) ^{action['id']}"
                if action.get("done"):
                    done.append(f"- [x] {line}")
                else:
                    open_by_project[record.session.project].append(f"- [ ] {line}")
        captured = [c for c in self.store.captures if c.get("todo")]

        out = ["# Action items", "",
               "Tick a box here or in the session note; the other copy follows.", ""]
        open_captured = [c for c in captured if not c.get("done")]
        if open_captured:
            out += ["## Captured", ""]
            out += [f"- [ ] {c['text']} ^{c['id']}" for c in reversed(open_captured)]
            out.append("")
        for project in sorted(open_by_project):
            out += [f"## {project}", ""] + open_by_project[project] + [""]
        if not open_captured and not open_by_project:
            out += ["Nothing open.", ""]
        done += [f"- [x] {c['text']} ^{c['id']}" for c in captured if c.get("done")]
        if done:
            out += ["## Done", ""] + done[:50]
        self.write(self.rel("Action Items"), None, "\n".join(out))

    def _links(self, records: list[Record]) -> None:
        sessions_by_url: dict[str, list[Record]] = defaultdict(list)
        for record in records:
            found: list[str] = []
            for prompt in record.session.prompts:
                found += extract.urls(prompt.text)
            for url in dict.fromkeys(found + record.session.reply_urls):
                sessions_by_url[url].append(record)
        for capture in self.store.captures:
            for url in extract.urls(capture["text"]):
                sessions_by_url.setdefault(url, [])

        def line(url: str) -> str:
            refs = ", ".join(self.session_link(r) for r in sessions_by_url[url][:8])
            return f"- {url}" + (f" — {refs}" if refs else " — captured")

        out = ["# Links", ""]
        shared = [u for u, rs in sessions_by_url.items() if len(rs) > 1]
        if shared:
            out += ["## Shared across sessions", ""] + [line(u) for u in shared] + [""]
        by_domain: dict[str, list[str]] = defaultdict(list)
        for url in sessions_by_url:
            by_domain[urlsplit(url).netloc.removeprefix("www.")].append(url)
        for domain in sorted(by_domain, key=lambda d: (-len(by_domain[d]), d)):
            out += [f"## {domain}", ""] + [line(u) for u in by_domain[domain]] + [""]
        if not sessions_by_url:
            out.append("No links captured yet.")
        self.write(self.rel("Links"), None, "\n".join(out))

    def _inbox(self) -> None:
        out = ["# Inbox", "", "Quick captures from the top bar and `promptloom capture`.", ""]
        for capture in reversed(self.store.captures):
            refs = []
            record = self.store.records.get(capture.get("session") or "")
            if record and record.note:
                refs.append(self.session_link(record))
            elif capture.get("context"):
                refs.append(self.project_link(Path(capture["context"]).name))
            where = f" ({' · '.join(refs)})" if refs else ""
            stamp = extract.local(capture["ts"])
            if capture.get("todo"):
                mark = "x" if capture.get("done") else " "
                out.append(f"- [{mark}] {capture['text']}{where} · {stamp} ^{capture['id']}")
            else:
                out.append(f"- {stamp} — {capture['text']}{where}")
        if not self.store.captures:
            out.append("Nothing captured yet.")
        self.write(self.rel("Inbox"), None, "\n".join(out))

    def _insights(self) -> None:
        insights = self.store.insights
        out = ["# Insights", ""]
        if not insights.get("data"):
            out.append("No insights yet. They are written once several sessions have been "
                       "summarised, or on demand with `promptloom insights`.")
            self.write(self.rel("Insights"), None, "\n".join(out))
            return
        by_ref = insights.get("refs", {})

        def refs(ids) -> str:
            links = []
            for ref in ids if isinstance(ids, list) else []:
                record = self.store.records.get(by_ref.get(str(ref).strip("[] "), ""))
                if record and record.note:
                    links.append(self.session_link(record))
            return ", ".join(links)

        data = insights["data"]
        out += [f"Written {extract.local(insights.get('at', ''))} from "
                f"{insights.get('session_count', 0)} sessions.", ""]
        out += ["## Opportunities", ""]
        for item in data.get("opportunities", []):
            out += [f"### {item.get('title', '')}", "", item.get("detail", ""), ""]
            linked = refs(item.get("sessions"))
            if linked:
                out += [f"Sessions: {linked}", ""]
            if isinstance(item.get("prompt"), str) and item["prompt"].strip():
                out += ["Prompt to try:", "", fenced(item["prompt"].strip()), ""]
        out += ["## Overarching themes", ""]
        for item in data.get("themes", []):
            out += [f"### {item.get('name', '')}", "", item.get("description", ""), ""]
            linked = refs(item.get("sessions"))
            if linked:
                out += [f"Sessions: {linked}", ""]
        self.write(self.rel("Insights"), None, "\n".join(out))

    def _home(self, records: list[Record]) -> None:
        open_count = sum(1 for r in records for a in r.actions if not a.get("done"))
        open_count += sum(1 for c in self.store.captures if c.get("todo") and not c.get("done"))
        nav = [
            self.link(self.rel("Action Items"), f"Action items ({open_count} open)"),
            self.link(self.rel("Connections"), "Connections"),
            self.link(self.rel("Map.canvas"), "Map"),
            self.link(self.rel("Links"), "Links"),
            self.link(self.rel("Insights"), "Insights"),
            self.link(self.rel("Inbox"), "Inbox"),
        ]
        out = ["# Promptloom", "", " · ".join(nav), ""]
        themes = self._counted(theme for r in records for theme in r.themes)
        if themes:
            out += ["**Themes:** " + ", ".join(
                f"{self.theme_link(theme)} ({count})" for theme, count in themes[:25]), ""]
        projects = self._counted(r.session.project for r in records)
        if projects:
            out += ["**Projects:** " + ", ".join(
                f"{self.project_link(p)} ({count})" for p, count in projects), ""]
        harnesses = self._counted(r.session.harness for r in records)
        if harnesses:
            out += ["**Harnesses:** " + ", ".join(
                f"{self.harness_link(h)} ({count})" for h, count in harnesses), ""]
        out += ["## Recent sessions", ""]
        out += [self._session_line(r) for r in records[:20]] or ["No sessions captured yet."]
        self.write(self.rel("Home"), None, "\n".join(out))

    def _prune(self) -> None:
        """Remove notes for hubs and sessions that no longer exist, unless annotated."""
        for rel in [r for r in self.store.notes if r not in self.written]:
            path = self.file(rel)
            if path.exists():
                tail = path.read_text().split(END, 1)[-1]
                if tail.strip() not in ("", "## Notes"):
                    continue  # the user wrote something here; leave it alone
                path.unlink()
            del self.store.notes[rel]
