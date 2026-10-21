"""Everything the app shows that is worked out from the captured sessions:
tags, search, project timelines, which models worked on what, and the graph.

All of it is recomputed from the store on demand; nothing here is saved.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from functools import lru_cache

from . import connect, extract
from .store import Record
from .userdata import UserData

UNKNOWN_MODEL = "unknown"
SNIPPET = 110
_FILTER = re.compile(r'(\w+):("[^"]*"|\S+)')
FILTER_KEYS = ("tag", "model", "harness", "project", "theme", "branch", "after", "before", "in")


@lru_cache(maxsize=512)
def is_app(context: str) -> bool:
    return connect.is_app(context)


def model_family(model: str) -> str:
    """"claude-opus-5-5" -> "claude-opus": versions of one line of models group together."""
    if not model:
        return UNKNOWN_MODEL
    model = model.split("/")[-1]
    parts = re.split(r"[-_:]", model.lower())
    kept = [p for p in parts if p and not re.fullmatch(r"v?\d+(\.\d+)*|latest", p)]
    return "-".join(kept[:2]) or model


def session_models(record: Record) -> list[str]:
    seen: list[str] = []
    for prompt in record.session.prompts:
        if prompt.model and prompt.model not in seen:
            seen.append(prompt.model)
    if record.session.model and record.session.model not in seen:
        seen.append(record.session.model)
    return seen


def session_tags(record: Record, user: UserData) -> list[str]:
    """Tags worked out from the session, then the ones people added."""
    session = record.session
    tags = [f"harness:{session.harness}", f"project:{session.project}"]
    tags += [f"model:{m}" for m in session_models(record)]
    if session.branch:
        tags.append(f"branch:{session.branch}")
    tags += [f"theme:{t}" for t in record.themes]
    tags += [t for t in user.session_tags.get(session.key, []) if t not in tags]
    return tags


def prompt_tags(record: Record, index: int, user: UserData) -> list[str]:
    session = record.session
    prompt = session.prompts[index]
    tags = [f"model:{prompt.model or UNKNOWN_MODEL}", f"harness:{session.harness}",
            f"project:{session.project}"]
    if session.branch:
        tags.append(f"branch:{session.branch}")
    tags += user.prompt_tags.get(f"{session.key}#{index}", [])
    tags += user.session_tags.get(session.key, [])
    return list(dict.fromkeys(tags))


# search


def parse_query(query: str) -> tuple[list[str], dict[str, list[str]]]:
    """Split "redis tag:bug model:opus" into words and filters."""
    filters: dict[str, list[str]] = defaultdict(list)

    def take(match: re.Match) -> str:
        key = match.group(1).lower()
        if key not in FILTER_KEYS:
            return match.group(0)
        filters[key].append(match.group(2).strip('"').lower())
        return " "

    rest = _FILTER.sub(take, query)
    phrases = [p.lower() for p in re.findall(r'"([^"]+)"', rest)]
    words = [w.lower() for w in re.sub(r'"[^"]*"', " ", rest).split()]
    return phrases + words, dict(filters)


def _tag_matches(wanted: str, tags: list[str]) -> bool:
    # "opus" matches "model:claude-opus-5-5"; "model:opus" only model tags.
    for tag in tags:
        if wanted == tag or wanted in tag.split(":", 1)[-1]:
            if ":" not in wanted or tag.startswith(wanted.split(":", 1)[0] + ":"):
                return True
    return False


def _excerpt(text: str, terms: list[str]) -> str:
    text = " ".join(extract.redact(text).split())
    lowered = text.lower()
    at = min((lowered.find(t) for t in terms if t in lowered), default=0)
    start = max(0, at - SNIPPET // 3)
    end = start + SNIPPET * 2
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def search(records: list[Record], user: UserData, query: str, limit: int = 200) -> dict:
    """Prompts and notes matching every word and filter, newest first."""
    terms, filters = parse_query(query)

    def wanted(key: str, value: str) -> bool:
        return all(v in value.lower() for v in filters.get(key, []))

    hits = []
    total = 0
    for record in records:
        session = record.session
        if not (wanted("harness", session.harness) and wanted("project", session.project)
                and wanted("branch", session.branch)
                and wanted("in", session.context)):
            continue
        if filters.get("theme") and not all(
                any(f in t.lower() for t in record.themes) for f in filters["theme"]):
            continue
        for index, prompt in enumerate(session.prompts):
            day = prompt.ts[:10]
            if any(day and day < f for f in filters.get("after", [])):
                continue
            if any(day and day > f for f in filters.get("before", [])):
                continue
            if not wanted("model", prompt.model or UNKNOWN_MODEL):
                continue
            tags = prompt_tags(record, index, user)
            if filters.get("tag") and not all(_tag_matches(t, tags) for t in filters["tag"]):
                continue
            haystack = prompt.text.lower()
            if not all(t in haystack for t in terms):
                continue
            total += 1
            if len(hits) < limit:
                hits.append({
                    "kind": "prompt", "key": session.key, "index": index, "ts": prompt.ts,
                    "title": record.title, "text": _excerpt(prompt.text, terms),
                    "model": prompt.model, "harness": session.harness,
                    "project": session.project, "tags": tags})
    notes = []
    for note in user.notes:
        text = f"{note['title']}\n{note['body']}".lower()
        if filters.get("tag") and not all(_tag_matches(t, note["tags"]) for t in filters["tag"]):
            continue
        if filters.get("project") and not all(
                f in note.get("project", "").lower() for f in filters["project"]):
            continue
        if all(t in text for t in terms) and not filters.get("model"):
            notes.append({"kind": "note", "id": note["id"], "title": note["title"],
                          "text": _excerpt(note["body"], terms), "ts": note["updated"],
                          "tags": note["tags"], "project": note.get("project", "")})
    hits.sort(key=lambda h: h["ts"], reverse=True)
    notes.sort(key=lambda h: h["ts"], reverse=True)
    return {"query": query, "terms": terms, "filters": filters, "total": total,
            "hits": hits, "notes": notes}


def stream(records: list[Record], user: UserData, query: str = "", before: str = "",
           limit: int = 60) -> dict:
    """Every prompt, newest first, in pages: the Stream. `before` is the last
    timestamp of the previous page; the filters are the search ones."""
    terms, filters = parse_query(query)

    def wanted(key: str, value: str) -> bool:
        return all(v in value.lower() for v in filters.get(key, []))

    rows = []
    for record in records:
        session = record.session
        if not (wanted("harness", session.harness) and wanted("project", session.project)):
            continue
        for index, prompt in enumerate(session.prompts):
            if before and prompt.ts >= before:
                continue
            if not wanted("model", prompt.model or UNKNOWN_MODEL):
                continue
            tags = prompt_tags(record, index, user)
            if filters.get("tag") and not all(_tag_matches(t, tags) for t in filters["tag"]):
                continue
            if terms and not all(t in prompt.text.lower() for t in terms):
                continue
            rows.append((prompt.ts, record, index, tags))
    rows.sort(key=lambda r: r[0], reverse=True)
    page = rows[:limit]
    notes_by_prompt: dict[str, list[dict]] = {}
    for note in user.notes:
        if note.get("prompt"):
            notes_by_prompt.setdefault(note["prompt"], []).append(
                {"id": note["id"], "title": note["title"]})
    collected: dict[str, list[str]] = {}
    for collection in user.collections:
        for ref in collection["items"]:
            collected.setdefault(ref, []).append(collection["id"])
    items = []
    for ts, record, index, tags in page:
        session = record.session
        ref = f"{session.key}#{index}"
        prompt = session.prompts[index]
        items.append({"ref": ref, "key": session.key, "index": index, "ts": ts,
                      "title": record.title, "text": extract.redact(prompt.text),
                      "model": prompt.model, "harness": session.harness,
                      "project": session.project, "tags": tags,
                      "user_tags": user.prompt_tags.get(ref, []),
                      "notes": notes_by_prompt.get(ref, []),
                      "collections": collected.get(ref, [])})
    return {"items": items, "total": len(rows), "terms": terms,
            "next": page[-1][0] if len(rows) > limit else ""}


def user_tags(records: list[Record], user: UserData) -> list[dict]:
    """The tags people added, with how many prompts and notes carry each."""
    counts: Counter = Counter()
    for tags in user.prompt_tags.values():
        counts.update(tags)
    for key, tags in user.session_tags.items():
        n = next((len(r.session.prompts) for r in records if r.session.key == key), 0)
        for tag in tags:
            counts[tag] += n
    for note in user.notes:
        counts.update(note.get("tags", []))
    return [{"tag": t, "count": n, "color": user.tag_colors.get(t, "")}
            for t, n in counts.most_common()]


# sessions and projects


def session_summary(record: Record, user: UserData) -> dict:
    session = record.session
    return {
        "key": session.key, "title": record.title, "harness": session.harness,
        "session_id": session.session_id, "project": session.project,
        "context": session.context, "branch": session.branch, "started": session.started,
        "updated": session.updated, "prompts": len(session.prompts),
        "models": session_models(record), "summary": record.summary,
        "themes": record.themes, "tags": session_tags(record, user),
        "user_tags": user.session_tags.get(session.key, []),
    }


def session_detail(record: Record, user: UserData, web: connect.Web | None,
                   resume: str = "") -> dict:
    session = record.session
    out = session_summary(record, user)
    out["prompts"] = [
        {"index": i, "ts": p.ts, "text": extract.redact(p.text), "model": p.model,
         "tags": prompt_tags(record, i, user),
         "user_tags": user.prompt_tags.get(f"{session.key}#{i}", [])}
        for i, p in enumerate(session.prompts)]
    out["actions"] = record.actions
    out["resume"] = resume.replace("{id}", session.session_id) if resume else ""
    out["reply_urls"] = session.reply_urls
    out["notes"] = [n for n in user.notes if n.get("session") == session.key]
    related = []
    for link in (web.similar(record) if web else [])[:8]:
        other = link.other(record)
        related.append({"key": other.session.key, "title": other.title,
                        "project": other.session.project, "words": link.words,
                        "hidden": link.hidden, "score": round(link.score, 3)})
    out["related"] = related
    return out


def projects(records: list[Record]) -> list[dict]:
    grouped: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        grouped[record.session.project].append(record)
    out = []
    for name, group in grouped.items():
        models: Counter = Counter()
        harnesses: Counter = Counter()
        for record in group:
            harnesses[record.session.harness] += len(record.session.prompts)
            for prompt in record.session.prompts:
                models[prompt.model or UNKNOWN_MODEL] += 1
        starts = [r.session.started for r in group if r.session.started]
        contexts = sorted({r.session.context for r in group if r.session.context})
        out.append({
            "name": name, "contexts": contexts,
            "app": any(is_app(c) for c in contexts),
            "sessions": len(group), "prompts": sum(models.values()),
            "first": min(starts, default=""),
            "last": max((r.session.updated for r in group), default=""),
            "models": dict(models.most_common()), "harnesses": dict(harnesses.most_common()),
        })
    out.sort(key=lambda p: p["last"], reverse=True)
    return out


def handoffs(records: list[Record]) -> list[dict]:
    """Each time the work on a project moved from one model to another."""
    prompts = sorted(((p.ts, p.model, r) for r in records for p in r.session.prompts
                      if p.model and p.ts), key=lambda item: item[0])
    out = []
    current = ""
    for ts, model, record in prompts:
        if current and model != current:
            out.append({"at": ts, "from": current, "to": model, "key": record.session.key,
                        "harness": record.session.harness, "title": record.title})
        current = model
    return out


def timeline(records: list[Record], project: str, user: UserData) -> dict:
    """How a project came together: activity by day and model, sessions, handoffs."""
    group = [r for r in records if r.session.project == project]
    days: dict[str, Counter] = defaultdict(Counter)
    for record in group:
        for prompt in record.session.prompts:
            day = extract.local(prompt.ts, "%Y-%m-%d")
            if day:
                days[day][prompt.model or UNKNOWN_MODEL] += 1
    sessions = sorted((session_summary(r, user) for r in group), key=lambda s: s["started"])
    eras = []
    for change in handoffs(group):
        eras.append(change)
    keywords: Counter = Counter()
    for record in group:
        keywords.update(set(record.themes))
    return {
        "project": project,
        "info": next((p for p in projects(group)), {}),
        "days": [{"day": d, "models": dict(c)} for d, c in sorted(days.items())],
        "sessions": sessions, "handoffs": eras,
        "themes": [t for t, _ in keywords.most_common(20)],
        "notes": [n for n in user.notes if n.get("project") == project],
        "actions": [dict(a, key=r.session.key, title=r.title)
                    for r in group for a in r.actions],
    }


def tools_overview(records: list[Record], labels: dict[str, str]) -> list[dict]:
    """Each tool (harness): how much it has been used, where, and with which models."""
    by_tool: dict[str, dict] = {}
    for record in records:
        session = record.session
        entry = by_tool.setdefault(session.harness, {
            "harness": session.harness, "label": labels.get(session.harness, session.harness),
            "sessions": 0, "prompts": 0, "projects": Counter(), "models": Counter(),
            "first": "", "last": ""})
        entry["sessions"] += 1
        entry["prompts"] += len(session.prompts)
        entry["projects"][session.project] += len(session.prompts)
        entry["models"].update(p.model or UNKNOWN_MODEL for p in session.prompts)
        if session.started:
            entry["first"] = min(entry["first"] or session.started, session.started)
        entry["last"] = max(entry["last"], session.updated)
    out = sorted(by_tool.values(), key=lambda t: -t["prompts"])
    for entry in out:
        entry["projects"] = dict(entry["projects"].most_common())
        entry["models"] = dict(entry["models"].most_common())
    return out


def models_overview(records: list[Record]) -> dict:
    """Which models touched which projects, and which models' work overlaps."""
    by_model: dict[str, dict] = {}
    model_projects: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        for prompt in record.session.prompts:
            model = prompt.model or UNKNOWN_MODEL
            entry = by_model.setdefault(model, {
                "model": model, "family": model_family(prompt.model), "prompts": 0,
                "first": "", "last": "", "harnesses": Counter()})
            entry["prompts"] += 1
            entry["harnesses"][record.session.harness] += 1
            if prompt.ts:
                entry["first"] = min(entry["first"] or prompt.ts, prompt.ts)
                entry["last"] = max(entry["last"], prompt.ts)
            model_projects[model][record.session.project] += 1
    models = sorted(by_model.values(), key=lambda m: -m["prompts"])
    for entry in models:
        entry["harnesses"] = dict(entry["harnesses"].most_common())
        entry["projects"] = dict(model_projects[entry["model"]].most_common())

    # Pairs of models that both worked on the same projects.
    shared = []
    names = [m["model"] for m in models if m["model"] != UNKNOWN_MODEL]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = sorted(set(model_projects[a]) & set(model_projects[b]))
            if both:
                shared.append({"a": a, "b": b, "projects": both})
    shared.sort(key=lambda s: -len(s["projects"]))

    grouped: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        grouped[record.session.project].append(record)
    per_project = []
    for project, group in grouped.items():
        changes = handoffs(group)
        used = Counter(p.model for r in group for p in r.session.prompts if p.model)
        if not used:
            continue
        per_project.append({
            "project": project, "models": dict(used.most_common()),
            "handoffs": len(changes),
            "pairs": dict(Counter(f"{c['from']} → {c['to']}" for c in changes).most_common(5)),
        })
    per_project.sort(key=lambda p: (-len(p["models"]), -p["handoffs"]))
    return {"models": models, "shared": shared, "projects": per_project}


