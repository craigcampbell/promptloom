"""Local git checkouts: which GitHub repository each project is, and its worktrees.

Promptloom only reads git here, except when asked to add or remove a worktree.
A worktree is stale when removing it loses nothing: no uncommitted or untracked
changes, every commit already on a remote, not locked, and nothing running in it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

WORKTREE_DIR = ".worktrees"  # inside the checkout, kept out of git by .git/info/exclude
_GITHUB = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/*$")
_REMOTE = re.compile(r'^\[remote "([^"]+)"\]\s*$')
_URL = re.compile(r"^\s*url\s*=\s*(\S+)")


class GitError(Exception):
    pass


def git(args: list[str], cwd: str | Path, timeout: int = 30) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]}: {exc}") from exc
    if done.returncode:
        raise GitError((done.stderr or done.stdout).strip() or f"git {args[0]} failed")
    return done.stdout


def github_repo(checkout: Path) -> str:
    """"owner/name" of the checkout's GitHub remote (origin first), or ""."""
    try:
        text = (checkout / ".git" / "config").read_text()
    except OSError:
        return ""
    remotes: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        match = _REMOTE.match(line)
        if match:
            current = match.group(1)
        elif line.lstrip().startswith("["):
            current = ""
        elif current and (url := _URL.match(line)):
            remotes.setdefault(current, url.group(1))
    for name in ["origin", *sorted(remotes)]:
        found = _GITHUB.search(remotes.get(name, ""))
        if found:
            return f"{found.group(1)}/{found.group(2)}"
    return ""


def toplevel(path: str) -> Path | None:
    """The main checkout holding a directory, found without running git."""
    if not path.startswith("/"):
        return None
    here = Path(path)
    for folder in [here, *here.parents]:
        if (folder / ".git").is_dir():
            return folder
        if folder == Path.home():
            break
    return None


def default_roots(contexts: list[str]) -> list[str]:
    """Folders that hold several checkouts: the parents of the projects seen so far."""
    roots = []
    for context in contexts:
        top = toplevel(context)
        if top and top.parent != Path("/") and str(top.parent) not in roots:
            roots.append(str(top.parent))
    return roots


def candidates(contexts: list[str], roots: list[str]) -> list[Path]:
    """Every checkout: those of the projects seen so far, then any in the roots."""
    found = [top for c in contexts if (top := toplevel(c))]
    for root in roots:
        try:
            found += sorted(p for p in Path(root).expanduser().iterdir()
                            if (p / ".git").is_dir())
        except OSError:
            continue
    return list(dict.fromkeys(found))


def checkouts(contexts: list[str], roots: list[str]) -> dict[str, Path]:
    """Local checkout for each GitHub repository ("owner/name", lowercased)."""
    found: dict[str, Path] = {}
    for checkout in candidates(contexts, roots):
        repo = github_repo(checkout)
        if repo:
            found.setdefault(repo.lower(), checkout)
    return found


def overview(paths: list[Path]) -> list[dict]:
    """Each checkout with its extra worktrees and what removing them would lose."""
    out = []
    for checkout in paths:
        try:
            trees = worktrees(checkout)
        except GitError:
            continue
        out.append({"checkout": str(checkout), "name": checkout.name,
                    "repo": github_repo(checkout), "branch": trees[0].branch,
                    "worktrees": [t.to_dict() for t in trees[1:]]})
    return out


def clean(paths: list[Path], only: list[str] | None = None,
          delete_branches: bool = False) -> list[dict]:
    """Remove stale worktrees (all of them, or those in `only`); report each one."""
    done = []
    for item in overview(paths):
        for tree in item["worktrees"]:
            if not tree["stale"] or (only is not None and tree["path"] not in only):
                continue
            try:
                message = remove(Path(item["checkout"]), tree["path"], delete_branches)
                done.append({"path": tree["path"], "ok": True, "message": message})
            except GitError as exc:
                done.append({"path": tree["path"], "ok": False, "message": str(exc)})
    return done


@dataclass
class Worktree:
    path: str
    checkout: str  # the main checkout it belongs to
    branch: str = ""  # "" when detached
    head: str = ""
    main: bool = False
    locked: bool = False
    missing: bool = False  # its folder is gone; git only needs telling
    dirty: int = 0  # changed or untracked files
    unpushed: int = 0  # commits on no remote
    behind: int = 0  # commits on the upstream it does not have
    upstream: str = ""
    in_use: list[str] = field(default_factory=list)
    stale: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def worktrees(checkout: Path) -> list[Worktree]:
    """Every worktree of a checkout, the extra ones with what removing them would lose."""
    out: list[Worktree] = []
    current: Worktree | None = None
    for line in git(["worktree", "list", "--porcelain"], checkout).splitlines() + [""]:
        if line.startswith("worktree "):
            current = Worktree(path=line[9:], checkout=str(checkout), main=not out)
        elif current is None:
            continue
        elif line.startswith("HEAD "):
            current.head = line[5:]
        elif line.startswith("branch "):
            current.branch = line[7:].removeprefix("refs/heads/")
        elif line.startswith("locked"):
            current.locked = True
        elif line.startswith("prunable"):
            current.missing = True
        elif not line:
            out.append(current)
            current = None
    for tree in out[1:]:
        inspect(tree)
    return out


