"""Starting a prompt in a tool, weaving drafts, and the tools overview."""

import json
import os
import stat
import threading
import urllib.request
from pathlib import Path

import pytest

from promptloom import analysis, config, desktop, launch, server
from promptloom.model import Session
from promptloom.store import Record
from promptloom.userdata import UserData


def record(harness, context, *models):
    session = Session(harness, f"s-{harness}-{len(models)}", context=context)
    for i, model in enumerate(models):
        session.set_model(model)
        session.add_prompt(f"2026-01-0{i + 1}T00:00:00Z", f"prompt {i}")
    return Record(session)


@pytest.fixture
def fake_tool(tmp_path):
    """A program that records how it was called."""
    log = tmp_path / "calls.txt"
    program = tmp_path / "fake-tool"
    program.write_text('#!/bin/sh\n{ pwd; for a in "$@"; do printf \'%s\\n\' "$a"; done; } > '
                       + repr(str(log)) + "\n")
    program.chmod(program.stat().st_mode | stat.S_IEXEC)
    return program, log


def cfg_with(tmp_path, program, terminal=None):
    cfg = config.load(tmp_path / "missing.toml")
    cfg["tools"] = {"agents": {
        "claude": {"cmd": [str(program), "{prompt}"]},
        "hermes": {"cmd": [str(program), "chat", "--query-file", "{prompt_file}"]},
        "mine": {"label": "Mine", "harness": "mine", "cmd": [str(program), "--go", "{prompt}"],
                 "model": ["-m", "{model}"], "models": ["x1"]},
        "editor": {"label": "Editor", "harness": "ed", "gui": True, "cmd": [str(program), "{dir}"]},
    }}
    # "terminal" here just runs the script, with no window.
    cfg["terminal"] = terminal or ["sh", "-c", 'exec "$@"', "sh"]
    return cfg


def test_tools_know_what_is_installed_and_which_models_were_used(tmp_path, fake_tool,
                                                                monkeypatch):
    program, _ = fake_tool
    cfg = cfg_with(tmp_path, program)
    monkeypatch.setattr(launch, "which", lambda p: p if p == str(program) else "")
    records = [record("claude-code", "/w/a", "claude-opus-5-5", "claude-opus-5-5",
                      "claude-haiku-5-5"), record("mine", "/w/b", "x9")]
    tools = {t["name"]: t for t in launch.tools(cfg, records)}
    assert tools["claude"]["available"] and tools["mine"]["available"]
    assert not tools["codex"]["available"] and not tools["pi"]["available"]
    assert tools["claude"]["models"][:2] == ["claude-opus-5-5", "claude-haiku-5-5"]  # seen first
    assert "opus" in tools["claude"]["models"] and tools["claude"]["takes_model"]
    assert tools["mine"]["models"] == ["x9", "x1"] and tools["editor"]["gui"]
    with pytest.raises(launch.LaunchError, match="not installed"):
        launch.tool(cfg, "codex")


def test_command_splices_the_model_after_the_program():
    spec = {"cmd": ["codex", "{prompt}"], "model": ["-m", "{model}"]}
    assert launch.command(spec, "hi", "o5", "/w", "/f") == ["codex", "-m", "o5", "hi"]
    assert launch.command(spec, "hi", "", "/w", "/f") == ["codex", "hi"]
    spec = {"cmd": ["hermes", "chat", "--query-file", "{prompt_file}"], "model": ["-m", "{model}"]}
    assert launch.command(spec, "hi", "m", "/w", "/f") == [
        "hermes", "-m", "m", "chat", "--query-file", "/f"]


def test_start_runs_the_tool_in_the_project_with_the_prompt_intact(tmp_path, fake_tool):
    program, log = fake_tool
    cfg = cfg_with(tmp_path, program)
    project = tmp_path / "proj"
    project.mkdir()
    prompt = "it's `rm -rf /` $(echo boom) \"quoted\"\nline two"

    done = launch.start(cfg, tmp_path / "state", "mine", prompt, "x1", str(project))
    assert done == {"tool": "Mine", "dir": str(project), "how": "terminal"}
    for _ in range(50):
        if log.exists() and log.read_text().count("\n") >= 5:
            break
        threading.Event().wait(0.05)
    lines = log.read_text().split("\n")
    assert lines[0] == str(project) and lines[1:4] == ["-m", "x1", "--go"]
    assert "\n".join(lines[4:6]) == prompt

    done = launch.start(cfg, tmp_path / "state", "hermes", "hello", "", str(project))
    for _ in range(50):
        if log.exists() and "--query-file" in log.read_text():
            break
        threading.Event().wait(0.05)
    lines = log.read_text().split("\n")
    assert lines[1:3] == ["chat", "--query-file"] and open(lines[3]).read() == "hello"

    with pytest.raises(launch.LaunchError, match="empty"):
        launch.start(cfg, tmp_path / "state", "mine", "  ", "", str(project))
    with pytest.raises(launch.LaunchError, match="not a folder"):
        launch.start(cfg, tmp_path / "state", "mine", "x", "", str(project / "nope"))


