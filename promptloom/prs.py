"""Pull requests: yours and other people's, per project, with reviews and fixes
by the coding agent you pick.

When switched on, the daemon asks GitHub (through the gh CLI) every few minutes
for your open pull requests, the ones others opened in your repositories and
the ones waiting for your review. When someone comments on one of yours, a
notification offers to fix it. A review runs an agent on its own and keeps what
it wrote; a fix opens a terminal so you can watch the agent work, in a worktree
of its own unless you say otherwise.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import extract, gitlocal
from .desktop import (DesktopError, child_env, open_terminal, open_uri, search_path,  # noqa: F401
                      spawn, terminal, which)
from .store import file_lock, write_atomic

STATE_NAME = "prs.json"
MAX_RUNS = 50
DIFF_CHARS = 60_000  # an argument may not exceed 128 KiB on Linux
BODY_CHARS = 4000
WRITE = {"ADMIN", "MAINTAIN", "WRITE"}  # permissions under which others' PRs are yours to see
PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
ICON = Path(__file__).parent / "ui" / "icon.svg"

# Reviews may read the checkout and ask git and gh about the pull request; nothing else.
READ_ONLY = ("Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(git diff:*),"
             "Bash(gh pr view:*),Bash(gh pr diff:*)")

# {prompt} becomes the prompt (otherwise it goes to stdin) and {output} a file the
# answer is read from (otherwise stdout). `fix` runs in a terminal, so it must
# take the prompt as {prompt}.
AGENTS = {
    "claude": {"label": "Claude Code",
               "review": ["claude", "-p", "--allowedTools", READ_ONLY],
               "fix": ["claude", "{prompt}"]},
    "codex": {"label": "Codex",
              "review": ["codex", "exec", "--sandbox", "read-only",
                         "--output-last-message", "{output}", "-"],
              "fix": ["codex", "{prompt}"]},
    "copilot": {"label": "Copilot CLI",
                "review": ["copilot", "-p", "{prompt}"],
                "fix": ["copilot", "-i", "{prompt}"]},
}

QUERY = """
query($mine: String!, $theirs: String!, $asked: String!) {
  mine: search(query: $mine, type: ISSUE, first: 50) { nodes { ...Full } }
  theirs: search(query: $theirs, type: ISSUE, first: 50) { nodes { ...Brief } }
  asked: search(query: $asked, type: ISSUE, first: 50) { nodes { ...Brief } }
  %s
}
fragment Brief on PullRequest {
  number title url isDraft createdAt updatedAt headRefName baseRefName
  additions deletions changedFiles reviewDecision
  repository { nameWithOwner } author { login }
  commentCount: comments { totalCount }
  commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
}
fragment Full on PullRequest {
  ...Brief
  comments(last: 40) { nodes { author { __typename login } body createdAt url } }
  reviews(last: 40) { nodes { author { __typename login } state body submittedAt url } }
  reviewThreads(last: 60) { nodes { isResolved isOutdated path line
    comments(first: 15) { nodes { author { __typename login } body createdAt url } } } }
}
"""
REPO_QUERY = """r%d: repository(owner: %s, name: %s) { viewerPermission
    pullRequests(states: OPEN, first: 30, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { ...Brief } } }"""

REVIEW_PROMPT = """\
Review pull request {repo}#{number}, "{title}", by {author}: {url}
It merges {head} into {base}: +{additions} -{deletions} in {files} files.

Look for:
- Correctness: bugs, unhandled cases, error handling, security.
- Quality: clarity, naming, structure, duplication, and how well it fits the code around it.
- Conciseness: anything that could be shorter or go: dead code, needless abstraction, \
comments that repeat the code, a diff bigger than the change needs.
- Tests: whether what matters is tested.
- The description: whether it says what changed and why.

Name the file and line for each point, say why it matters and what to do instead. Put \
what most needs changing first and minor points last. If it is ready to merge, say so \
plainly. This is a review only: do not change any files or post anything to GitHub.

{where}"""

FIX_PROMPT = """\
Pull request {repo}#{number}, "{title}" ({url}), needs work.

{feedback}

