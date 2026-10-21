"""Pull requests: what GitHub says, what counts as new, prompts, runs and the API."""

import json
import os
import subprocess
import threading
import tomllib
import urllib.request

import pytest

from promptloom import config, prs, server
from promptloom.model import Session
from promptloom.store import Record

LOGIN = "me"
URL = "https://github.com/me/shop/pull/7"


def who(login, bot=False):
    return {"__typename": "Bot" if bot else "User", "login": login}


def node(url=URL, author="me", full=True, **extra):
    base = {
        "number": int(url.rsplit("/", 1)[1]), "title": "Faster checkout", "url": url,
        "isDraft": False, "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-05T00:00:00Z",
        "headRefName": "fast", "baseRefName": "main", "additions": 10, "deletions": 2,
        "changedFiles": 3, "reviewDecision": "CHANGES_REQUESTED",
        "repository": {"nameWithOwner": url.split("/")[3] + "/" + url.split("/")[4]},
        "author": {"login": author}, "commentCount": {"totalCount": 3},
        "commits": {"nodes": [{"commit": {"committedDate": "2026-01-02T00:00:00Z",
                                          "statusCheckRollup": {"state": "FAILURE"}}}]},
    }
    if full:
        base |= {
            "comments": {"nodes": [
                {"author": who("vercel", bot=True), "body": "Preview ready", "createdAt": "2026-01-03T00:00:00Z", "url": "u1"},
                {"author": who("sam"), "body": "Old point", "createdAt": "2026-01-01T12:00:00Z", "url": "u2"},
                {"author": who("sam"), "body": "Why a new cache?", "createdAt": "2026-01-03T00:00:00Z", "url": "u3"}]},
            "reviews": {"nodes": [
                {"author": who("copilot-reviewer", bot=True), "state": "COMMENTED", "body": "",
                 "submittedAt": "2026-01-03T00:00:00Z", "url": "u4"},
                {"author": who("ana"), "state": "CHANGES_REQUESTED", "body": "",
                 "submittedAt": "2026-01-04T00:00:00Z", "url": "u5"}]},
            "reviewThreads": {"nodes": [
                {"isResolved": False, "isOutdated": False, "path": "cart.py", "line": 12,
                 "comments": {"nodes": [
                     {"author": who("copilot-reviewer", bot=True), "body": "Off by one", "createdAt": "2026-01-03T01:00:00Z", "url": "u6"}]}},
                {"isResolved": False, "isOutdated": False, "path": "api.py", "line": 3,
                 "comments": {"nodes": [
                     {"author": who("ana"), "body": "Rename this", "createdAt": "2026-01-03T02:00:00Z", "url": "u7"},
                     {"author": who("me"), "body": "Done", "createdAt": "2026-01-03T03:00:00Z", "url": "u8"}]}},
                {"isResolved": True, "isOutdated": False, "path": "x.py", "line": 1,
                 "comments": {"nodes": [{"author": who("ana"), "body": "Fixed already", "createdAt": "2026-01-04T00:00:00Z", "url": "u9"}]}}]},
        }
    return base | extra


def answer(mine=None, theirs=(), asked=(), **repos):
    data = {"mine": {"nodes": [node()] if mine is None else mine},
            "theirs": {"nodes": list(theirs)}, "asked": {"nodes": list(asked)}}
    data.update(repos)
    return data


def test_parse_sorts_out_whose_and_what_is_new(tmp_path):
    theirs = node("https://github.com/me/shop/pull/9", author="sam", full=False)
    asked = node("https://github.com/acme/api/pull/2", author="lee", full=False)
    readable = {"viewerPermission": "READ", "pullRequests": {"nodes": [
        node("https://github.com/up/stream/pull/1", author="x", full=False)]}}
    seen_in = {URL.lower(): [{"key": "claude-code:s1", "title": "Speed up checkout"}]}
    out = prs.parse(answer(theirs=[theirs, {}], asked=[asked], r0=readable), LOGIN,
                    {"me/shop": tmp_path}, seen_in)

    by = {p["url"]: p for p in out}
    assert set(by) == {URL, theirs["url"], asked["url"]}  # not the read-only repo's
    mine = by[URL]
    assert mine["mine"] and not by[theirs["url"]]["mine"] and by[asked["url"]]["asked"]
    assert mine["checkout"] == str(tmp_path) and mine["project"] == tmp_path.name
    assert by[asked["url"]]["project"] == "api" and by[asked["url"]]["checkout"] == ""
    assert mine["checks"] == "FAILURE" and mine["sessions"][0]["key"] == "claude-code:s1"

    said = {(i["author"], i["body"]): i for i in mine["feedback"]}
    assert ("vercel", "Preview ready") not in said  # a bot's comment
    assert ("ana", "Fixed already") not in said  # a resolved thread
    assert ("copilot-reviewer", "") not in said  # an empty review
    assert said[("copilot-reviewer", "Off by one")]["new"]  # a bot's review line counts
    assert not said[("sam", "Old point")]["new"]  # before the last commit
    assert not said[("ana", "Rename this")]["new"]  # you replied after it
    assert said[("sam", "Why a new cache?")]["new"] and said[("ana", "")]["new"]
    assert mine["waiting"] == 3