# graph


def graph(records: list[Record], user: UserData, web: connect.Web,
          sessions: bool = False) -> dict:
    """Nodes and edges for the connections view.

    Projects are the hubs. Models, harnesses, keywords, themes, tags and notes
    join the projects they touch; with `sessions`, each session is drawn too.
    Edges between projects are the links worked out from shared unusual words,
    the ones no theme or tag explains marked hidden.
    """
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str], dict] = {}

    def node(kind: str, name: str, label: str = "", weight: float = 1, **extra) -> str:
        nid = f"{kind}:{name}"
        if nid in nodes:
            nodes[nid]["weight"] += weight
        else:
            nodes[nid] = {"id": nid, "kind": kind, "label": label or name, "weight": weight,
                          **extra}
        return nid

    def edge(a: str, b: str, kind: str, weight: float = 1, **extra) -> None:
        pair = (a, b) if a < b else (b, a)
        if pair in edges:
            edges[pair]["weight"] += weight
            return
        edges[pair] = {"source": pair[0], "target": pair[1], "kind": kind, "weight": weight,
                       **extra}

    for record in records:
        session = record.session
        size = max(1, len(session.prompts))
        project = node("project", session.project, weight=size,
                       app=is_app(session.context))
        anchor = project
        if sessions:
            anchor = node("session", session.key, record.title, size,
                          project=session.project)
            edge(anchor, project, "in")
        edge(anchor, node("harness", session.harness, weight=size), "harness", size)
        for model, count in Counter(p.model for p in session.prompts if p.model).items():
            edge(anchor, node("model", model, weight=count), "model", count)
        for theme in record.themes:
            edge(anchor, node("theme", theme), "theme")
        for tag in user.session_tags.get(session.key, []):
            edge(anchor, node("tag", tag), "tag")

    for note in user.notes:
        nid = node("note", note["id"], note["title"])
        if note.get("project") and f"project:{note['project']}" in nodes:
            edge(nid, f"project:{note['project']}", "note")
        if note.get("session") and f"session:{note['session']}" in nodes:
            edge(nid, f"session:{note['session']}", "note")
        for tag in note.get("tags", []):
            edge(nid, node("tag", tag), "tag")

    for term, holders in web.keywords(limit=30):
        kid = node("keyword", term, weight=len(holders))
        for record in holders:
            target = (f"session:{record.session.key}" if sessions
                      else f"project:{record.session.project}")
            edge(kid, target, "keyword")

    for link in web.projects()[:40]:
        hidden = all(l.hidden for l in link.links)
        edge(f"project:{link.a}", f"project:{link.b}", "link", round(link.score, 3),
             words=link.words, hidden=hidden)

    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def connections(store, web: connect.Web, limit: int = 12) -> dict:
    """Links between projects and the prompts to try that come out of them."""
    pairs = []
    for link in web.projects()[:30]:
        best = link.links[0]
        pairs.append({
            "a": link.a, "b": link.b, "score": round(link.score, 3), "words": link.words,
            "hidden": all(l.hidden for l in link.links),
            "sessions": [{"key": l.a.session.key, "title": l.a.title, "other": l.b.session.key,
                          "other_title": l.b.title, "words": l.words} for l in link.links[:4]],
            "snippets": [s for s in (connect.snippet(best.a, w) for w in best.words[:2]) if s],
        })
    ideas = [{"title": i.title, "prompt": i.prompt, "where": i.where, "words": i.words,
              "sessions": [{"key": r.session.key, "title": r.title} for r in i.sessions]}
             for i in connect.ideas(store, web, limit)]
    insights = store.insights.get("data") or {}
    return {"pairs": pairs, "ideas": ideas, "themes": insights.get("themes", []),
            "insights_at": store.insights.get("at", "")}