def inspect(tree: Worktree) -> Worktree:
    """Fill in what removing the worktree would lose, and decide if it is stale."""
    if tree.missing or not Path(tree.path).is_dir():
        tree.missing, tree.stale = True, not tree.locked
        tree.reason = "its folder is gone" + (", but it is locked" if tree.locked else "")
        return tree
    status = git(["status", "--porcelain", "--untracked-files=normal"], tree.path)
    tree.dirty = len(status.splitlines())
    # Commits reachable from HEAD and from no remote-tracking branch: removing the
    # worktree would leave them on a local branch at best.
    tree.unpushed = int(git(["rev-list", "--count", "HEAD", "--not", "--remotes"],
                            tree.path).strip() or 0)
    try:
        tree.upstream = git(["rev-parse", "--abbrev-ref", "@{upstream}"], tree.path).strip()
        tree.behind = int(git(["rev-list", "--count", "HEAD..@{upstream}"], tree.path).strip())
    except GitError:
        tree.upstream, tree.behind = "", 0
    tree.in_use = processes_in(tree.path)
    why = []
    if tree.dirty:
        why.append(f"{tree.dirty} uncommitted or untracked file{'s' * (tree.dirty > 1)}")
    if tree.unpushed:
        why.append(f"{tree.unpushed} commit{'s' * (tree.unpushed > 1)} not on any remote")
    if tree.locked:
        why.append("locked")
    if tree.in_use:
        why.append("in use by " + ", ".join(tree.in_use))
    tree.stale = not why
    if why:
        tree.reason = "keep: " + "; ".join(why)
    else:
        tree.reason = "clean and everything is on the remote" + (
            f" ({tree.behind} behind {tree.upstream})" if tree.behind else "")
    return tree


def processes_in(path: str) -> list[str]:
    """Programs whose working directory is inside the path (Linux only)."""
    if not sys.platform.startswith("linux"):
        return []
    root = os.path.realpath(path)
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
            if cwd == root or cwd.startswith(root + "/"):
                name = Path(f"/proc/{pid}/comm").read_text().strip()
                found.append(f"{name} ({pid})")
        except OSError:
            continue  # gone, or someone else's
    return found


def remove(checkout: Path, path: str, delete_branch: bool = False) -> str:
    """Remove a stale worktree, checking again first. Returns what was done."""
    tree = next((t for t in worktrees(checkout)[1:] if t.path == path), None)
    if tree is None:
        raise GitError(f"{path} is not a worktree of {checkout}")
    if not tree.stale:
        raise GitError(f"{path}: {tree.reason}")
    if tree.missing:
        git(["worktree", "prune"], checkout)
        return f"forgot {path} (its folder was gone)"
    git(["worktree", "remove", path], checkout)  # never --force: git refuses if anything changed
    done = f"removed {path}"
    if delete_branch and tree.branch:
        try:
            # -d (not -D): git refuses if the branch is not merged into its upstream.
            git(["branch", "-d", tree.branch], checkout)
            done += f" and branch {tree.branch}"
        except GitError as exc:
            done += f"; kept branch {tree.branch} ({exc})"
    return done


def slug(branch: str) -> str:
    return re.sub(r"[^\w.-]+", "-", branch).strip("-.") or "worktree"


def _exclude(checkout: Path) -> None:
    """Keep the worktree folder out of `git status` without touching .gitignore."""
    try:
        git(["check-ignore", "-q", WORKTREE_DIR + "/x"], checkout)
        return  # already ignored
    except GitError:
        pass
    exclude = checkout / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as fh:
        fh.write(f"\n/{WORKTREE_DIR}/\n")


def add(checkout: Path, branch: str, base: str = "") -> Path:
    """A worktree for a branch: the existing one if it has one, else a new one.

    An existing branch is checked out as it is; a new one starts from `base`
    (default: the checkout's HEAD).
    """
    for tree in worktrees(checkout):
        if tree.branch == branch:
            return Path(tree.path)
    path = checkout / WORKTREE_DIR / slug(branch)
    if path.exists():
        raise GitError(f"{path} already exists")
    _exclude(checkout)
    try:
        git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], checkout)
        git(["worktree", "add", str(path), branch], checkout)
    except GitError:
        args = ["worktree", "add", "-b", branch, str(path)]
        git(args + ([base] if base else []), checkout)
    return path


def add_for_pr(checkout: Path, number: int, branch: str) -> Path:
    """A worktree on a pull request's branch, set up by `gh pr checkout` (forks too)."""
    for tree in worktrees(checkout):
        if branch and tree.branch == branch:
            return Path(tree.path)
    path = checkout / WORKTREE_DIR / slug(branch or f"pr-{number}")
    if not path.exists():
        _exclude(checkout)
        git(["worktree", "add", "--detach", str(path)], checkout)
    try:
        done = subprocess.run(["gh", "pr", "checkout", str(number)], cwd=path,
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"gh pr checkout: {exc}") from exc
    if done.returncode:
        raise GitError((done.stderr or done.stdout).strip() or "gh pr checkout failed")
    return path