def test_refresh_notifies_once_per_new_comment(tmp_path, monkeypatch):
    data = answer()
    calls, sent = [], []

    def fake_gh(args, stdin=None, timeout=60, check=True):
        calls.append(args[:2])
        return "me\n" if args[:2] == ["api", "user"] else json.dumps({"data": data})

    monkeypatch.setattr(prs, "gh", fake_gh)
    monkeypatch.setattr(prs, "notify_feedback", lambda cfg, due: sent.append(
        [p["number"] for p in due]))
    cfg = {"prs": {"notify": True, "roots": [str(tmp_path / "none")]}}

    state = prs.refresh(cfg, tmp_path, [])
    assert state["login"] == "me" and not state["error"] and sent == [[7]]
    prs.refresh(cfg, tmp_path, [])
    assert sent == [[7]] and calls.count(["api", "user"]) == 1  # the login is kept

    data["mine"]["nodes"][0]["comments"]["nodes"].append(
        {"author": who("sam"), "body": "One more", "createdAt": "2026-01-06T00:00:00Z", "url": "u10"})
    prs.refresh(cfg, tmp_path, [])
    assert sent == [[7], [7]]

    data["mine"]["nodes"] = []  # merged: forget it
    assert prs.refresh(cfg, tmp_path, [])["seen"] == {}

    monkeypatch.setattr(prs, "gh", lambda *a, **k: (_ for _ in ()).throw(prs.PRError("gh: offline")))
    assert prs.refresh(cfg, tmp_path, [])["error"] == "gh: offline"


def test_tick_checks_only_when_on_and_due(tmp_path, monkeypatch):
    done = []
    monkeypatch.setattr(prs, "refresh", lambda cfg, d, r: done.append(1) or prs.load(d))
    off = {"prs": {"enabled": False}}
    on = {"prs": {"enabled": True, "poll_minutes": 5}}
    assert not prs.tick(off, tmp_path, [])
    assert prs.tick(on, tmp_path, [])  # never checked
    with prs.editing(tmp_path) as state:
        state["checked"] = prs.extract.now_ts()
    assert not prs.tick(on, tmp_path, [])
    prs.request_refresh(tmp_path)
    assert prs.tick(on, tmp_path, []) and not (tmp_path / "prs.refresh").exists()
    assert len(done) == 2


def test_mentions_link_prs_to_sessions():
    session = Session("claude-code", "s1", context="/w/shop")
    session.add_prompt("2026-01-01T00:00:00Z", "see https://github.com/Me/Shop/pull/7/files")
    session.reply_urls = ["https://github.com/me/shop/pull/7"]
    found = prs.mentions([Record(session)])
    assert [s["key"] for s in found[URL]] == ["claude-code:s1"]


def test_prompts_put_your_words_last():
    pr = prs.parse(answer(), LOGIN, {}, {})[0]
    text = prs.review_prompt(pr, "Makes checkout fast.", "x" * (prs.DIFF_CHARS + 10),
                             "No checkout.", "Focus on caching")
    assert text.index("Makes checkout fast.") < text.index("```diff") < text.index("Focus on caching")
    assert "the rest of the diff is cut" in text and text.rstrip().endswith("Focus on caching")

    fix = prs.fix_prompt(pr, review="Rename cart_total", worktree=True, extra="Keep the API")
    assert "1. sam in a comment: Why a new cache?" in fix
    assert "cart.py:12" in fix and "Old point" not in fix  # only what is new
    assert "A review promptloom ran said:\n\nRename cart_total" in fix
    assert "worktree of its own" in fix and fix.endswith("Also:\n\nKeep the API")
    assert "gh pr checkout 7" in prs.fix_prompt(pr, worktree=False)


