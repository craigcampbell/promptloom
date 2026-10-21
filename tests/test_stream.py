"""The stream, tags with colours, collections, the organiser and the MCP write tools."""

import json
import threading
import urllib.request

import pytest

from promptloom import analysis, config, launch, mcp, server
from promptloom.engine import Loom
from promptloom.userdata import UserData


def line(sid, text, cwd, ts):
    return json.dumps({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": ts,
                       "message": {"role": "user", "content": text}}) + "\n"


@pytest.fixture
def loom(tmp_path):
    root = tmp_path / "claude" / "proj"
    root.mkdir(parents=True)
    (root / "s1.jsonl").write_text(
        line("s1", "set up billing with stripe", "/w/shop", "2026-01-01T10:00:00Z")
        + line("s1", "add invoices to billing", "/w/shop", "2026-01-02T10:00:00Z"))
    (root / "s2.jsonl").write_text(line("s2", "lobby for players", "/w/game", "2026-01-03T10:00:00Z"))
    cfg = config.load(tmp_path / "missing.toml")
    cfg["harness"] = {"claude-code": {"root": str(tmp_path / "claude")}}
    loom = Loom(cfg, state_dir=tmp_path / "state", plugin_dir=tmp_path / "plugins")
    loom.backend = None
    loom.sync(enrich_limit=0)
    return loom


def test_stream_pages_newest_first_with_tags_notes_and_collections(loom):
    user = UserData(loom.store.dir)
    with user.editing() as u:
        u.add_tags("claude-code:s1#0", ["billing", "Work Stuff"])
        u.set_tag_color("billing", "green")
        col = u.put_collection({"name": "Money", "color": "teal"})
        u.collect(col["id"], ["claude-code:s1#0", "nope"])
        u.put_note({"title": "n", "body": "b", "prompt": "claude-code:s1#1", "session": "claude-code:s1"})
    records = loom.store.by_recency()
    page = analysis.stream(records, user, limit=2)
    assert [i["ref"] for i in page["items"]] == ["claude-code:s2#0", "claude-code:s1#1"]
    assert page["total"] == 3 and page["next"] == "2026-01-02T10:00:00Z"
    assert page["items"][1]["notes"][0]["title"] == "n"
    rest = analysis.stream(records, user, before=page["next"])
    assert [i["ref"] for i in rest["items"]] == ["claude-code:s1#0"] and rest["next"] == ""
    first = rest["items"][0]
    assert first["user_tags"] == ["billing", "work-stuff"] and first["collections"] == [col["id"]]
    assert [i["ref"] for i in analysis.stream(records, user, "tag:billing")["items"]] == ["claude-code:s1#0"]
    assert [i["ref"] for i in analysis.stream(records, user, "project:game")["items"]] == ["claude-code:s2#0"]
    tags = analysis.user_tags(records, user)
    assert {t["tag"]: (t["count"], t["color"]) for t in tags} == {"billing": (1, "green"), "work-stuff": (1, "")}
    with user.editing() as u:
        assert u.add_tags("claude-code:s1#0", ["billing"], remove=True) == ["work-stuff"]
        assert u.collect(col["id"], ["claude-code:s1#0"], remove=True)["items"] == []
        assert u.put_collection({"name": "money"})["id"] == col["id"]  # same name, same collection


