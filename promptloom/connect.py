"""What sessions have in common beyond their themes: the unusual words they share.

Every session becomes a weighted set of words and two-word phrases (TF-IDF over
its prompts, title and summary). Sessions in different projects that share
unusual words are linked, words that recur across projects become keywords, and
the strongest links between projects become prompts that suggest connecting them.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import extract
from .store import Record

MAX_PROMPT_CHARS = 2500  # pasted logs follow what was typed; the typed part says more
TERMS_PER_SESSION = 60
MAX_SESSIONS = 400
MIN_SCORE = 0.1  # weaker overlaps are mostly coincidence
# What a project directory has and a home or downloads folder does not.
PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "README.md")

STOPWORDS = frozenset("""
a ability able about above across act actual actually add added adding after again against ago
agent all allow allows almost along already also although always am among amount an and another
any anyone anything anyway anywhere app application applications apply approach apps are area
around as ask asked asking assist at available away back based basic basically be because become
been before begin behind being below beside best better between beyond big bit both bring broken
build built but button by call called can cannot case cause certain chance change changed
changes check checked choose clear clearly close code come complete completely computer
configuration configured consider contain content continue correct could couldn course create
created creating current currently data day days deal decide default definitely describe detail
details develop developed development device did didn difference different directly do does
doesn doing don done down during each easier easy either else enable end enough ensure entire
environment error errors especially etc even ever every everything exact exactly example except
existing expect explain extra fact far feature features feel feels few field fields file files
find fine finish first fix fixed folder following for form found free from full fully function
functions further general generate generated get gets getting give given gives go goal going
gone good got great guess had hand handle happen happens hard has have haven having he heavy
hello help her here hers hey high him his hitting hold home hope how huge idea ideas if
implement implemented important in include included including info information input inside
install installed instead interesting into is isn issue issues it item items its itself just
keep kick kind know large last later latest least left less let level like likely line lines
list little load loaded local long longer look looks lose lot love low machine main mainly make
makes making manage manual many may maybe me mean means meant mention method might mind minute
minutes missing mode model models module moment month months more most mostly move moving much
multi multiple must my name named names near nearly need needed needs never new next nice no
none nor normal not note notes nothing now number of off offer often okay old on once one ones
online only open option options or order original other otherwise our out output outside over
overall own page part particular pass past people per perfect perhaps person pick place plan
play please point possible potential prefer present pretty previous probably process program
project projects properly provide provided put quick quickly quite ran random rather ready real
really reason recent related remaining remove replace request required resolve resolved
resolving result results return review right run running said same save say says screen second
section see seem seems seen select send sense separate service services set setting settings
setup several shall she short should show side similar simple simply since single site small
smart so some someone something sometimes soon sort source special specific specifically start
started state status stay step steps still stop structure stuff style such suggest support
supposed sure system take taken talk task tasks tell terminal test tested tests text than thank
thanks that the their them then title there these they thing things think this those though
thought three through throughout time to today together too tool tools top total toward true try
trying turn two type types under unless until up update updated upon us use used useful user
users uses using usually value values variable various version very via view wait want wanted
was way ways we well went were what whatever when where whether which while who whole why wide
will window windows with within without won wonder word words work working works world worse
would write wrong year years yes yet you your yours yourself bin dev etc false http https lib
none null opt share tmp true usr var www com org html json txt png jpg md py js ts sh
""".split()) | {Path.home().name.lower()}

_WORD = re.compile(r"[a-z][a-z0-9]*(?:[.+#-][a-z0-9]+)*")
_CONTRACTION = re.compile(r"[a-z]+['’][a-z]+")
_HEXISH = re.compile(r"[0-9a-f]{6,}")


def is_app(context: str) -> bool:
    """Whether a session ran inside a project of its own, not a folder that holds many."""
    if not context.startswith("/"):
        return False
    path = Path(context).expanduser()
    return path != Path.home() and any((path / marker).exists() for marker in PROJECT_MARKERS)


def _keep(word: str) -> bool:
    if not 3 <= len(word) <= 24 or _HEXISH.fullmatch(word):
        return False
    if word in STOPWORDS or (word.endswith("s") and word[:-1] in STOPWORDS):
        return False
    return sum(c.isdigit() for c in word) <= len(word) * 0.3


def terms(text: str) -> set[str]:
    """The words and adjacent word pairs worth comparing in one piece of text."""
    text = _CONTRACTION.sub(" ", extract.URL_RE.sub(" ", text.lower()))
    run: list[str | None] = []  # None marks a break: punctuation or a dropped word
    end = 0
    for match in _WORD.finditer(text):
        if text[end:match.start()].strip():
            run.append(None)
        end = match.end()
        word = match.group().strip(".+#-")
        run.append(word if _keep(word) else None)
    found = {w for w in run if w}
    found.update(part for w in list(found) if "-" in w for part in w.split("-") if _keep(part))
    found.update(f"{x} {y}" for x, y in zip(run, run[1:]) if x and y and x != y)
    return found


def session_terms(record: Record) -> Counter:
    """How many prompts use each term, so a pasted log repeating a word counts once."""
    counts: Counter = Counter()
    for prompt in record.session.prompts:
        counts.update(terms(prompt.text[:MAX_PROMPT_CHARS]))
    for piece in [record.title, record.summary, *record.themes]:
        for term in terms(piece):
            counts[term] += 2
    return counts


@dataclass
class Link:
    a: Record
    b: Record
    score: float
    words: list[str]  # the shared terms that weigh most, strongest first
    themes: list[str] = field(default_factory=list)  # themes they share

    @property
    def hidden(self) -> bool:
        """No theme connects these two; only their words do."""
        return not self.themes

    def other(self, record: Record) -> Record:
        return self.b if record is self.a else self.a


@dataclass
class Idea:
    """A prompt to try, from the model's insights or from a link between projects."""

    title: str
    prompt: str
    sessions: list[Record]
    where: str = ""  # directory to run it in, when known
    words: list[str] = field(default_factory=list)


