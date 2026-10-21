"""Worktrees and checkouts, against real throwaway git repositories."""

import subprocess
import sys
from pathlib import Path

import pytest

from promptloom import gitlocal


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A checkout of a bare 'remote', with main pushed."""
    remote = tmp_path / "remote.git"
    run("git", "init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    checkout = tmp_path / "shop"
    run("git", "clone", str(remote), str(checkout), cwd=tmp_path)
    for key, value in (("user.name", "T"), ("user.email", "t@example.com"),
                       ("commit.gpgsign", "false")):
        run("git", "config", key, value, cwd=checkout)
    (checkout / "a.txt").write_text("a\n")
    run("git", "add", ".", cwd=checkout)
    run("git", "commit", "-m", "first", cwd=checkout)
    run("git", "push", "-u", "origin", "main", cwd=checkout)
    return checkout


def branch_worktree(repo, name, push=True):
    path = gitlocal.add(repo, name)
    if push:
        run("git", "push", "-u", "origin", name, cwd=path)
    return path


def by_path(repo):
    return {Path(t.path).name: t for t in gitlocal.worktrees(repo)[1:]}


def test_github_repo_from_https_and_ssh_remotes(tmp_path):
    for url, want in (("https://github.com/acme/shop.git", "acme/shop"),
                      ("git@github.com:acme/shop.js.git", "acme/shop.js"),
                      ("https://gitlab.com/acme/shop.git", "")):
        (tmp_path / ".git").mkdir(exist_ok=True)
        (tmp_path / ".git" / "config").write_text(
            f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n')
        assert gitlocal.github_repo(tmp_path) == want


def test_add_puts_worktrees_in_an_ignored_folder_and_reuses_them(repo):
    path = gitlocal.add(repo, "feature/login")
    assert path == repo / ".worktrees" / "feature-login" and path.is_dir()
    assert "/.worktrees/" in (repo / ".git" / "info" / "exclude").read_text()
    assert gitlocal.git(["status", "--porcelain"], repo) == ""  # the folder is not noise
    assert gitlocal.add(repo, "feature/login") == path


def test_only_worktrees_that_lose_nothing_are_stale(repo):
    branch_worktree(repo, "pushed")
    (branch_worktree(repo, "dirty") / "new.txt").write_text("x")
    ahead = branch_worktree(repo, "ahead")
    (ahead / "a.txt").write_text("changed\n")
    run("git", "commit", "-am", "local only", cwd=ahead)
    locked = branch_worktree(repo, "locked")
    run("git", "worktree", "lock", str(locked), cwd=repo)

    trees = by_path(repo)
    assert trees["pushed"].stale and "on the remote" in trees["pushed"].reason
    assert not trees["dirty"].stale and "1 uncommitted" in trees["dirty"].reason
    assert not trees["ahead"].stale and "1 commit not on any remote" in trees["ahead"].reason
    assert not trees["locked"].stale and "locked" in trees["locked"].reason


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_worktree_in_use_is_kept(repo):
    path = branch_worktree(repo, "busy")
    sleeper = subprocess.Popen(["sleep", "30"], cwd=path)
    try:
        tree = by_path(repo)["busy"]
        assert not tree.stale and "in use by sleep" in tree.reason
    finally:
        sleeper.kill()


def test_remove_and_clean(repo):
    branch_worktree(repo, "done")
    branch_worktree(repo, "also-done")
    (branch_worktree(repo, "busy") / "wip.txt").write_text("x")
    gone = branch_worktree(repo, "gone")
    subprocess.run(["rm", "-rf", str(gone)], check=True)

    with pytest.raises(gitlocal.GitError, match="uncommitted"):
        gitlocal.remove(repo, str(repo / ".worktrees" / "busy"))

    assert "and branch done" in gitlocal.remove(repo, str(repo / ".worktrees" / "done"),
                                                delete_branch=True)
    results = gitlocal.clean([repo], only=[str(gone)])
    assert [r["ok"] for r in results] == [True] and "folder was gone" in results[0]["message"]
    assert set(by_path(repo)) == {"also-done", "busy"}
    assert [r["path"] for r in gitlocal.clean([repo])] == [str(repo / ".worktrees" / "also-done")]
    assert set(by_path(repo)) == {"busy"}


def test_checkouts_from_projects_and_roots(repo, tmp_path):
    gitlocal.git(["remote", "set-url", "origin", "git@github.com:acme/shop.git"], repo)
    other = tmp_path / "other"
    other.mkdir()
    assert gitlocal.default_roots([str(repo / "src")]) == [str(tmp_path)]
    assert gitlocal.checkouts([], [str(tmp_path)]) == {"acme/shop": repo}
    assert gitlocal.candidates([str(repo)], [str(tmp_path)]) == [repo]
    [item] = gitlocal.overview([repo])
    assert item["repo"] == "acme/shop" and item["branch"] == "main" and item["worktrees"] == []