def test_gui_tool_gets_the_folder_and_the_clipboard(tmp_path, fake_tool, monkeypatch):
    program, log = fake_tool
    cfg = cfg_with(tmp_path, program)
    copied = []
    monkeypatch.setattr(launch, "copy_to_clipboard", lambda text: copied.append(text) or True)
    done = launch.start(cfg, tmp_path / "state", "editor", "open sesame", "", str(tmp_path))
    assert done["how"] == "gui" and done["copied"] and copied == ["open sesame"]
    for _ in range(50):
        if log.exists():
            break
        threading.Event().wait(0.05)
    assert log.read_text().split("\n")[1] == str(tmp_path)


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.prompts = []

    def ready(self):
        return ""

    def complete(self, prompt, schema):
        self.prompts.append(prompt)
        return {"prompt": "Do the thing, then run the tests.", "notes": "Added a check."}


def test_weave_keeps_both_versions(tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(launch, "weaver", lambda cfg, choice, workdir: backend)
    woven = launch.weave({}, str(tmp_path), "fake", "do the thing", "Claude Code", "shop",
                         ["Fix login: made the redirect stop looping"])
    assert woven == {"prompt": "Do the thing, then run the tests.", "notes": "Added a check."}
    sent = backend.prompts[0]
    assert "(Claude Code) in the project shop" in sent
    assert "- Fix login: made the redirect stop looping" in sent
    assert sent.endswith("The prompt:\n\ndo the thing")
    with pytest.raises(launch.LaunchError, match="nothing to weave"):
        launch.weave({}, str(tmp_path), "fake", "  ")

    user = UserData(tmp_path)
    with user.editing() as u:
        draft = u.put_draft({"body": "do the thing", "tool": "claude", "context": "/w/shop"})
        assert draft["title"] == "do the thing" and not draft["sent"]
        u.weave_draft(draft["id"], woven["prompt"], "qwen (local)", woven["notes"])
        u.mark_sent(draft["id"], "Claude Code")
    again = UserData(tmp_path)
    again.load()
    [saved] = again.drafts
    assert saved["body"] == "Do the thing, then run the tests."
    [version] = saved["versions"]
    assert version["before"] == "do the thing" and version["by"] == "qwen (local)"
    assert saved["sent_with"] == "Claude Code"
    with again.editing() as u:
        assert u.delete_draft(saved["id"]) and not u.delete_draft("nope")


def test_weavers_come_from_ollama_and_stored_keys(monkeypatch):
    monkeypatch.setattr(launch.enrich.Ollama, "_call",
                        lambda self, path, body=None, timeout=5: {"models": [{"name": "qwen3"}]})
    cfg = {"enrich": {"backend": "api", "url": "http://x", "model": "o5", "provider": "openai"},
           "keys": {"openai": "k", "anthropic": "k2", "groq": ""}}
    found = launch.weavers(cfg)
    assert [w["id"] for w in found] == ["ollama:qwen3", "api:openai:o5", "api:openai:gpt-5.5-mini",
                                        "api:anthropic:claude-opus-5-5",
                                        "api:anthropic:claude-haiku-5-5"]
    assert [w["default"] for w in found] == [False, True, False, False, False]
    assert found[2]["label"].endswith("(cheap)")


def test_tools_overview():
    records = [record("claude-code", "/w/a", "m1", "m1"), record("claude-code", "/w/b", "m2"),
               record("codex", "/w/a", "m3")]
    out = analysis.tools_overview(records, {"claude-code": "Claude Code"})
    assert [t["label"] for t in out] == ["Claude Code", "codex"]
    assert out[0]["sessions"] == 2 and out[0]["prompts"] == 3
    assert out[0]["models"] == {"m1": 2, "m2": 1} and out[0]["projects"] == {"a": 2, "b": 1}


@pytest.fixture
def api(tmp_path, fake_tool):
    program, log = fake_tool
    cfg = cfg_with(tmp_path, program)
    cfg["harness"] = {}
    httpd = server.serve(cfg, tmp_path / "state", port=0, plugin_dir=tmp_path / "none")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", log
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())