@dataclass
class ProjectLink:
    a: str
    b: str
    score: float
    words: list[str]
    links: list[Link]


class Web:
    """Pairwise links between sessions, built once per render."""

    def __init__(self, records: list[Record], cache: dict | None = None):
        self.records = records[:MAX_SESSIONS]
        cache = {} if cache is None else cache
        counts = {}
        for record in self.records:
            stamp = (len(record.session.prompts), record.title, record.summary,
                     tuple(record.themes))
            cached = cache.get(record.session.key)
            if not cached or cached[0] != stamp:
                cached = cache[record.session.key] = (stamp, session_terms(record))
            counts[record.session.key] = cached[1]
        self.vectors = self._weigh(counts)
        self.by_key = {r.session.key: r for r in self.records}
        self.links = self._link()
        self._by_session: dict[str, list[Link]] = defaultdict(list)
        for link in self.links:
            self._by_session[link.a.session.key].append(link)
            self._by_session[link.b.session.key].append(link)

    def _weigh(self, counts: dict[str, Counter]) -> dict[str, dict[str, float]]:
        vocabulary = {t for c in counts.values() for t in c}
        # Fold plurals into a singular the sessions also use: "videos" -> "video".
        fold = {t: t[:-1] for t in vocabulary
                if t.endswith("s") and not t.endswith("ss") and t[:-1] in vocabulary}
        folded = {}
        for key, c in counts.items():
            merged: Counter = Counter()
            for term, n in c.items():
                merged[fold.get(term, term)] += n
            folded[key] = merged
        total = len(folded)
        df = Counter(t for c in folded.values() for t in c)
        common = max(2, int(total * 0.4))  # a word half the sessions use links nothing
        vectors = {}
        for key, c in folded.items():
            weights = {t: (1 + math.log(n)) * math.log((total + 1) / df[t])
                       for t, n in c.items() if 2 <= df[t] <= common}
            # Ties break by name: sets order strings differently in every process,
            # and a map that changes on each restart would be rewritten each time.
            ranked = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
            top = ranked[:TERMS_PER_SESSION]
            norm = math.sqrt(sum(w * w for _, w in top)) or 1.0
            vectors[key] = {t: w / norm for t, w in top}
        return vectors

    def _link(self) -> list[Link]:
        postings: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for key, vector in self.vectors.items():
            for term, weight in vector.items():
                postings[term].append((key, weight))
        shared: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
        for term, posting in postings.items():
            for i, (ka, wa) in enumerate(posting):
                for kb, wb in posting[i + 1:]:
                    shared[(ka, kb) if ka < kb else (kb, ka)].append((wa * wb, term))
        links = []
        for (ka, kb), parts in shared.items():
            score = sum(w for w, _ in parts)
            if score < MIN_SCORE:
                continue
            a, b = self.by_key[ka], self.by_key[kb]
            themes = sorted(set(a.themes) & set(b.themes))
            links.append(Link(a, b, score, _strongest(parts), themes))
        links.sort(key=lambda link: (-link.score, link.a.session.key, link.b.session.key))
        return links

    def similar(self, record: Record) -> list[Link]:
        return self._by_session.get(record.session.key, [])

    def across(self) -> list[Link]:
        """Links between sessions in different projects, strongest first."""
        return [link for link in self.links
                if link.a.session.project != link.b.session.project]

    def projects(self) -> list[ProjectLink]:
        """Pairs of projects, scored by their three strongest session links."""
        grouped: dict[tuple[str, str], list[Link]] = defaultdict(list)
        for link in self.across():
            pair = tuple(sorted((link.a.session.project, link.b.session.project)))
            grouped[pair].append(link)
        out = []
        for (a, b), links in grouped.items():
            best = links[:3]
            words: list[str] = []
            for link in best:
                words += [w for w in link.words if w not in words]
            out.append(ProjectLink(a, b, sum(l.score for l in best), words[:6], links))
        out.sort(key=lambda p: (-p.score, p.a, p.b))
        return out

    def keywords(self, limit: int = 40) -> list[tuple[str, list[Record]]]:
        """Terms that turn up in more than one project, most connecting first."""
        holders: dict[str, list[Record]] = defaultdict(list)
        weight: Counter = Counter()
        for key, vector in self.vectors.items():
            for term, w in vector.items():
                holders[term].append(self.by_key[key])
                weight[term] += w
        ranked = []
        for term, records in holders.items():
            spread = len({r.session.project for r in records})
            if spread >= 2:
                ranked.append((weight[term] * spread, term, records))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        chosen: list[tuple[str, list[Record]]] = []
        for _, term, records in ranked:
            # "deepseek harness" says more than "deepseek" and "harness" on their own
            # pages, but a phrase whose words are both already chosen adds nothing.
            if " " in term and all(part in dict(chosen) for part in term.split()):
                continue
            chosen.append((term, records))
            if len(chosen) == limit:
                break
        return chosen


