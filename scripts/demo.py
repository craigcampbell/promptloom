"""Write made-up sessions for a few imaginary projects, then sync them into a
throwaway state directory, so you can try the app without your own history:

    python scripts/demo.py /tmp/promptloom-demo
    XDG_STATE_HOME=/tmp/promptloom-demo/state XDG_CONFIG_HOME=/tmp/promptloom-demo/config \\
        promptloom ui

The sessions are fictional. They exist to show shared words, model changes and tags.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)

# (day offset, harness, model, project, prompts)
SESSIONS = [
    (0, "claude", "claude-sonnet-5-5", "trailmap", [
        "Start a SvelteKit app that shows hiking trails on vector map tiles",
        "Cache the map tiles in IndexedDB so the app works offline on the trail",
        "Add a GPX import that draws the route over the tiles"]),
    (1, "codex", "gpt-6.1-sol", "pantry", [
        "Create a FastAPI service for a pantry inventory with SQLite",
        "Add barcode lookup and expiry dates for each item"]),
    (2, "claude", "claude-opus-5-5", "trailmap", [
        "The offline sync conflicts when two phones edit the same trail note. Design a CRDT-free merge",
        "Write tests for the merge with clock skew between devices"]),
    (4, "copilot", "gpt-5.2", "pantry", [
        "Why does the expiry date query ignore the timezone?",
        "Refactor the barcode lookup into its own module"]),
    (5, "claude", "claude-opus-5-5", "ledger-cli", [
        "Build a Rust CLI that imports bank CSV exports into a local SQLite ledger",
        "Detect duplicate transactions across overlapping CSV exports"]),
    (7, "claude", "claude-haiku-5-5", "pantry", [
        "Pantry app needs offline sync for the shopping list on my phone",
        "Reuse a last-writer-wins merge, keyed by item id"]),
    (9, "codex", "gpt-6.1-sol", "ledger-cli", [
        "Categorise transactions with rules first, then a small local model for the rest",
        "Add a monthly budget report as Markdown"]),
    (10, "claude", "claude-sonnet-5-5", "trailmap", [
        "Elevation profile chart under the map, with hover that moves a marker on the route"]),
    (12, "copilot", "claude-sonnet-5-5", "homeboard", [
        "Wall dashboard for a Raspberry Pi: weather, calendar and the pantry shopping list",
        "Pull the shopping list from the pantry FastAPI service"]),
    (13, "claude", "claude-opus-5-5", "homeboard", [
        "The dashboard should keep working offline and sync when the wifi comes back",
        "Show this month's budget from ledger-cli as a small bar chart"]),
    (15, "claude", "claude-fable-5-1", "ledger-cli", [
        "Encrypt the SQLite ledger at rest with a key from the OS keychain",
        "Export a read-only JSON feed of the monthly budget for other apps"]),
    (17, "codex", "gpt-6.1-sol", "trailmap", [
        "Package the offline map tiles per region so downloads stay small"]),
    (18, "claude", "claude-fable-5-1", "pantry", [
        "Move the pantry SQLite schema to migrations and add a JSON feed of the shopping list"]),
]


def ts(day: int, minute: int) -> str:
    return (START + timedelta(days=day, minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main(out: Path) -> None:
    for n, (day, harness, model, project, prompts) in enumerate(SESSIONS):
        cwd = f"/home/demo/code/{project}"  # never created; only the name matters here
        sid = f"0000{n:04d}-demo-4000-8000-{project[:12]:0<12}"
        if harness == "claude":
            path = out / "claude" / project / f"{sid}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as fh:
                for i, text in enumerate(prompts):
                    for kind, body in (("user", text), ("assistant", "Done.")):
                        message = {"role": kind, "content": body}
                        if kind == "assistant":
                            message["model"] = model
                        fh.write(json.dumps({"type": kind, "sessionId": sid, "cwd": cwd,
                                             "gitBranch": "main", "message": message,
                                             "timestamp": ts(day, i * 20 + (kind == "assistant"))})
                                 + "\n")
        elif harness == "codex":
            path = out / "codex" / f"rollout-{sid}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "session_meta", "timestamp": ts(day, 0),
                                     "payload": {"id": sid, "cwd": cwd}}) + "\n")
                for i, text in enumerate(prompts):
                    fh.write(json.dumps({"type": "turn_context", "timestamp": ts(day, i * 20),
                                         "payload": {"cwd": cwd, "model": model}}) + "\n")
                    fh.write(json.dumps({"type": "event_msg", "timestamp": ts(day, i * 20),
                                         "payload": {"type": "user_message", "message": text}})
                             + "\n")
        else:
            ws = out / "vscode" / f"ws{n}"
            (ws / "chatSessions").mkdir(parents=True, exist_ok=True)
            (ws / "workspace.json").write_text(json.dumps({"folder": "file://" + cwd}))
            when = START + timedelta(days=day)
            requests = [{"message": {"text": text}, "modelId": f"copilot/{model}",
                         "timestamp": int((when + timedelta(minutes=i * 20)).timestamp() * 1000),
                         "response": [{"value": "Here is how."}]}
                        for i, text in enumerate(prompts)]
            (ws / "chatSessions" / f"{sid}.json").write_text(
                json.dumps({"sessionId": sid, "requests": requests}))

    config_dir = out / "config" / "promptloom"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(f"""\
[enrich]
backend = "none"

[ui]
port = 8766

[harness.claude-code]
root = "{out / 'claude'}"
[harness.codex]
root = "{out / 'codex'}"
[harness.copilot]
root = "{out / 'vscode'}"
[harness.hermes]
enabled = false
""")
    env = dict(os.environ, XDG_STATE_HOME=str(out / "state"), XDG_CONFIG_HOME=str(out / "config"))
    subprocess.run([sys.executable, "-m", "promptloom", "sync"], env=env, check=True)
    print(f"\nXDG_STATE_HOME={out / 'state'} XDG_CONFIG_HOME={out / 'config'} promptloom ui")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/promptloom-demo").resolve())