def test_http_api_for_drafts_and_launching(api, tmp_path):
    base, log = api
    tools = call(base + "/api/tools")
    assert {t["name"] for t in tools["tools"] if t["available"]} >= {"claude", "mine", "editor"}
    draft = call(base + "/api/drafts", {"body": "ship it", "tool": "mine", "context": str(tmp_path)})
    assert call(base + "/api/draft?id=" + draft["id"])["title"] == "ship it"
    assert call(base + "/api/overview")["drafts"] == 1
    done = call(base + "/api/launch", {"tool": "mine", "prompt": "ship it", "model": "x1",
                                       "context": str(tmp_path), "draft": draft["id"]})
    assert done["tool"] == "Mine"
    assert call(base + "/api/drafts")[0]["sent_with"] == "Mine"
    assert call(base + "/api/overview")["drafts"] == 0
    with pytest.raises(urllib.error.HTTPError) as err:
        call(base + "/api/launch", {"tool": "codex", "prompt": "x"})
    assert err.value.code == 400
    assert call(base + "/api/drafts", {"delete": draft["id"]}) == {"deleted": draft["id"]}


def test_desktop_entry_names_the_app_window():
    text = desktop.desktop_entry(8765)
    assert "StartupWMClass=chrome-127.0.0.1__-Default" in text
    assert "Exec=" in text and "app --at compose" in text


def test_web_and_desktop_tools_take_the_prompt_in_a_url(monkeypatch):
    monkeypatch.setattr(launch, "which", lambda p: "/bin/" + p if p == "chatgpt" else "")
    monkeypatch.setattr(launch.Path, "exists", lambda self: False)
    tools = {t["name"]: t for t in launch.tools({})}
    assert tools["chatgpt"]["kind"] == "web" and tools["chatgpt"]["available"]
    assert tools["chatgpt-desktop"]["kind"] == "app" and tools["chatgpt-desktop"]["available"]
    assert not tools["claude-desktop"]["available"] and tools["claude-web"]["available"]
    assert tools["chatgpt"]["takes_model"] and not tools["claude-web"]["takes_model"]
    assert tools["codex-web"]["clipboard"]

    url, clip = launch.url_for(launch.TOOLS["chatgpt"], "fix the bug & test", "gpt-5.5")
    assert url == "https://chatgpt.com/?q=fix+the+bug+%26+test&model=gpt-5.5" and not clip
    assert launch.url_for(launch.TOOLS["claude-web"], "hi", "")[0] == "https://claude.ai/new?q=hi"
    assert launch.url_for(launch.TOOLS["codex-web"], "hi", "") == ("https://chatgpt.com/codex", True)
    long_url, clip = launch.url_for(launch.TOOLS["claude-web"], "x" * 3000, "")
    assert long_url == "https://claude.ai/new" and clip

    opened, copied = [], []
    monkeypatch.setattr(launch, "open_uri", lambda u: opened.append(u))
    monkeypatch.setattr(launch, "copy_to_clipboard", lambda t: copied.append(t) or True)
    done = launch.start({}, Path("/tmp"), "claude-web", "hello", "", "/tmp")
    assert done["how"] == "url" and opened == ["https://claude.ai/new?q=hello"] and not copied
    done = launch.start({}, Path("/tmp"), "codex-web", "hello", "", "/tmp")
    assert done["via_clipboard"] and copied == ["hello"]


def test_hints_from_a_cheap_model_are_short_lists(tmp_path, monkeypatch):
    class Backend:
        def ready(self):
            return ""

        def complete(self, prompt, schema):
            assert "What they have typed so far" in prompt and "Known about" not in prompt
            return {"decide": ["Which pages?", 3, "  Persist how? "], "add": [], "watch": "no"}

    monkeypatch.setattr(launch, "weaver", lambda cfg, choice, workdir: Backend())
    out = launch.hints({}, str(tmp_path), "fake", "add a dark mode toggle", "Claude Code", "shop")
    assert out == {"decide": ["Which pages?", "Persist how?"], "add": [], "watch": []}
    assert launch.hints({}, str(tmp_path), "fake", "hi") == {"decide": [], "add": [], "watch": []}