{where}
Make the changes the feedback asks for where you agree with it, and tell me where you \
don't and why. Run the tests, then commit and push to the pull request's branch. Finish \
with a short summary of what you did about each point, and ask me before replying on \
the pull request."""


class PRError(Exception):
    pass


# settings and agents

def settings(cfg: dict) -> dict:
    return cfg.get("prs", {})


def agents(cfg: dict) -> dict[str, dict]:
    custom = settings(cfg).get("agents", {})
    out = {}
    for name in [*AGENTS, *(n for n in custom if n not in AGENTS)]:
        spec = {**AGENTS.get(name, {"label": name}), **custom.get(name, {})}
        spec["available"] = bool(spec.get("review") and which(spec["review"][0]))
        out[name] = spec
    return out


def agent(cfg: dict, name: str = "") -> tuple[str, dict]:
    known = agents(cfg)
    name = name or settings(cfg).get("agent", "claude")
    if name not in known:
        raise PRError(f"unknown agent {name!r}: choose from {', '.join(known)}")
    if not known[name]["available"]:
        raise PRError(f"{known[name]['label']} is not installed "
                      f"(no {known[name]['review'][0]!r} on PATH)")
    return name, known[name]


# GitHub

def gh(args: list[str], stdin: str | None = None, timeout: int = 60, check: bool = True) -> str:
    if not which("gh"):
        raise PRError("the GitHub CLI (gh) is not installed: https://cli.github.com, "
                      "then run `gh auth login`")
    try:
        done = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True,
                              timeout=timeout, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PRError(f"gh: {exc}") from exc
    if done.returncode and (check or not done.stdout.strip()):
        message = " ".join((done.stderr or done.stdout).split())
        raise PRError("gh: " + (message[:300] or "failed"))
    return done.stdout


def fetch(login: str, repos: list[str]) -> dict:
    """One GraphQL request: your PRs, others' PRs in your repos, and those asking you."""
    aliases = []
    for i, repo in enumerate(repos):
        owner, name = repo.split("/", 1)
        aliases.append(REPO_QUERY % (i, json.dumps(owner), json.dumps(name)))
    base = "is:pr is:open archived:false "
    args = ["api", "graphql", "-f", "query=" + QUERY % "\n  ".join(aliases),
            "-f", f"mine={base}author:{login}",
            "-f", f"theirs={base}user:{login} -author:{login}",
            "-f", f"asked={base}review-requested:{login}"]
    # One repository that cannot be read fails the request but not the others'
    # answers, so take whatever data came back.
    data = json.loads(gh(args, check=False))
    if data.get("errors") and not data.get("data"):
        raise PRError("GitHub: " + "; ".join(e.get("message", "") for e in data["errors"]))
    return data.get("data") or {}


def _who(node: dict | None) -> tuple[str, bool]:
    node = node or {}
    return node.get("login") or "ghost", node.get("__typename") == "Bot"


def feedback(node: dict, login: str) -> list[dict]:
    """What others said on a pull request that is still open to act on.

    Unresolved review threads, reviews with something to say, and comments;
    comments from bots (deploy previews, coverage) are left out, their reviews
    not. Each item carries when you last answered where it was said: in its
    thread for a line comment, in the conversation for the rest.
    """
    def mine(c: dict) -> bool:
        return _who(c.get("author"))[0].lower() == login.lower()

    comments = (node.get("comments") or {}).get("nodes", [])
    reviews = (node.get("reviews") or {}).get("nodes", [])
    talked = max([c.get("createdAt", "") for c in comments if mine(c)] +
                 [r.get("submittedAt") or "" for r in reviews if mine(r)], default="")
    items: list[dict] = []

    def add(kind: str, c: dict, at: str, replied: str, **extra) -> None:
        who, bot = _who(c.get("author"))
        if who.lower() != login.lower() and (kind != "comment" or not bot):
            items.append({"kind": kind, "author": who, "body": (c.get("body") or "")[:1500],
                          "at": at, "url": c.get("url", ""), "replied": replied, **extra})

    for c in comments:
        add("comment", c, c.get("createdAt", ""), talked)
    for r in reviews:
        if (r.get("body") or "").strip() or r.get("state") == "CHANGES_REQUESTED":
            add("review", r, r.get("submittedAt") or "", talked, state=r.get("state", ""))
    for thread in (node.get("reviewThreads") or {}).get("nodes", []):
        if thread.get("isResolved"):
            continue
        said = (thread.get("comments") or {}).get("nodes", [])
        replied = max((c.get("createdAt", "") for c in said if mine(c)), default="")
        for c in said:
            add("line", c, c.get("createdAt", ""), replied, path=thread.get("path") or "",
                line=thread.get("line"), outdated=bool(thread.get("isOutdated")))
    items.sort(key=lambda i: i["at"])
    return items


