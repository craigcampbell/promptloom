"""Working memory between you and your AIs: briefs and threads.

A *brief* is what every assistant should know before touching a project: where
things stand, the rules you keep repeating, what went wrong before, what is
still open. Promptloom drafts it from your history; you edit it; the composer
puts it at the top of a prompt and the MCP server hands it to any assistant
that asks.

A *thread* is a line of work: the sessions (across tools and models), drafts
and pull requests that belong to one thing you are trying to get done. They
are found by grouping sessions in a project whose prompts share unusual words
and that happened near each other in time.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import connect, extract
from .store import Record

THREAD_GAP_DAYS = 21  # sessions further apart than this are different threads
THREAD_MIN_SCORE = 0.12  # how strongly two sessions must share words to join
MAX_CONVENTIONS = 12
MAX_PITFALLS = 8
MAX_STANDING = 5

# Sentences in which someone laid down a rule, a preference or a constraint.
_RULE = re.compile(
    r"\b(always|never|don'?t|do not|must(?: not)?|should(?: not|n'?t)?|make sure|be sure|"
    r"keep (?:it|the|them|everything)|prefer|avoid|only use|use only|stick (?:to|with)|"
    r"no longer|we use|we'?re using|from now on|going forward|"
    r"every time|whenever|remember to)\b", re.I)
# Sentences about something that went wrong, worth warning the next assistant about.
_PITFALL = re.compile(
    r"\b(broke|broken|crash(?:ed|es)?|froze|frozen|hangs?|hung|doesn'?t work|didn'?t work|"
    r"not working|stopped working|still (?:fails|failing|broken|wrong)|regress(?:ed|ion)|"
    r"revert(?:ed)?|undo|lost (?:my|the|all)|wiped|deleted (?:my|the|all)|"
    r"wrong (?:file|folder|branch|project)|careful|watch out|gotcha)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?\n])\s+|\n+")
# A rule is stated as one: it starts with the rule word, or says what "we" or "you" do.
_STATED = re.compile(
    r"^(?:please\s+)?(?:always|never|don'?t|do not|make sure|be sure|keep|prefer|avoid|only|"
    r"use|stick|remember|no\b)|\b(?:we|you|i)\b.*\b(?:always|never|don'?t|do not|must|"
    r"should|prefer|avoid|only|use|stick|need to|have to)\b", re.I)
_MUSING = re.compile(r"\?\s*$|\b(i think|i don'?t think|maybe|perhaps|not sure|if you want|"
                     r"you don'?t have to|wonder)\b", re.I)
# Prompts a tool wrote itself when it resumed or compacted a session.
_MACHINE = re.compile(r"continued from a previous conversation|Resume directly|"
                      r"^<system-reminder>|^This session is being continued", re.I | re.M)


def sentences(text: str) -> list[str]:
    """What someone typed, sentence by sentence; nothing quoted, pasted or machine-made."""
    if _MACHINE.search(text or ""):
        return []
    out = []
    for raw in _SENTENCE.split(text or ""):
        line = " ".join(raw.split())
        if (20 <= len(line) <= 240 and not line.startswith(("```", "http", "{", "<", "- \"",
                                                             "-", "*", "|", ">", "#"))):
            out.append(line)
    return out


def _dedupe(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for line in lines:
        key = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()[:60]
        if key not in seen:
            seen.add(key)
            out.append(line)
    return out


def conventions(records: list[Record]) -> list[dict]:
    """Rules and preferences stated in prompts, the most repeated first."""
    found: list[tuple[str, Record, str]] = []
    for record in records:
        for prompt in record.session.prompts:
            for line in sentences(extract.redact(prompt.text)):
                if _RULE.search(line) and _STATED.search(line) and not _MUSING.search(line):
                    found.append((line, record, prompt.ts))
    # The same rule said twice (or with a few different words) is the rule that matters.
    groups: dict[str, list[tuple[str, Record, str]]] = defaultdict(list)
    for line, record, ts in found:
        words = frozenset(w for w in re.findall(r"[a-z]{3,}", line.lower())
                          if w not in connect.STOPWORDS)
        key = " ".join(sorted(words)[:6])
        groups[key].append((line, record, ts))
    ranked = sorted(groups.values(), key=lambda g: (len(g), max(t for _, _, t in g)),
                    reverse=True)
    out = []
    for group in ranked[:MAX_CONVENTIONS]:
        # The best-written wording stands for the group: a capital, a full stop.
        line, record, ts = max(group, key=lambda item: (
            item[0][0].isupper(), item[0].endswith((".", "!")), len(item[0]), item[2]))
        out.append({"text": line, "times": len(group), "session": record.session.key,
                    "title": record.title, "at": ts})
    return _dedupe_items(out)


def pitfalls(records: list[Record]) -> list[dict]:
    """Things that went wrong, most recent first."""
    found = []
    for record in records:
        for prompt in record.session.prompts:
            for line in sentences(extract.redact(prompt.text)):
                if _PITFALL.search(line) and not _RULE.search(line):
                    found.append({"text": line, "session": record.session.key,
                                  "title": record.title, "at": prompt.ts})
    found.sort(key=lambda item: item["at"], reverse=True)
    return _dedupe_items(found)[:MAX_PITFALLS]


def _dedupe_items(items: list[dict]) -> list[dict]:
    kept = _dedupe([i["text"] for i in items])
    by_text = {i["text"]: i for i in items}
    out = [by_text[t] for t in kept]
    for item in out:
        item["key"] = extract.item_id(item["text"])  # stable, for hiding one
    return out


def draft_brief(project: str, records: list[Record], hidden: list[str] | None = None) -> dict:
    """What promptloom can work out about a project on its own.

    `hidden` holds the keys of suggestions the user dismissed.
    """
    group = sorted((r for r in records if r.session.project == project),
                   key=lambda r: r.session.updated, reverse=True)
    contexts = sorted({r.session.context for r in group if r.session.context})
    standing = [{"title": r.title, "summary": r.summary, "at": r.session.updated,
                 "session": r.session.key, "harness": r.session.harness,
                 "models": sorted({p.model for p in r.session.prompts if p.model})}
                for r in group[:MAX_STANDING]]
    models = Counter(p.model for r in group for p in r.session.prompts if p.model)
    tools = Counter(r.session.harness for r in group)
    open_items = [{"text": a["text"], "session": r.session.key, "title": r.title}
                  for r in group for a in r.actions if not a.get("done")]
    themes = Counter(t for r in group for t in r.themes)
    return {
        "project": project, "contexts": contexts,
        "sessions": len(group), "prompts": sum(len(r.session.prompts) for r in group),
        "since": min((r.session.started for r in group if r.session.started), default=""),
        "models": dict(models.most_common(5)), "tools": dict(tools.most_common()),
        "themes": [t for t, _ in themes.most_common(8)],
        "standing": standing,
        "conventions": [c for c in conventions(group) if c["key"] not in (hidden or [])],
        "pitfalls": [p for p in pitfalls(group) if p["key"] not in (hidden or [])],
        "open": open_items[:12],
    }


def render_brief(auto: dict, own: str = "") -> str:
    """The brief as Markdown: what you wrote first, then what promptloom worked out.

    Of the rules it noticed, only ones said more than once go in on their own;
    the rest are suggestions in the app until you add them to your part.
    """
    name = auto["project"]
    out = [f"# About {name}", ""]
    if own.strip():
        out += [own.strip(), ""]
    if auto["contexts"]:
        out.append(f"Folder: {', '.join(auto['contexts'])}")
    if auto["models"] or auto["tools"]:
        bits = []
        if auto["tools"]:
            bits.append("tools: " + ", ".join(auto["tools"]))
        if auto["models"]:
            bits.append("models: " + ", ".join(auto["models"]))
        out.append(f"{auto['sessions']} sessions since {auto['since'][:10]}; " + "; ".join(bits))
    if auto["themes"]:
        out.append("Themes: " + ", ".join(auto["themes"]))
    if auto["standing"]:
        out += ["", "## Where things stand (most recent first)"]
        for item in auto["standing"]:
            line = f"- {item['at'][:10]} {item['title']}"
            if item["summary"]:
                line += f": {item['summary']}"
            out.append(line)
    rules = [c for c in auto["conventions"] if c["times"] > 1]
    if rules:
        out += ["", "## Rules and preferences I have stated before"]
        out += [f"- {c['text']} (said {c['times']}×)" for c in rules]
    if auto["pitfalls"]:
        out += ["", "## What has gone wrong before"]
        out += [f"- {p['at'][:10]}: {p['text']}" for p in auto["pitfalls"]]
    if auto["open"]:
        out += ["", "## Still open"]
        out += [f"- {o['text']}" for o in auto["open"]]
    return "\n".join(out).rstrip() + "\n"


def for_prompt(auto: dict, own: str = "", limit: int = 2500) -> str:
    """The brief cut to a size that fits at the top of a prompt."""
    text = render_brief(auto, own)
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n[… the rest is in promptloom: project_brief]\n"


# threads

@dataclass
class Thread:
    id: str
    project: str
    name: str
    sessions: list[Record]
    words: list[str] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)
    prs: list[dict] = field(default_factory=list)

    @property
    def started(self) -> str:
        return min((r.session.started for r in self.sessions if r.session.started), default="")

    @property
    def updated(self) -> str:
        stamps = [r.session.updated for r in self.sessions]
        stamps += [d.get("updated", "") for d in self.drafts]
        stamps += [p.get("updated", "") for p in self.prs]
        return max(stamps, default="")

    def to_dict(self, labels: dict[str, str], user_tags: dict | None = None) -> dict:
        tools = Counter(r.session.harness for r in self.sessions)
        models = Counter(p.model for r in self.sessions for p in r.session.prompts if p.model)
        return {
            "id": self.id, "project": self.project, "name": self.name,
            "started": self.started, "updated": self.updated, "words": self.words,
            "prompts": sum(len(r.session.prompts) for r in self.sessions),
            "tools": {h: {"label": labels.get(h, h), "sessions": n} for h, n in tools.items()},
            "models": dict(models.most_common(4)),
            "sessions": [{"key": r.session.key, "title": r.title, "harness": r.session.harness,
                          "updated": r.session.updated, "summary": r.summary,
                          "prompts": len(r.session.prompts)}
                         for r in sorted(self.sessions, key=lambda r: r.session.updated,
                                         reverse=True)],
            "drafts": [{"id": d["id"], "title": d["title"], "updated": d["updated"],
                        "sent": d.get("sent", "")} for d in self.drafts],
            "prs": [{"url": p["url"], "number": p["number"], "title": p["title"],
                     "waiting": p.get("waiting", 0), "mine": p.get("mine")} for p in self.prs],
            "open": [a["text"] for r in self.sessions for a in r.actions if not a.get("done")][:6],
        }


def _days_apart(a: str, b: str) -> float:
    ta, tb = extract.parse_ts(a), extract.parse_ts(b)
    if not ta or not tb:
        return 0.0
    return abs((ta - tb).total_seconds()) / 86400


def threads(records: list[Record], web: connect.Web, drafts: list[dict] | None = None,
            prs: list[dict] | None = None, settings: dict | None = None) -> list[Thread]:
    """Group sessions into lines of work; attach drafts and pull requests by folder."""
    settings = settings or {}
    parent: dict[str, str] = {r.session.key: r.session.key for r in records}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    merged: dict[frozenset, list[str]] = defaultdict(list)
    for link in web.links:
        a, b = link.a.session, link.b.session
        if a.project != b.project or link.score < THREAD_MIN_SCORE:
            continue
        if _days_apart(a.updated, b.updated) > THREAD_GAP_DAYS:
            continue
        if a.key in parent and b.key in parent:
            parent[find(a.key)] = find(b.key)
            merged[frozenset((a.key, b.key))] = link.words
    # A quick follow-up session ("yes, push it") belongs with the session in the
    # same project right before it, when it is recent and they share any word.
    by_project: dict[str, list[Record]] = defaultdict(list)
    for record in sorted(records, key=lambda r: r.session.updated):
        by_project[record.session.project].append(record)
    for group in by_project.values():
        for earlier, later in zip(group, group[1:]):
            shared = set(web.vectors.get(earlier.session.key, {})) & set(
                web.vectors.get(later.session.key, {}))
            if (find(later.session.key) != find(earlier.session.key)
                    and len(later.session.prompts) <= 2 and shared
                    and _days_apart(earlier.session.updated, later.session.updated) <= 2):
                parent[find(later.session.key)] = find(earlier.session.key)

    members: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        members[find(record.session.key)].append(record)
    overrides = settings.get("threads", {})
    out = []
    for root, group in members.items():
        group.sort(key=lambda r: r.session.updated, reverse=True)
        words: Counter = Counter()
        keys = {r.session.key for r in group}
        for pair, shared in merged.items():
            if pair <= keys:
                words.update(shared)
        # The id follows the oldest session, so a thread keeps its name as it grows.
        oldest = min(group, key=lambda r: r.session.started or r.session.updated)
        thread_id = "t-" + extract.item_id(oldest.session.key)
        own = overrides.get(thread_id, {})
        if own.get("closed"):
            continue
        biggest = max(group, key=lambda r: len(r.session.prompts))
        thread = Thread(thread_id, group[0].session.project, own.get("name") or biggest.title,
                        group, [w for w, _ in words.most_common(5)])
        out.append(thread)

    by_context: dict[str, Thread] = {}
    for thread in sorted(out, key=lambda t: t.updated):
        for record in thread.sessions:
            if record.session.context:
                by_context[record.session.context] = thread  # the newest thread wins
    for draft in drafts or []:
        thread = by_context.get(draft.get("context", ""))
        if thread and not draft.get("sent"):
            thread.drafts.append(draft)
    for pr in prs or []:
        thread = by_context.get(pr.get("checkout", ""))
        if thread:
            thread.prs.append(pr)
    out.sort(key=lambda t: t.updated, reverse=True)
    pinned = {tid for tid, own in overrides.items() if own.get("pinned")}
    out.sort(key=lambda t: t.id not in pinned)
    return out


def handoff_prompt(record: Record, brief_text: str, tool_label: str) -> str:
    """A prompt that picks up a session's work in another tool or model."""
    session = record.session
    last = [" ".join(extract.redact(p.text).split())[:400] for p in session.prompts[-3:]]
    open_items = [a["text"] for a in record.actions if not a.get("done")]
    parts = [f"I am continuing work from another session ({record.title}) in {tool_label}."]
    if record.summary:
        parts.append(f"What was done: {record.summary}")
    if last:
        parts.append("The last things I asked there:\n" + "\n".join(f"- {t}" for t in last))
    if open_items:
        parts.append("Still open:\n" + "\n".join(f"- {t}" for t in open_items))
    parts.append("Read the current state of the code before changing anything, tell me what "
                 "you find, and pick up from the open items.")
    if brief_text.strip():
        parts.append("For context:\n\n" + brief_text.strip())
    return "\n\n".join(parts)