def test_organizer_keeps_only_sane_actions(tmp_path, monkeypatch):
    class Backend:
        def ready(self):
            return ""

        def complete(self, prompt, schema):
            assert "Instruction: tag billing" in prompt and "claude-code:s1#0 | " in prompt
            return {"summary": "Two tags.", "actions": [
                {"op": "tag", "ref": "claude-code:s1#0", "value": "billing", "why": "stripe"},
                {"op": "collect", "ref": "claude-code:s1#1", "value": "Money"},
                {"op": "tag", "ref": "made-up#9", "value": "x"},
                {"op": "delete", "ref": "claude-code:s1#0", "value": "x"},
                "junk"]}

    monkeypatch.setattr(launch, "weaver", lambda cfg, choice, workdir: Backend())
    rows = ["claude-code:s1#0 | 2026-01-01 | shop | claude-code | - | set up billing",
            "claude-code:s1#1 | 2026-01-02 | shop | claude-code | - | add invoices"]
    out = launch.organize({}, str(tmp_path), "fake", "tag billing", rows)
    assert out["summary"] == "Two tags."
    assert [(a["op"], a["ref"], a["value"]) for a in out["actions"]] == [
        ("tag", "claude-code:s1#0", "billing"), ("collect", "claude-code:s1#1", "Money")]
    with pytest.raises(launch.LaunchError, match="say what"):
        launch.organize({}, str(tmp_path), "fake", " ", rows)


@pytest.fixture
def http(loom):
    httpd = server.serve(loom.cfg, loom.store.dir, port=0, plugin_dir=loom.store.dir / "none")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())


def test_http_api_for_stream_tags_collections_and_apply(http):
    assert len(call(http + "/api/stream")["items"]) == 3
    assert call(http + "/api/prompt-tags", {"ref": "claude-code:s1#0", "tags": ["#Billing"]})["tags"] == ["billing"]
    assert call(http + "/api/tag-color", {"tag": "billing", "color": "green"})["colors"] == {"billing": "green"}
    assert call(http + "/api/overview")["tag_colors"] == {"billing": "green"}
    col = call(http + "/api/collections", {"name": "Money"})
    assert call(http + "/api/collect", {"id": col["id"], "ref": "claude-code:s1#1"})["items"] == ["claude-code:s1#1"]
    assert call(http + "/api/collections")[0]["items"][0]["text"] == "add invoices to billing"
    done = call(http + "/api/apply", {"actions": [
        {"op": "tag", "ref": "claude-code:s2#0", "value": "game"},
        {"op": "collect", "ref": "claude-code:s2#0", "value": "Money"},
        {"op": "untag", "ref": "claude-code:s1#0", "value": "billing"}, {"op": "x"}]})
    assert done["applied"] == 3
    stream = call(http + "/api/stream")
    by_ref = {i["ref"]: i for i in stream["items"]}
    assert by_ref["claude-code:s2#0"]["user_tags"] == ["game"] and by_ref["claude-code:s1#0"]["user_tags"] == []
    assert [c["count"] for c in stream["collections"]] == [2]
    assert call(http + "/api/collections", {"delete": col["id"]}) == {"deleted": col["id"]}


def test_mcp_tools_can_organize(loom, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", loom.store.dir)
    tools = mcp.Tools()
    listing = tools.recent_prompts(limit=2)
    assert listing.startswith("3 prompts (showing 2)") and "claude-code:s2#0" in listing
    assert tools.tag_prompt("claude-code:s1#0", ["billing"]) == "claude-code:s1#0 now tagged: billing"
    assert "- billing (1)" in tools.list_tags()
    assert tools.add_to_collection("Money", ["claude-code:s1#0", "claude-code:s1#1"]).startswith("2 added to Money")
    assert "Money (2 prompts" in tools.list_collections()
    assert tools.note_on("watch the stripe webhook", ref="claude-code:s1#0").startswith("Note saved: watch the stripe")
    assert tools.create_draft("add refunds", tool="claude", context="/w/shop").startswith("Draft saved: add refunds")
    assert "shop" in tools.list_threads()
    names = {t["name"] for t in mcp.TOOLS}
    assert {"recent_prompts", "tag_prompt", "list_tags", "list_collections", "add_to_collection",
            "note_on", "list_threads", "create_draft", "project_brief"} <= names
    user = UserData(loom.store.dir)
    user.load()
    assert user.notes[0]["prompt"] == "claude-code:s1#0" and user.notes[0]["project"] == "shop"