def _strongest(parts: list[tuple[float, str]], limit: int = 4) -> list[str]:
    def pieces(term: str) -> set[str]:
        return set(re.split(r"[ -]", term))

    words: list[str] = []
    for _, term in sorted(parts, key=lambda part: (-part[0], part[1])):
        if any(pieces(term) < pieces(w) for w in words):
            continue  # "gpu" next to "gpu monitor" is noise
        covered = [w for w in words if pieces(w) < pieces(term)]
        if covered:  # "gpu monitor" says more than "gpu" and "monitor" apart
            at = words.index(covered[0])
            words = [w for w in words if w not in covered]
            words.insert(at, term)
        elif len(words) < limit:
            words.append(term)
    return words


def snippet(record: Record, term: str, width: int = 70) -> str:
    """A short excerpt of a prompt where the term is used, for context."""
    pattern = re.compile(
        r"\b" + r"[\W_]{1,3}".join(re.escape(part) + "s?" for part in term.split()) + r"\b",
        re.I)
    for text in [p.text[:MAX_PROMPT_CHARS] for p in record.session.prompts] + [record.summary]:
        match = pattern.search(text)
        if match:
            start, end = max(0, match.start() - width), match.end() + width
            excerpt = " ".join(extract.redact(text[start:end]).split())
            return ("…" if start else "") + excerpt + ("…" if end < len(text) else "")
    return ""


def bridge_prompt(link: Link) -> tuple[Record, str]:
    """A prompt that asks how to connect the two, to run in one of their projects.

    That is the project with a directory of its own if only one has, else the
    more recent. Returns the session whose project it is meant for, and the prompt.
    """
    here, there = sorted((link.a, link.b), reverse=True, key=lambda r: (
        is_app(r.session.context), r.session.updated))

    def described(record: Record) -> str:
        where = record.session.context if record.session.context.startswith("/") else ""
        text = f'"{record.title}"' + (f" ({where})" if where else "")
        return text + (f": {record.summary.rstrip('.')}" if record.summary else "")

    prompt = (
        f"In {there.session.project} I worked on {described(there)}. "
        f"Here in {here.session.project} I worked on {described(here)}. "
        f"Both involve {', '.join(link.words)}. "
        f"Look at both and suggest concrete ways to connect them: code or a tool one could "
        f"reuse from the other, something they could share, or a feature that only makes "
        f"sense with both. Start with the idea with the best payoff for the least work.")
    return here, prompt


def ideas(store, web: Web, limit: int = 8) -> list[Idea]:
    """Prompts to try: the model's, then one per pair of linked projects."""
    out = []
    insights = store.insights
    by_ref = insights.get("refs", {})
    for item in (insights.get("data") or {}).get("opportunities", []):
        prompt = item.get("prompt") if isinstance(item, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            continue
        sessions = []
        refs = item.get("sessions")
        for ref in refs if isinstance(refs, list) else []:
            record = store.records.get(by_ref.get(str(ref).strip("[] "), ""))
            if record and record.session.prompts:
                sessions.append(record)
        out.append(Idea(str(item.get("title", "")), prompt.strip(), sessions))
    out = out[:limit // 2]
    seen = set()

    def promise(link: Link) -> tuple[int, float]:
        # Two apps of your own come before a session run from a home or
        # downloads folder, and a link no theme explains is the one least
        # likely to have been noticed already.
        apps = is_app(link.a.session.context) + is_app(link.b.session.context)
        return apps, link.score * (1.5 if link.hidden else 1)

    for link in sorted((l for l in web.across() if len(l.words) >= 2), key=promise,
                       reverse=True):
        if len(out) >= limit:
            break
        pair = frozenset((link.a.session.project, link.b.session.project))
        if pair in seen:
            continue
        seen.add(pair)
        here, prompt = bridge_prompt(link)
        there = link.other(here)
        where = here.session.context if here.session.context.startswith("/") else ""
        out.append(Idea(f"Connect {here.session.project} with {there.session.project}", prompt,
                        [here, there], where, link.words))
    return out
