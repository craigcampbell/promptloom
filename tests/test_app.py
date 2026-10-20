"""The built-in app's data: models on prompts, tags, search, timelines, the graph,
the HTTP API, the ingest harness, Copilot and the MCP server."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from promptloom import analysis, config, connect, mcp, server
from promptloom.engine import Loom
from promptloom.harnesses.copilot import Copilot, replay
from promptloom.model import Session
from promptloom.userdata import UserData


def claude(sid, kind, text, cwd, ts, model=""):
    message = {"role": kind, "content": text}
    if model:
        message["model"] = model
    return json.dumps({"type": kind, "sessionId": sid, "cwd": cwd, "timestamp": ts,
                       "message": message}) + "\n"


@pytest.fixture
def loom(tmp_path):
    root = tmp_path / "claude" / "proj"
    root.mkdir(parents=True)
    with open(root / "s1.jsonl", "w") as fh:
        fh.write(claude("s1", "user", "set up the redis websocket gateway", "/w/api",
                        "2026-01-01T10:00:00Z"))
        fh.write(claude("s1", "assistant", "done", "/w/api", "2026-01-01T10:01:00Z",
                        "claude-opus-5-5"))
        fh.write(claude("s1", "user", "now add retries to the gateway", "/w/api",
                        "2026-01-02T10:00:00Z"))
        fh.write(claude("s1", "assistant", "ok", "/w/api", "2026-01-02T10:01:00Z",
                        "claude-sonnet-5-5"))
    with open(root / "s2.jsonl", "w") as fh:
        fh.write(claude("s2", "user", "subscribe the app to the redis websocket gateway",
                        "/w/app", "2026-01-03T10:00:00Z"))
        fh.write(claude("s2", "assistant", "ok", "/w/app", "2026-01-03T10:01:00Z",
                        "claude-opus-5-5"))
    cfg = config.load(tmp_path / "missing.toml")
    cfg["harness"] = {"claude-code": {"root": str(tmp_path / "claude")}, "api": {}}
    loom = Loom(cfg, state_dir=tmp_path / "state", plugin_dir=tmp_path / "plugins")
    loom.backend = None
    loom.sync(enrich_limit=0)
    return loom


def test_prompts_carry_the_model_that_answered(loom):
    prompts = loom.store.records["claude-code:s1"].session.prompts
    assert [p.model for p in prompts] == ["claude-opus-5-5", "claude-sonnet-5-5"]


def test_vault_is_off_by_default(loom, tmp_path):
    assert not loom.vault.enabled
    assert loom.read_status()["vault"] == ""
    assert loom.read_status()["ui"] == "http://127.0.0.1:8765/"
    assert not any(tmp_path.glob("**/*.md"))


def test_old_state_is_reread_for_models(loom):
    loom.store.records["claude-code:s1"].enrich = {"summary": "kept", "prompt_count": 2}
    for record in loom.store.records.values():
        for prompt in record.session.prompts:
            prompt.model = ""
    loom.store.save()
    raw = json.loads(loom.store.path.read_text())
    raw["version"] = 1
    loom.store.path.write_text(json.dumps(raw))
    loom.store._loaded = None
    loom.sync(enrich_limit=0)
    record = loom.store.records["claude-code:s1"]
    assert [p.model for p in record.session.prompts] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert record.summary == "kept" and len(record.session.prompts) == 2
    assert json.loads(loom.store.path.read_text())["version"] == 2


def test_search_words_and_filters(loom):
    records, user = loom.store.by_recency(), UserData(loom.store.dir)
    assert analysis.search(records, user, "gateway")["total"] == 3
    assert analysis.search(records, user, "gateway model:sonnet")["total"] == 1
    assert analysis.search(records, user, "gateway project:app")["total"] == 1
    assert analysis.search(records, user, '"websocket gateway" after:2026-01-02')["total"] == 1
    hit = analysis.search(records, user, "retries")["hits"][0]
    assert "model:claude-sonnet-5-5" in hit["tags"] and "project:api" in hit["tags"]


def test_user_tags_and_notes_are_searchable(loom):
    user = UserData(loom.store.dir)
    with user.editing():
        user.set_tags("claude-code:s1", ["#Idea", "idea", "perf"], prompt=1)
        user.put_note({"title": "Gateway plan", "body": "shard the gateway", "tags": ["plan"],
                       "project": "api"})
    user = UserData(loom.store.dir)
    user.load()
    assert user.prompt_tags["claude-code:s1#1"] == ["idea", "perf"]
    records = loom.store.by_recency()
    result = analysis.search(records, user, "tag:idea")
    assert result["total"] == 1 and result["hits"][0]["index"] == 1
    assert [n["title"] for n in analysis.search(records, user, "shard")["notes"]] == ["Gateway plan"]


def test_timeline_and_handoffs(loom):
    records, user = loom.store.by_recency(), UserData(loom.store.dir)
    data = analysis.timeline(records, "api", user)
    assert [d["models"] for d in data["days"]] == [{"claude-opus-5-5": 1}, {"claude-sonnet-5-5": 1}]
    assert [(h["from"], h["to"]) for h in data["handoffs"]] == [
        ("claude-opus-5-5", "claude-sonnet-5-5")]
    overview = analysis.models_overview(records)
    assert overview["shared"][0]["projects"] == ["api"]
    assert {m["model"]: m["prompts"] for m in overview["models"]}["claude-opus-5-5"] == 2


def test_graph_links_projects_through_shared_words(loom):
    records, user = loom.store.by_recency(), UserData(loom.store.dir)
    g = analysis.graph(records, user, connect.Web(records))
    ids = {n["id"] for n in g["nodes"]}
    assert {"project:api", "project:app", "model:claude-opus-5-5", "harness:claude-code"} <= ids
    links = [e for e in g["edges"] if e["kind"] == "link"]
    assert links and "gateway" in " ".join(links[0]["words"])


def test_model_family():
    assert analysis.model_family("claude-opus-5-5") == "claude-opus"
    assert analysis.model_family("copilot/gpt-4o") == "gpt-4o"
    assert analysis.model_family("") == "unknown"


def test_ingest_harness_files_prompts_under_the_sending_tool(loom):
    server.append_ingest(loom.store.dir, [
        json.dumps({"harness": "Aider CLI", "context": "/w/api", "prompt": "rename the gateway",
                    "model": "gpt-6", "session": "x1", "ts": "2026-01-04T10:00:00Z"}),
        json.dumps({"harness": "aider-cli", "session": "x1", "reply": "renamed",
                    "model": "gpt-6.1", "ts": "2026-01-04T10:01:00Z"})])
    loom.sync(enrich_limit=0)
    session = loom.store.records["aider-cli:x1"].session
    assert session.project == "api"
    assert [(p.text, p.model) for p in session.prompts] == [("rename the gateway", "gpt-6.1")]


def test_copilot_json_and_change_log(tmp_path):
    ws = tmp_path / "ws" / "abc123"
    (ws / "chatSessions").mkdir(parents=True)
    (ws / "workspace.json").write_text(json.dumps({"folder": "file:///home/me/My%20App"}))
    request = {"message": {"text": "why is the build slow"}, "timestamp": 1767261600000,
               "modelId": "copilot/gpt-4.1", "response": [{"value": "Because "}, {"value": "x"}]}
    (ws / "chatSessions" / "one.json").write_text(json.dumps(
        {"sessionId": "one", "customTitle": "Slow build", "requests": [request]}))
    log = [{"kind": 0, "v": {"sessionId": "two", "requests": []}},
           {"kind": 2, "k": ["requests"], "v": [dict(request, modelId="claude-sonnet-5-5")]},
           {"kind": 1, "k": ["customTitle"], "v": "Logged"}]
    (ws / "chatSessions" / "two.jsonl").write_text("\n".join(json.dumps(l) for l in log))

    sessions = {}
    harness = Copilot("copilot", {"root": str(tmp_path / "ws")})

    def get(sid, as_harness=""):
        return sessions.setdefault(sid, Session("copilot", sid))

    for path in harness.sources():
        harness.read(path, {}, get)
    one, two = sessions["one"], sessions["two"]
    assert one.context == "/home/me/My App" and one.title == "Slow build"
    assert [(p.text, p.model) for p in one.prompts] == [("why is the build slow", "gpt-4.1")]
    assert two.title == "Logged" and two.prompts[0].model == "claude-sonnet-5-5"
    assert replay(['{"kind":0,"v":{"a":[1,2,3]}}', '{"kind":2,"k":["a"],"v":[9],"i":1}']) == {
        "a": [1, 9]}


@pytest.fixture
def http(loom):
    httpd = server.serve(loom.cfg, loom.store.dir, port=0, plugin_dir=loom.store.dir / "none")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data, headers={
        "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())


def test_http_api_reads_and_writes(http):
    assert call(http + "/api/overview")["prompts"] == 3
    assert call(http + "/api/search?q=retries")["total"] == 1
    note = call(http + "/api/notes", {"title": "Plan", "body": "b", "tags": ["x"]})
    assert call(http + "/api/notes")[0]["id"] == note["id"]
    assert call(http + "/api/tags", {"key": "claude-code:s1", "tags": ["wow"]})["tags"] == ["wow"]
    assert call(http + "/api/ingest", {"prompt": "hi", "harness": "t"})["queued"] == 1
    with urllib.request.urlopen(http + "/") as res:
        assert b"Promptloom" in res.read()


def test_http_api_refuses_other_sites(http):
    with pytest.raises(urllib.error.HTTPError) as err:
        call(http + "/api/notes", {"title": "x"}, {"Origin": "https://evil.example"})
    assert err.value.code == 403
    req = urllib.request.Request(http + "/api/overview", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(urllib.request.Request(
            http + "/api/notes", b"title=x", headers={"Content-Type": "text/plain"}))
    assert err.value.code == 400


def test_mcp_tools(loom, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", loom.store.dir)
    tools = mcp.Tools()
    init = mcp.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "promptloom"
    listed = mcp.handle(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert "search_prompts" in {t["name"] for t in listed["result"]["tools"]}
    found = mcp.handle(tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "search_prompts", "arguments": {"query": "retries"}}})
    assert "claude-sonnet-5-5" in found["result"]["content"][0]["text"]
    history = mcp.handle(tools, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "project_history", "arguments": {"project": "api"}}})
    assert "claude-opus-5-5 → claude-sonnet-5-5" in history["result"]["content"][0]["text"]
    assert mcp.handle(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
