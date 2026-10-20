import json
import sqlite3

from promptloom.harnesses.claude_code import ClaudeCode
from promptloom.harnesses.codex import Codex
from promptloom.harnesses.generic import GenericJsonl
from promptloom.harnesses.hermes import Hermes
from promptloom.model import Session


class Sessions(dict):
    def get_for(self, harness):
        def get(sid):
            return self.setdefault(sid, Session(harness, sid))
        return get


def write_lines(path, records):
    with open(path, "a") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def claude_user(text, **extra):
    return {"type": "user", "sessionId": "s1", "cwd": "/home/me/proj", "gitBranch": "main",
            "timestamp": "2026-01-02T03:04:05.000Z", "message": {"role": "user", "content": text},
            **extra}


def test_claude_keeps_only_what_the_user_typed(tmp_path):
    path = tmp_path / "s1.jsonl"
    write_lines(path, [
        claude_user("fix the login bug"),
        claude_user("[Image: source: /tmp/x.webp]", isMeta=True),
        claude_user([{"type": "tool_result", "tool_use_id": "t", "content": "output"}]),
        claude_user("<system-reminder>injected</system-reminder>"),
        claude_user("<command-name>/clear</command-name>\n<command-args></command-args>"),
        claude_user([{"type": "text", "text": "and add a test"}, {"type": "image", "source": {}}]),
        claude_user("subagent prompt", isSidechain=True),
        {"type": "custom-title", "sessionId": "s1", "customTitle": "Login bug"},
        {"type": "assistant", "sessionId": "s1", "timestamp": "2026-01-02T03:05:00.000Z",
         "message": {"content": [{"type": "text", "text": "See https://example.com/docs."}]}},
        {"type": "attachment", "sessionId": "s1", "timestamp": "2026-01-02T03:06:00.000Z",
         "attachment": {"type": "queued_command", "prompt": "also bump the version",
                        "origin": {"kind": "human"}}},
    ])
    sessions = Sessions()
    touched = ClaudeCode("claude-code", {}).read(path, {}, sessions.get_for("claude-code"))

    session = sessions["s1"]
    assert touched == {"s1"}
    assert [p.text for p in session.prompts] == [
        "fix the login bug", "and add a test", "also bump the version"]
    assert session.title == "Login bug"
    assert session.context == "/home/me/proj" and session.project == "proj"
    assert session.reply_urls == ["https://example.com/docs"]
    assert session.started == "2026-01-02T03:04:05Z"
    assert session.updated == "2026-01-02T03:06:00Z"


def test_jsonl_is_read_incrementally_and_waits_for_whole_lines(tmp_path):
    path = tmp_path / "s1.jsonl"
    harness, sessions, cursor = ClaudeCode("claude-code", {}), Sessions(), {}
    get = sessions.get_for("claude-code")

    write_lines(path, [claude_user("one")])
    half = json.dumps(claude_user("two"))
    with open(path, "a") as fh:
        fh.write(half[:20])
    harness.read(path, cursor, get)
    assert [p.text for p in sessions["s1"].prompts] == ["one"]

    with open(path, "a") as fh:
        fh.write(half[20:] + "\n")
    harness.read(path, cursor, get)
    harness.read(path, cursor, get)  # nothing new: must not duplicate
    assert [p.text for p in sessions["s1"].prompts] == ["one", "two"]


def test_codex_rollout(tmp_path):
    sid = "0199a1b2-aaaa-bbbb-cccc-0123456789ab"
    path = tmp_path / f"rollout-2026-01-02T03-04-05-{sid}.jsonl"
    write_lines(path, [
        {"timestamp": "2026-01-02T03:04:05Z", "type": "session_meta",
         "payload": {"id": sid, "cwd": "/home/me/api", "git": {"branch": "dev"}}},
        {"timestamp": "2026-01-02T03:04:06Z", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
        {"timestamp": "2026-01-02T03:04:07Z", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "add pagination"}]}},
        {"timestamp": "2026-01-02T03:04:07Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "add pagination"}},
        {"timestamp": "2026-01-02T03:04:09Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Done, see http://localhost:3000"}},
    ])
    sessions = Sessions()
    Codex("codex", {}).read(path, {}, sessions.get_for("codex"))

    session = sessions[sid]
    assert [p.text for p in session.prompts] == ["add pagination"]
    assert session.context == "/home/me/api" and session.branch == "dev"
    assert session.reply_urls == ["http://localhost:3000"]


def test_generic_jsonl_follows_configured_paths(tmp_path):
    path = tmp_path / "chat-42.jsonl"
    write_lines(path, [
        {"msg": {"who": "human", "body": "hello there"}, "meta": {"dir": "/srv/app"}, "t": 1767323045},
        {"msg": {"who": "bot", "body": "hi"}, "t": 1767323050},
    ])
    cfg = {"glob": str(tmp_path / "*.jsonl"), "session_id": "@stem", "role": "msg.who",
           "user_value": "human", "assistant_value": "bot", "text": "msg.body",
           "timestamp": "t", "cwd": "meta.dir", "label": "DeepSeek"}
    harness = GenericJsonl("deepseek", cfg)
    sessions = Sessions()
    assert [p.name for p in harness.sources()] == ["chat-42.jsonl"]
    harness.read(path, {}, sessions.get_for("deepseek"))

    session = sessions["chat-42"]
    assert harness.label == "DeepSeek"
    assert [p.text for p in session.prompts] == ["hello there"]
    assert session.context == "/srv/app"
    assert session.last_reply == "hi"


def test_hermes_database_is_reread_without_duplicating(tmp_path):
    db_path = tmp_path / "state.db"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                               content TEXT, timestamp REAL);
        INSERT INTO sessions VALUES ('h1', 'cli', 'Garden notes', 1767323045);
        INSERT INTO messages VALUES (1, 'h1', 'user', 'when do I plant garlic', 1767323046);
        INSERT INTO messages VALUES (2, 'h1', 'assistant', 'In autumn.', 1767323047);
        INSERT INTO messages VALUES (3, 'h1', 'tool', 'ignored', 1767323048);
    """)
    db.commit()
    db.close()
    harness = Hermes("hermes", {"db": str(db_path)})
    sessions = Sessions()
    get = sessions.get_for("hermes")
    harness.read(db_path, {}, get)
    harness.read(db_path, {}, get)

    session = sessions["h1"]
    assert [p.text for p in session.prompts] == ["when do I plant garlic"]
    assert session.title == "Garden notes" and session.context == "hermes-cli"
