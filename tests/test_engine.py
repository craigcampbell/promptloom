import json

import pytest

from promptloom import config, extract
from promptloom.engine import Loom
from promptloom.vault import END, obsidian_problem


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.prompts = []

    def ready(self):
        return ""

    def complete(self, prompt, schema):
        self.prompts.append(prompt)
        if "opportunities" in schema["properties"]:
            return {"themes": [{"name": "Auth", "description": "Logins.", "sessions": ["S1", "S2"]}],
                    "opportunities": [{"title": "Share the token helper", "detail": "Reuse it.",
                                       "sessions": ["S1", "S3", "S99"],
                                       "prompt": "Reuse the token helper from /home/me/app."}]}
        return {"title": "Fix login redirect", "summary": "Fixing the login redirect loop.",
                "themes": ["Authentication", "web_frontend"],
                "action_items": ["Rotate the staging API key"]}


def user_line(sid, text, cwd, ts="2026-01-02T03:04:05.000Z"):
    return json.dumps({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": ts,
                       "message": {"role": "user", "content": text}}) + "\n"


@pytest.fixture
def loom(tmp_path):
    root = tmp_path / "claude" / "proj"
    root.mkdir(parents=True)
    (root / "aaaa1111.jsonl").write_text(
        user_line("aaaa1111", "the login page redirects forever, see https://example.com/bug/1",
                  "/home/me/shop"))
    cfg = config.load(tmp_path / "missing.toml")
    cfg["vault"] = str(tmp_path / "vault")
    cfg["harness"] = {"claude-code": {"root": str(tmp_path / "claude")}}
    cfg["enrich"]["idle_seconds"] = 0
    cfg["enrich"]["refresh_idle_seconds"] = 0
    loom = Loom(cfg, state_dir=tmp_path / "state", plugin_dir=tmp_path / "plugins")
    loom.backend = FakeBackend()
    loom.root = root
    return loom


def note_text(loom, key):
    return loom.vault.file(loom.store.records[key].note).read_text()


def test_sync_writes_linked_notes(loom, tmp_path):
    result = loom.sync()
    assert result["changed"] == 1 and result["enriched"] == 1

    record = loom.store.records["claude-code:aaaa1111"]
    assert record.title == "Fix login redirect"
    assert record.themes == ["authentication", "web frontend"]
    text = note_text(loom, "claude-code:aaaa1111")
    assert "session_id: \"aaaa1111\"" in text
    assert "[[Promptloom/Themes/authentication|authentication]]" in text
    assert "[[Promptloom/Projects/shop|shop]]" in text
    assert "- [ ] Rotate the staging API key ^a-" in text

    folder = tmp_path / "vault" / "Promptloom"
    for name in ("Home", "Action Items", "Links", "Inbox", "Insights",
                 "Themes/authentication", "Projects/shop", "Harnesses/claude-code"):
        assert (folder / f"{name}.md").exists(), name
    assert "https://example.com/bug/1" in (folder / "Links.md").read_text()

    status = json.loads((tmp_path / "state" / "status.json").read_text())
    assert status["recent"][0]["title"] == "Fix login redirect"
    assert status["open_actions"] == 1


def test_user_notes_and_ticked_boxes_survive_a_rewrite(loom):
    loom.sync()
    key = "claude-code:aaaa1111"
    path = loom.vault.file(loom.store.records[key].note)
    text = path.read_text().replace("- [ ] Rotate", "- [x] Rotate") + "my own thoughts\n"
    path.write_text(text)

    with open(loom.root / "aaaa1111.jsonl", "a") as fh:
        fh.write(user_line("aaaa1111", "now it 404s", "/home/me/shop", "2026-01-02T04:00:00.000Z"))
    loom.sync()

    text = path.read_text()
    assert text.split(END)[1].endswith("my own thoughts\n")
    assert "now it 404s" in text
    assert "- [x] Rotate the staging API key" in text
    actions = loom.vault.file(loom.vault.rel("Action Items")).read_text()
    assert "- [x] Rotate the staging API key" in actions

    # Unticking it in the index flows back to the session note.
    index = loom.vault.file(loom.vault.rel("Action Items"))
    index.write_text(index.read_text().replace("- [x] Rotate", "- [ ] Rotate"))
    loom.sync()
    assert "- [ ] Rotate the staging API key" in path.read_text()


def test_capture_attaches_to_the_active_session(loom):
    with open(loom.root / "aaaa1111.jsonl", "a") as fh:
        fh.write(user_line("aaaa1111", "still here", "/home/me/shop", extract.now_ts()))
    loom.sync()
    capture = loom.capture("todo: email Sam the fix")
    assert capture["todo"] and capture["text"] == "email Sam the fix"
    assert capture["session"] == "claude-code:aaaa1111"

    loom.sync()
    inbox = loom.vault.file(loom.vault.rel("Inbox")).read_text()
    assert "- [ ] email Sam the fix" in inbox and "Fix login redirect" in inbox
    assert loom.status()["open_actions"] == 2