def test_agents_commands_and_availability(monkeypatch):
    monkeypatch.setattr(prs, "which", lambda p: "/bin/" + p if p in ("claude", "mine") else "")
    cfg = {"prs": {"agent": "claude", "agents": {"mine": {"label": "Mine", "review": ["mine"],
                                                           "fix": ["mine", "{prompt}"]}}}}
    known = prs.agents(cfg)
    assert known["claude"]["available"] and not known["codex"]["available"]
    assert known["mine"]["available"] and prs.agent(cfg)[0] == "claude"
    with pytest.raises(prs.PRError, match="not installed"):
        prs.agent(cfg, "codex")
    assert prs._fill(["c", "-p"], "hi", "/o") == (["c", "-p"], "hi")
    assert prs._fill(["c", "{prompt}", "-o", "{output}"], "hi", "/o") == (["c", "hi", "-o", "/o"], None)


def test_fix_script_passes_the_prompt_untouched(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("it's `rm -rf /` $(echo boom) \"quoted\"\nline two")
    pr = {"repo": "me/shop", "number": 7, "url": URL, "checkout": str(tmp_path), "head": "fast"}
    spec = {"label": "Echo", "fix": ["printf", "<%s>", "--{prompt}--"]}
    script = tmp_path / "fix.sh"
    script.write_text(prs.fix_script({"prs": {}}, pr, spec, prompt, worktree=False))
    out = subprocess.run(["sh", str(script)], capture_output=True, text=True,
                         env={**os.environ, "SHELL": "true"}).stdout
    assert "<--" + prompt.read_text() + "-->" in out

    text = prs.fix_script({"prs": {}}, pr, spec, prompt, worktree=True)
    assert "pr worktree" in text
    cloned = prs.fix_script({"prs": {"roots": [str(tmp_path)]}}, pr | {"checkout": ""}, spec,
                            prompt, worktree=True)
    assert "gh repo clone me/shop" in cloned and "gh pr checkout 7" in cloned


def test_settings_file_keeps_other_switches(tmp_path):
    path = tmp_path / "settings.toml"
    config.save_settings("prs", {"enabled": True, "agent": "codex"}, path)
    config.save_settings("prs", {"enabled": False}, path)
    config.save_settings("other", {"x": 1}, path)
    assert tomllib.loads(path.read_text()) == {
        "prs": {"enabled": False, "agent": "codex"}, "other": {"x": 1}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.toml")
    monkeypatch.setattr(prs, "which", lambda p: "/bin/" + p if p == "claude" else "")
    state_dir = tmp_path / "state"
    with prs.editing(state_dir) as state:
        state.update(login="me", checked="2026-01-05T00:00:00Z",
                     prs=prs.parse(answer(), LOGIN, {}, {}))
    cfg = config.load(tmp_path / "missing.toml")
    cfg["harness"] = {}
    httpd = server.serve(cfg, state_dir, port=0, plugin_dir=tmp_path / "none")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state_dir
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())


def test_http_api_for_pull_requests(api, monkeypatch):
    base, state_dir = api
    data = call(base + "/api/prs")
    assert [p["number"] for p in data["prs"]] == [7] and not data["settings"]["enabled"]
    assert {a["name"]: a["available"] for a in data["agents"]}["claude"]

    assert call(base + "/api/pr-settings", {"enabled": True, "agent": "claude"})["enabled"]
    assert call(base + "/api/prs")["settings"]["enabled"]
    assert "enabled = true" in config.SETTINGS_FILE.read_text()

    preview = call(base + f"/api/pr-prompt?kind=review&url={URL}&extra=Look+at+caching")
    assert preview["prompt"].rstrip().endswith("Look at caching")

    started = []
    monkeypatch.setattr(prs, "spawn", lambda argv, cwd=None: started.append(argv))
    run = call(base + "/api/pr-review", {"url": URL, "agent": "claude", "extra": "hm"})
    assert run["status"] == "running" and started[-1][-2:] == ["_run", run["id"]]
    with pytest.raises(urllib.error.HTTPError) as err:
        call(base + "/api/pr-review", {"url": URL, "agent": "codex"})
    assert err.value.code == 400  # not installed

    (state_dir / "reviews").mkdir()
    (state_dir / "reviews" / "r.md").write_text("Looks good")
    prs._update_run(state_dir, run["id"], status="done", output=str(state_dir / "reviews" / "r.md"))
    assert call(base + f"/api/pr-run?id={run['id']}")["text"] == "Looks good"
    fix = call(base + f"/api/pr-prompt?kind=fix&url={URL}")["prompt"]
    assert "A review promptloom ran said:\n\nLooks good" in fix