def mentions(records) -> dict[str, list[dict]]:
    """Pull request URL -> the sessions whose prompts or replies mention it."""
    found: dict[str, list[dict]] = {}
    for record in records:
        session = record.session
        texts = [p.text for p in session.prompts] + session.reply_urls + [session.last_reply]
        for text in texts:
            for owner, name, number in PR_URL.findall(text or ""):
                key = pr_key(owner, name, number)
                if all(s["key"] != session.key for s in found.get(key, [])):
                    found.setdefault(key, []).append({"key": session.key, "title": record.title})
    return found


def pr_key(owner: str, name: str, number) -> str:
    return f"https://github.com/{owner}/{name}/pull/{number}".lower()


def parse(data: dict, login: str, places: dict[str, Path],
          seen_in: dict[str, list[dict]]) -> list[dict]:
    nodes = [(n, "mine") for n in (data.get("mine") or {}).get("nodes", [])]
    nodes += [(n, "theirs") for n in (data.get("theirs") or {}).get("nodes", [])]
    nodes += [(n, "asked") for n in (data.get("asked") or {}).get("nodes", [])]
    for alias, repo in data.items():
        if alias.startswith("r") and alias[1:].isdigit() and repo:
            if repo.get("viewerPermission") in WRITE:
                nodes += [(n, "theirs") for n in repo["pullRequests"]["nodes"]]
    prs: dict[str, dict] = {}
    for node, kind in nodes:
        if not node or not node.get("url"):
            continue  # search can return issues, which come back empty
        pr = prs.get(node["url"])
        if pr is None:
            repo = node["repository"]["nameWithOwner"]
            commit = ((node.get("commits") or {}).get("nodes") or [{}])[-1].get("commit") or {}
            checkout = places.get(repo.lower())
            author = _who(node.get("author"))[0]
            pr = prs[node["url"]] = {
                "url": node["url"], "repo": repo, "number": node["number"],
                "title": node["title"], "author": author,
                "mine": author.lower() == login.lower(), "asked": False,
                "draft": node["isDraft"], "created": node["createdAt"],
                "updated": node["updatedAt"], "head": node["headRefName"],
                "base": node["baseRefName"], "additions": node.get("additions", 0),
                "deletions": node.get("deletions", 0), "files": node.get("changedFiles", 0),
                "decision": node.get("reviewDecision") or "",
                "checks": ((commit.get("statusCheckRollup") or {}).get("state") or ""),
                "last_commit": commit.get("committedDate") or "",
                "comments": (node.get("commentCount") or {}).get("totalCount", 0),
                "checkout": str(checkout) if checkout else "",
                "project": checkout.name if checkout else repo.split("/")[1],
                "sessions": seen_in.get(node["url"].lower(), []),
                "feedback": [], "waiting": 0,
            }
        pr["asked"] = pr["asked"] or kind == "asked"
        if "reviewThreads" in node:
            # New: said after your last push and after you last answered there.
            items = feedback(node, login)
            for item in items:
                item["new"] = item["at"] > max(pr["last_commit"], item.pop("replied"))
            pr["feedback"] = items
            pr["waiting"] = sum(1 for i in items if i["new"])
    return sorted(prs.values(), key=lambda p: p["updated"], reverse=True)


# state: prs.json beside state.json

def load(state_dir: Path) -> dict:
    try:
        state = json.loads((state_dir / STATE_NAME).read_text())
    except (OSError, ValueError):
        state = {}
    for key, empty in (("prs", []), ("seen", {}), ("runs", [])):
        state.setdefault(key, empty)
    return state


