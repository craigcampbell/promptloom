"""Briefs and threads: what an assistant should know, and lines of work."""

import json
import threading
import urllib.request

import pytest

from promptloom import brief, config, connect, server
from promptloom.model import Session
from promptloom.store import Record
from promptloom.userdata import UserData


def record(sid, context, *prompts, day=1, title="", summary=""):
    session = Session("claude-code", sid, context=context)
    for i, text in enumerate(prompts):
        session.add_prompt(f"2026-01-{day:02d}T{10 + i:02d}:00:00Z", text)
    rec = Record(session)
    if title or summary:
        rec.enrich = {"title": title, "summary": summary}
    return rec


def test_conventions_find_rules_said_as_rules_and_skip_the_rest():
    recs = [record("a", "/w/shop", "Never commit the .env file. Also the form renders wrong",
                   "Always run the tests before you push. Should we use redis?",
                   "I think maybe we could use postgres?"),
            record("b", "/w/shop", "never commit the .env file", day=2),
            record("c", "/w/shop", "This session is being continued from a previous conversation. "
                   "Resume directly — do not acknowledge the summary. Always do X.", day=3)]
    found = brief.conventions(recs)
    texts = {c["text"]: c["times"] for c in found}
    assert texts["Never commit the .env file."] == 2  # said twice, in different words
    assert texts["Always run the tests before you push."] == 1
    assert not any("renders wrong" in t or "redis" in t or "postgres" in t for t in texts)
    assert not any("Always do X" in t for t in texts)  # a compaction prompt, not the user


def test_brief_renders_what_you_wrote_first_and_only_repeated_rules():
    recs = [record("a", "/w/shop", "Never commit the .env file.", title="Set up shop",
                   summary="Scaffolded the shop."),
            record("b", "/w/shop", "never commit the .env file. Keep the API small.", day=2,
                   title="Add the API", summary="Added the API."),
            record("c", "/w/shop", "the deploy broke after the node upgrade", day=3)]
    recs[1].actions = [{"id": "a-1", "text": "Write the README", "done": False}]
    auto = brief.draft_brief("shop", recs)
    text = brief.render_brief(auto, "We ship on Fridays.")
    assert text.startswith("# About shop\n\nWe ship on Fridays.\n")
    assert "## Where things stand" in text and "Add the API: Added the API." in text
    assert "- Never commit the .env file. (said 2×)" in text
    assert "Keep the API small" not in text  # said once: a suggestion, not in the brief
    assert "## What has gone wrong before" in text and "deploy broke" in text
    assert "## Still open\n- Write the README" in text

    hidden = [p["key"] for p in auto["pitfalls"]]
    assert not brief.draft_brief("shop", recs, hidden)["pitfalls"]
    short = brief.for_prompt(auto, "x" * 3000, limit=500)
    assert len(short) <= 560 and short.endswith("project_brief]\n")


def test_threads_group_linked_sessions_in_a_project_and_attach_drafts_and_prs():
    recs = [record("a", "/w/shop", "the websocket gateway drops redis messages", day=1,
                   title="Gateway drops"),
            record("b", "/w/shop", "websocket gateway reconnects, redis messages lost again",
                   "more", "more", day=3, title="Gateway again"),
            record("c", "/w/shop", "write the recipe parser for markdown files", day=5,
                   title="Recipe parser"),
            record("d", "/w/game", "lobby websocket gateway for players and redis", day=2,
                   title="Lobby")]
    web = connect.Web(recs)
    drafts = [{"id": "d1", "title": "fix", "updated": "2026-01-06T00:00:00Z", "context": "/w/shop",
               "sent": ""}]
    prs = [{"url": "u", "number": 1, "title": "pr", "checkout": "/w/shop",
            "updated": "2026-01-04T00:00:00Z"}]
    found = brief.threads(recs, web, drafts, prs)
    by_name = {t.name: t for t in found}
    assert {r.session.session_id for r in by_name["Gateway again"].sessions} == {"a", "b"}
    assert "Recipe parser" in by_name and "Lobby" in by_name  # not merged across projects
    assert set(by_name["Gateway again"].words) & {"websocket gateway", "redis messages"}
    newest_shop = max((t for t in found if t.project == "shop"), key=lambda t: t.updated)
    assert [d["id"] for d in newest_shop.drafts] == ["d1"] and newest_shop.prs[0]["number"] == 1

    thread_id = by_name["Gateway again"].id
    again = brief.threads(recs, web, settings={"threads": {thread_id: {"name": "Gateway work"}}})
    assert any(t.name == "Gateway work" for t in again)
    closed = brief.threads(recs, web, settings={"threads": {thread_id: {"closed": True}}})
    assert not any(t.id == thread_id for t in closed)


def test_handoff_prompt_carries_the_work_over():
    rec = record("a", "/w/shop", "add the gateway", "now add retries", title="Gateway",
                 summary="Built the gateway.")
    rec.actions = [{"id": "a-1", "text": "Add metrics", "done": False}]
    text = brief.handoff_prompt(rec, "# About shop\nWe ship on Fridays.", "Codex")
    assert text.startswith("I am continuing work from another session (Gateway) in Codex.")
    assert "What was done: Built the gateway." in text
    assert "- now add retries" in text and "- Add metrics" in text
    assert text.endswith("We ship on Fridays.")


@pytest.fixture
def api(tmp_path):
    from promptloom.engine import Loom

    root = tmp_path / "claude" / "proj"
    root.mkdir(parents=True)
    for sid, text, ts in (("s1", "Never commit the .env file.", "2026-01-01T10:00:00Z"),
                          ("s2", "never commit the .env file. Prefer tiny endpoints.",
                           "2026-01-02T10:00:00Z")):
        (root / f"{sid}.jsonl").write_text(json.dumps({
            "type": "user", "sessionId": sid, "cwd": "/w/shop", "timestamp": ts,
            "message": {"role": "user", "content": text}}) + "\n")
    cfg = config.load(tmp_path / "missing.toml")
    cfg["harness"] = {"claude-code": {"root": str(tmp_path / "claude")}}
    loom = Loom(cfg, state_dir=tmp_path / "state", plugin_dir=tmp_path / "plugins")
    loom.backend = None
    loom.sync(enrich_limit=0)
    httpd = server.serve(cfg, tmp_path / "state", port=0, plugin_dir=tmp_path / "none")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())


def test_http_api_for_briefs_threads_and_handoff(api):
    data = call(api + "/api/brief?project=shop")
    assert "(said 2×)" in data["text"] and data["own"] == ""
    suggestion = next(c for c in data["auto"]["conventions"] if "tiny endpoints" in c["text"])
    data = call(api + "/api/brief", {"project": "shop", "text": "We ship on Fridays.",
                                     "hide": suggestion["key"]})
    assert data["text"].startswith("# About shop\n\nWe ship on Fridays.")
    assert not any(c["key"] == suggestion["key"] for c in data["auto"]["conventions"])
    assert "We ship on Fridays." in call(api + "/api/brief?project=shop")["prompt"]

    [thread] = call(api + "/api/threads")
    assert thread["project"] == "shop" and len(thread["sessions"]) == 2
    assert call(api + "/api/thread", {"id": thread["id"], "name": "Env hygiene", "pinned": True})
    [thread] = call(api + "/api/threads")
    assert thread["name"] == "Env hygiene" and thread["pinned"]

    hand = call(api + "/api/handoff?key=claude-code:s1&tool=codex")
    assert hand["context"] == "/w/shop" and "We ship on Fridays." in hand["prompt"]
    with pytest.raises(urllib.error.HTTPError):
        call(api + "/api/brief?project=nope")