def test_insights_link_back_to_sessions(loom):
    for sid, cwd in (("bbbb2222", "/home/me/api"), ("cccc3333", "/home/me/app")):
        (loom.root / f"{sid}.jsonl").write_text(user_line(sid, "add token refresh", cwd))
    loom.sync(enrich_limit=10)
    with loom.store.lock():
        assert loom.write_insights(force=True)
        loom.vault.render()

    text = loom.vault.file(loom.vault.rel("Insights")).read_text()
    assert "### Share the token helper" in text
    assert text.count("[[Promptloom/Sessions/claude-code/") >= 4  # the unknown S99 is dropped
    assert "Reuse the token helper from /home/me/app." in text
    assert " | /home/me/api | " in loom.backend.prompts[-1]  # the model sees directories
    connections = loom.vault.file(loom.vault.rel("Connections")).read_text()
    assert "### Share the token helper" in connections.split("## Projects that connect")[0]


def test_connections_keywords_and_map(loom, tmp_path):
    for sid, cwd, text in (
            ("bbbb2222", "/home/me/api", "the websocket gateway drops redis messages"),
            ("cccc3333", "/home/me/app", "subscribe to the websocket gateway, cache in redis")):
        (loom.root / f"{sid}.jsonl").write_text(user_line(sid, text, cwd))
    loom.sync(enrich_limit=10)

    folder = tmp_path / "vault" / "Promptloom"
    assert any("websocket" in p.name for p in (folder / "Keywords").iterdir())
    note = note_text(loom, "claude-code:bbbb2222")
    assert "- **Keywords:** " in note and "words: " in note.split("## Related sessions")[1]
    connections = (folder / "Connections.md").read_text()
    assert "### Connect app with api" in connections or "### Connect api with app" in connections
    assert "[[Promptloom/Map.canvas|Open the map]]" in connections

    doc = json.loads((folder / "Map.canvas").read_text())
    labels = {n.get("label") for n in doc["nodes"] if n["type"] == "group"}
    assert {"api", "app", "Prompts to try"} <= labels
    assert any("websocket" in e.get("label", "") for e in doc["edges"])
    assert loom.status()["map"].endswith("Promptloom/Map.canvas")


def test_ignored_contexts_are_never_captured(loom):
    loom.cfg["ignore_contexts"] = ["/home/me/shop"]
    loom.sync()
    assert loom.store.records == {}


def test_redaction_and_links():
    text = 'export OPENAI_API_KEY="sk-abcdefghijklmnopqrstuvwxyz" then visit https://a.example/x).'
    assert "sk-abc" not in extract.redact(text)
    assert extract.urls(text) == ["https://a.example/x"]


def test_hosted_provider_round_trip(tmp_path):
    """Key file is private, overrides the config, and the OpenAI-compatible call works."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from promptloom import providers

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers["Authorization"]
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = {"choices": [{"message": {"content": 'Sure: {"ok": true}'}}]}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        chosen = tmp_path / "provider.toml"
        providers.save_file(chosen, {
            "provider": "custom", "model": "tiny-1",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "keys": {"custom": 'k"ey-123'}})
        assert chosen.stat().st_mode & 0o777 == 0o600

        cfg = config.load(tmp_path / "missing.toml", chosen)
        assert cfg["enrich"]["backend"] == "api" and cfg["keys"]["custom"] == 'k"ey-123'
        backend = providers.build(cfg)
        assert backend.complete("hello", {"type": "object"}) == {"ok": True}
        assert seen["auth"] == 'Bearer k"ey-123'
        assert seen["body"]["model"] == "tiny-1"
    finally:
        server.shutdown()


def test_missing_key_leaves_sessions_pending(loom, tmp_path):
    loom.cfg["enrich"].update(backend="api", provider="openai", model="x")
    broken = Loom(loom.cfg, state_dir=tmp_path / "state", plugin_dir=tmp_path / "plugins")
    result = broken.sync()
    assert result["enriched"] == 0
    assert "no API key" in result["status"]["waiting"]
    assert len(broken.pending(force=True)) == 1


def test_obsidian_problem_needs_a_vault_containing_the_notes(tmp_path):
    listing = tmp_path / "obsidian.json"
    vault = tmp_path / "Obsidian"
    assert obsidian_problem(vault, "Promptloom", [listing]) == ""  # no listing: no opinion

    listing.write_text(json.dumps({"vaults": {"a": {"path": str(tmp_path / "Other")}}}))
    problem = obsidian_problem(vault, "Promptloom", [listing])
    assert str(vault) in problem and "Open folder as vault" in problem

    for known in (vault, vault / "Promptloom", tmp_path):
        listing.write_text(json.dumps({"vaults": {"a": {"path": str(known)}}}))
        assert obsidian_problem(vault, "Promptloom", [listing]) == ""