@contextmanager
def editing(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(state_dir / "prs.lock"):
        state = load(state_dir)
        yield state
        state["runs"] = state["runs"][-MAX_RUNS:]
        write_atomic(state_dir / STATE_NAME, json.dumps(state, ensure_ascii=False, indent=1))


def find(state: dict, url: str) -> dict:
    url = normal_url(url)
    pr = next((p for p in state["prs"] if p["url"].lower() == url.lower()), None)
    if pr is None:
        raise PRError(f"{url} is not among the open pull requests promptloom knows; "
                      "run `promptloom pr check` first")
    return pr


def normal_url(text: str) -> str:
    match = PR_URL.search(text)
    return f"https://github.com/{match[1]}/{match[2]}/pull/{match[3]}" if match else text


# checking GitHub

def places_for(cfg: dict, records) -> tuple[dict[str, Path], list[str]]:
    """Local checkouts by repository, and the repositories you work in."""
    contexts = sorted({r.session.context for r in records if r.session.context.startswith("/")})
    roots = settings(cfg).get("roots") or gitlocal.default_roots(contexts)
    places = gitlocal.checkouts(contexts, roots)
    worked_in = sorted({repo for c in contexts if (top := gitlocal.toplevel(c))
                        and (repo := gitlocal.github_repo(top))})
    return places, worked_in


def local_checkouts(cfg: dict, records) -> list[Path]:
    contexts = sorted({r.session.context for r in records if r.session.context.startswith("/")})
    return gitlocal.candidates(contexts, settings(cfg).get("roots") or
                               gitlocal.default_roots(contexts))


def refresh(cfg: dict, state_dir: Path, records) -> dict:
    """Ask GitHub now, and notify about new feedback on your pull requests."""
    places, worked_in = places_for(cfg, records)
    due: list[dict] = []
    with editing(state_dir) as state:
        state["checked"] = extract.now_ts()
        try:
            login = state.get("login") or gh(["api", "user", "--jq", ".login"]).strip()
            # Others' PRs in repos you own come from search; repos owned by someone
            # else are asked about one by one, and only shown if you can push there.
            repos = [r for r in dict.fromkeys(worked_in + settings(cfg).get("repos", []))
                     if "/" in r and r.split("/")[0].lower() != login.lower()]
            prs = parse(fetch(login, repos), login, places, mentions(records))
        except (PRError, ValueError, KeyError) as exc:
            state["error"] = str(exc)
            return state
        state.update(login=login, prs=prs, error="")
        seen = state["seen"]
        for pr in prs:
            newest = max((i["at"] for i in pr["feedback"] if i["new"]), default="")
            if pr["mine"] and newest > seen.get(pr["url"], ""):
                due.append(pr)
                seen[pr["url"]] = newest
        open_urls = {pr["url"] for pr in prs}
        state["seen"] = {url: at for url, at in seen.items() if url in open_urls}
    if due and settings(cfg).get("notify", True):
        notify_feedback(cfg, due)
    return state


def tick(cfg: dict, state_dir: Path, records) -> bool:
    """Called by the daemon each cycle: check GitHub when it is time. True if it did."""
    if not settings(cfg).get("enabled"):
        return False
    flag = state_dir / "prs.refresh"
    last = extract.parse_ts(load(state_dir).get("checked", ""))
    every = max(1, settings(cfg).get("poll_minutes", 5)) * 60
    if not flag.exists() and last and time.time() - last.timestamp() < every:
        return False
    flag.unlink(missing_ok=True)
    refresh(cfg, state_dir, records)
    return True


def request_refresh(state_dir: Path) -> None:
    """Ask the daemon to check GitHub on its next cycle."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "prs.refresh").touch()


# prompts

def review_place(pr: dict) -> tuple[Path | None, str]:
    """Where a review runs (a worktree on the branch, the checkout, or nowhere),
    and what the prompt tells the agent about it."""
    checkout = Path(pr["checkout"]) if pr["checkout"] else None
    if checkout is None:
        return None, "There is no checkout of the repository here: review it from the diff."
    for tree in gitlocal.worktrees(checkout):
        if tree.branch == pr["head"]:
            return Path(tree.path), ("The working directory is a checkout of the pull "
                                     "request's branch: read it for context.")
    return checkout, ("The working directory is a checkout of the repository, possibly on "
                      "another branch: read it for context; the diff below is what is under "
                      "review.")


def review_prompt(pr: dict, body: str, diff: str, where: str, extra: str = "") -> str:
    text = REVIEW_PROMPT.format_map({**pr, "where": where})
    if body.strip():
        text += "\n\nThe description:\n\n" + body.strip()[:BODY_CHARS]
    if len(diff) > DIFF_CHARS:
        diff = diff[:DIFF_CHARS] + "\n[… the rest of the diff is cut; ask git or gh for it]"
    text += f"\n\nThe diff:\n\n```diff\n{diff}\n```" if diff else ""
    if extra.strip():
        text += "\n\nAlso:\n\n" + extra.strip()
    return text


def describe(item: dict) -> str:
    if item["kind"] == "line":
        where = f"on {item['path']}" + (f":{item['line']}" if item.get("line") else "")
        where += " (outdated)" if item.get("outdated") else ""
    elif item["kind"] == "review":
        where = "in a review" + (" requesting changes" if item.get("state") ==
                                 "CHANGES_REQUESTED" else "")
    else:
        where = "in a comment"
    return f"{item['author']} {where}: {item['body'].strip() or '(no text)'}\n   {item['url']}"


def fix_prompt(pr: dict, review: str = "", worktree: bool = False, extra: str = "") -> str:
    items = [i for i in pr.get("feedback", []) if i.get("new")] or pr.get("feedback", [])
    parts = []
    if items:
        parts.append("Feedback on GitHub:\n\n" + "\n".join(
            f"{n}. {describe(i)}" for n, i in enumerate(items, 1)))
    if review.strip():
        parts.append("A review promptloom ran said:\n\n" + review.strip()[:8000])
    if not parts:
        parts.append("There is no feedback on it yet: review it yourself and fix what needs it.")
    if worktree:
        where = (f"You are in a worktree of its own, on the pull request's branch "
                 f"({pr['head']}).")
    else:
        where = (f"Check out the pull request's branch ({pr['head']}) with `gh pr checkout "
                 f"{pr['number']}`, but if this checkout has uncommitted changes, ask me first.")
    text = FIX_PROMPT.format_map({**pr, "feedback": "\n\n".join(parts), "where": where})
    if extra.strip():
        text += "\n\nAlso:\n\n" + extra.strip()
    return text


# runs: reviews in the background, fixes in a terminal

def _new_run(state: dict, kind: str, pr: dict, name: str, extra: str) -> dict:
    run = {"id": f"{kind[0]}-" + extract.item_id(f"{time.time()} {pr['url']}"),
           "kind": kind, "agent": name, "url": pr["url"], "repo": pr["repo"],
           "number": pr["number"], "title": pr["title"], "extra": extra,
           "started": extract.now_ts(), "finished": "", "status": "running",
           "error": "", "output": "", "dir": ""}
    state["runs"].append(run)
    return run


def _update_run(state_dir: Path, run_id: str, **fields) -> None:
    with editing(state_dir) as state:
        for run in state["runs"]:
            if run["id"] == run_id:
                run.update(fields)


def latest_review(state_dir: Path, url: str) -> str:
    for run in reversed(load(state_dir)["runs"]):
        if run["kind"] == "review" and run["url"] == url and run["status"] == "done":
            try:
                return Path(run["output"]).read_text()
            except OSError:
                return ""
    return ""


def start_review(cfg: dict, state_dir: Path, url: str, name: str = "", extra: str = "") -> dict:
    name, _ = agent(cfg, name)
    with editing(state_dir) as state:
        run = _new_run(state, "review", find(state, url), name, extra)
    spawn([sys.executable, "-m", "promptloom", "pr", "_run", run["id"]])
    return run


def _fill(argv: list[str], prompt: str, output: str) -> tuple[list[str], str | None]:
    """Put the prompt and output file into an agent command; returns stdin to send."""
    filled = [a.replace("{prompt}", prompt).replace("{output}", output) for a in argv]
    return filled, None if any("{prompt}" in a for a in argv) else prompt


def run_review(cfg: dict, state_dir: Path, run_id: str) -> dict:
    """Do a review queued by start_review (in its own process). Returns the run."""
    state = load(state_dir)
    run = next(r for r in state["runs"] if r["id"] == run_id)
    try:
        pr = find(state, run["url"])
        _, spec = agent(cfg, run["agent"])
        details = json.loads(gh(["pr", "view", pr["url"], "--json", "body"]))
        diff = gh(["pr", "diff", pr["url"]], timeout=120)
        cwd, where = review_place(pr)
        prompt = review_prompt(pr, details.get("body") or "", diff, where, run["extra"])
        reviews = state_dir / "reviews"
        reviews.mkdir(parents=True, exist_ok=True)
        output = reviews / f"{run_id}.md"
        argv, stdin = _fill(spec["review"], prompt, str(output))
        with tempfile.TemporaryDirectory() as scratch:
            done = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                                  cwd=cwd or scratch, env=child_env(),
                                  timeout=settings(cfg).get("review_timeout_minutes", 20) * 60)
        text = output.read_text() if "{output}" in " ".join(spec["review"]) else done.stdout
        if done.returncode or not text.strip():
            raise PRError(f"{spec['label']} exited with {done.returncode}: "
                          + (done.stderr or done.stdout).strip()[-500:])
        output.write_text(text)
        _save_to_vault(cfg, pr, run, text)
        _update_run(state_dir, run_id, status="done", finished=extract.now_ts(),
                    output=str(output), dir=str(cwd or ""))
        notify(f"Review of {pr['repo']}#{pr['number']} is ready",
               f"{spec['label']} reviewed “{pr['title']}”.",
               {"read": "Read it", "fix": f"Fix with {spec['label']}"}, url=pr["url"],
               run=run_id)
    except (PRError, gitlocal.GitError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        _update_run(state_dir, run_id, status="failed", finished=extract.now_ts(),
                    error=str(exc))
        notify(f"Review of {run['repo']}#{run['number']} failed", str(exc)[:200])
    return next(r for r in load(state_dir)["runs"] if r["id"] == run_id)


def _save_to_vault(cfg: dict, pr: dict, run: dict, text: str) -> None:
    if not cfg.get("vault"):
        return
    from .vault import frontmatter, safe_name

    day = extract.local(run["started"], "%Y-%m-%d")
    folder = Path(cfg["vault"]) / cfg.get("folder", "Promptloom") / "Reviews"
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_name(f"{day} {pr['repo'].split('/')[1]} {pr['number']} {pr['title']} "
                     f"({run['agent']})", 120)
    front = frontmatter({"pull_request": pr["url"], "agent": run["agent"],
                         "reviewed": run["started"], "tags": ["promptloom/review"]})
    (folder / f"{name}.md").write_text(
        f"{front}# Review: {pr['title']}\n\n[{pr['repo']}#{pr['number']}]({pr['url']})\n\n"
        + text.strip() + "\n")


def fix_script(cfg: dict, pr: dict, spec: dict, prompt_file: Path, worktree: bool) -> str:
    """The shell script a fix runs in its terminal: get a checkout, then the agent."""
    q = shlex.quote
    me = f"{q(sys.executable)} -m promptloom"
    lines = ["#!/bin/sh", "# Written by promptloom for one fix; safe to delete.",
             f"export PATH={q(search_path())}"]
    if pr["checkout"]:
        lines.append(f"cd {q(pr['checkout'])} || exit 1")
        if worktree:
            lines.append(f'dir=$({me} pr worktree {q(pr["url"])}) && cd "$dir" || '
                         'echo "promptloom: no worktree; working in the checkout itself"')
    else:
        roots = settings(cfg).get("roots") or []
        home = Path(roots[0]).expanduser() if roots else Path.home()
        dest = home / pr["repo"].split("/")[1]
        if dest.exists():
            dest = home / pr["repo"].replace("/", "-")
        lines += [f"gh repo clone {q(pr['repo'])} {q(str(dest))} || exit 1",
                  f"cd {q(str(dest))} && gh pr checkout {pr['number']}"]
    prompt = '"$(cat ' + q(str(prompt_file)) + ')"'
    command = " ".join(prompt.join(q(part) if part else "" for part in a.split("{prompt}"))
                       for a in spec["fix"])
    lines += [f"echo 'promptloom: fixing {pr['repo']}#{pr['number']} with {spec['label']} "
              "in' \"$(pwd)\"", command,
              "echo; echo 'promptloom: the agent has finished; this terminal stays open.'",
              'exec "${SHELL:-/bin/sh}"']
    return "\n".join(lines) + "\n"


def start_fix(cfg: dict, state_dir: Path, url: str, name: str = "", extra: str = "",
              worktree: bool | None = None) -> dict:
    """Open a terminal where the agent works on the pull request's feedback."""
    name, spec = agent(cfg, name)
    if not any("{prompt}" in a for a in spec["fix"]):
        raise PRError(f"the fix command for {name} must contain {{prompt}}")
    if worktree is None:
        worktree = settings(cfg).get("worktrees", True)
    opener = terminal(cfg)
    review = latest_review(state_dir, normal_url(url))
    with editing(state_dir) as state:
        pr = find(state, url)
        run = _new_run(state, "fix", pr, name, extra)
        worktree = bool(worktree and pr["checkout"])
        prompt = fix_prompt(pr, review, worktree, extra)
        folder = state_dir / "runs"
        folder.mkdir(parents=True, exist_ok=True)
        prompt_file = folder / f"{run['id']}.prompt"
        prompt_file.write_text(prompt)
        script = folder / f"{run['id']}.sh"
        script.write_text(fix_script(cfg, pr, spec, prompt_file, worktree))
        script.chmod(0o700)
        run.update(status="opened", dir=pr["checkout"], output=str(prompt_file))
        spawn([*opener, str(script)], cwd=pr["checkout"] or None)
    return run


def worktree_for(state_dir: Path, url: str) -> Path:
    pr = find(load(state_dir), url)
    if not pr["checkout"]:
        raise PRError(f"no local checkout of {pr['repo']}")
    return gitlocal.add_for_pr(Path(pr["checkout"]), pr["number"], pr["head"])


def notify(title: str, body: str, actions: dict[str, str] | None = None, url: str = "",
           run: str = "") -> None:
    """A desktop notification; its buttons are handled by a small waiting process."""
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
        spawn(["osascript", "-e", script])
    elif sys.platform == "win32":
        from .desktop import windows_toast

        windows_toast(title, body)
    elif which("notify-send"):
        if actions:
            argv = [sys.executable, "-m", "promptloom", "pr", "_notify", "--title", title,
                    "--body", body, "--url", url, "--run", run]
            for key, label in actions.items():
                argv += ["--action", f"{key}={label}"]
            spawn(argv)
        else:
            spawn(["notify-send", "-a", "Promptloom", "-i", str(ICON), title, body])


def notify_feedback(cfg: dict, due: list[dict]) -> None:
    label = agents(cfg).get(settings(cfg).get("agent", "claude"), {}).get("label", "the agent")
    if len(due) > 3:
        notify(f"{len(due)} of your pull requests have new feedback",
               ", ".join(f"{p['repo']}#{p['number']}" for p in due),
               {"app": "Show them"})
        return
    for pr in due:
        new = [i for i in pr["feedback"] if i["new"]]
        who = sorted({i["author"] for i in new})
        notify(f"{', '.join(who)} on {pr['repo']}#{pr['number']}",
               f"{len(new)} new on “{pr['title']}”: {new[-1]['body'][:140]}",
               {"fix": f"Fix with {label}", "open": "Open on GitHub"}, url=pr["url"])


def wait_for_action(cfg: dict, state_dir: Path, title: str, body: str,
                    actions: list[str], url: str = "", run: str = "") -> str:
    """Show a notification with buttons, wait for a click and act on it."""
    argv = ["notify-send", "-a", "Promptloom", "-i", str(ICON)]
    for action in actions:
        argv += ["-A", action]
    try:
        chosen = subprocess.run(argv + [title, body], capture_output=True, text=True,
                                env=child_env()).stdout.strip()
    except OSError:
        return ""
    ui = f"http://127.0.0.1:{cfg.get('ui', {}).get('port', 8765)}/"
    if chosen == "fix" and url:
        try:
            start_fix(cfg, state_dir, url)
        except (PRError, DesktopError) as exc:
            notify("Could not start the fix", str(exc))
    elif chosen == "open" and url:
        open_uri(url)
    elif chosen == "read" and run:
        open_uri(f"{ui}#/prs?run={run}")
    elif chosen == "app":
        open_uri(f"{ui}#/prs")
    return chosen


def summary(state_dir: Path, cfg: dict) -> dict:
    """What the menu bar needs: on or off, and how many want your attention."""
    if not settings(cfg).get("enabled"):
        return {"enabled": False}
    prs = load(state_dir)["prs"]
    return {"enabled": True,
            "waiting": sum(1 for p in prs if p["mine"] and p["waiting"]),
            "asked": sum(1 for p in prs if p["asked"])}
