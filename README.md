# promptloom

Keeps every prompt you type into AI coding tools (Claude Code, Codex, Copilot in
VS Code, Hermes, and any other you point it at) and lets you search them, tag
them, write notes beside them, and see how your projects connect and came
together over time.

![Search across every tool](docs/search.png)

It runs quietly in the background, reading the session logs those tools
already keep. Nothing is sent anywhere unless you choose a hosted model for
summaries. It runs on Linux and macOS; Windows is planned (the core already
runs there, the tray icon and service don't yet).

## What you get

`promptloom ui` opens the app in your browser. It is served from your own
machine and works offline.

- **Search** every prompt from every tool, newest first. Filters go in the
  query: `model:opus project:shop harness:codex tag:idea theme:auth
  branch:main after:2026-01-01 before:2026-02-01 "exact phrase"`.
- **Tags on every prompt**: the model that answered it, the tool, the project
  and the git branch are added automatically. Add your own to a session or to
  a single prompt, and search by them.
- **Notes** of your own, in Markdown, attached to a project or a session. They
  show up in search and on the graph.
- **Projects** shows each project's activity day by day, coloured by model, and
  every point where the work moved from one model to another.
- **Models** shows which models worked on which projects, the projects several
  models shared, and the most common hand-offs.
- **Graph** joins projects through the models, tools, themes, tags and unusual
  words they have in common. Dashed purple lines are links no theme explains:
  the ones you and the AI are least likely to have noticed.
- **Connections** turns the strongest links between projects into ready-to-paste
  prompts that ask an assistant how to connect them.

| | |
| --- | --- |
| ![Project timeline](docs/project-trailmap.png) | ![Models](docs/models.png) |
| ![Graph](docs/graph.png) | ![Connections](docs/connections.png) |

The screenshots use made-up projects from `scripts/demo.py`.

A menu bar icon (a GNOME Shell extension on Linux, a small native app on macOS)
shows the session you are in, your recent sessions and a quick-capture box.

## Supported tools

| Tool | How | Model per prompt |
| --- | --- | --- |
| Claude Code | `~/.claude/projects` | yes |
| Codex (CLI and desktop) | `~/.codex/sessions` | yes |
| GitHub Copilot Chat in VS Code | VS Code's `workspaceStorage/*/chatSessions` | yes |
| Hermes | `~/.hermes/state.db` | when recorded |
| Anything that writes JSON lines | a few lines of config | if it logs one |
| Anything else | `promptloom log`, the HTTP API, or a Python plugin | if you pass it |

Claude Code and Codex have been run against real logs. The Copilot and Hermes
adapters follow those tools' storage formats and are tested on synthetic files;
reports from real installs are welcome.

### Sending prompts from other tools

From a script, a git hook or an editor task:

```sh
promptloom log --harness aider --model gpt-6 "make the importer resumable"
```

Or over HTTP, while the daemon is running:

```sh
curl -X POST http://127.0.0.1:8765/api/ingest -H 'Content-Type: application/json' \
  -d '{"harness": "aider", "context": "/path/to/project", "prompt": "…", "model": "gpt-6"}'
```

`context` is the project directory. Add `session` to group prompts into one
session; without it, prompts from the same tool and directory on the same day
are grouped together. A list of such objects is accepted too.

### Letting assistants read your history (MCP)

`promptloom mcp` is a Model Context Protocol server, so an assistant can search
what you asked before, look up how a project came together, or find related
work in your other projects.

```sh
claude mcp add promptloom -- promptloom mcp          # Claude Code
```

```toml
# ~/.codex/config.toml (Codex)
[mcp_servers.promptloom]
command = "promptloom"
args = ["mcp"]
```

```json
// .vscode/mcp.json (Copilot in VS Code)
{ "servers": { "promptloom": { "type": "stdio", "command": "promptloom", "args": ["mcp"] } } }
```

Its tools are `search_prompts`, `project_history`, `related_work`,
`list_projects` and `capture_note`. It reads your history and can add to your
inbox; it changes nothing else.

## Install

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or pipx).

```sh
git clone https://github.com/craigcampbell/promptloom
cd promptloom
./install.sh
```

**Linux** installs the `promptloom` command, a systemd user service, and the
GNOME top bar extension (skipped without GNOME). Nothing needs sudo. On Wayland
a new extension only appears after you log out and back in.

**macOS** installs the command, a launchd agent, and builds the menu bar app
into `~/Applications` (needs the Xcode Command Line Tools: `xcode-select
--install`). See [macos/README.md](macos/README.md).

**Elsewhere**, `uv tool install .` and run `promptloom watch` however you like
to keep things running.

`./uninstall.sh` removes the service, the menu bar item and the command, and
leaves your captured history and config alone.

To try the app without your own history:

```sh
python scripts/demo.py ~/promptloom-demo   # prints the command to open it
```

## Commands

```
promptloom ui [QUERY]          open the app (search for QUERY)
promptloom search WORDS        search from the terminal (--json for scripts)
promptloom status              active and recent sessions
promptloom capture TEXT        save a note against the active session
promptloom capture --todo TEXT save an action item (or start TEXT with "todo:")
promptloom log TEXT            record a prompt from any tool (see above)
promptloom mcp                 MCP server on stdio
promptloom harnesses           what is configured and what was found
promptloom sync                capture once (the daemon does this every 5 s)
promptloom sync --all          also summarise every pending session now
promptloom insights            ask the model what connects your sessions, now
promptloom provider ...        use a hosted model for summaries (below)
promptloom watch               run continuously (what the service runs)
```

Logs: `journalctl --user -u promptloom -f` on Linux,
`~/Library/Logs/promptloom.log` on macOS.

## How connections are found

Themes come from the model and only connect what it thought to group. To find
the rest, promptloom compares the words and two-word phrases of every session's
prompts, weighting the ones few sessions use (TF-IDF). Sessions in different
projects that share such words are linked, and the words are listed as the
reason. This needs no model, so it works for every session.

- **Shared words** are words that turn up in more than one project.
- **Hidden links** join projects that share unusual words but no theme.
- **Prompts to try** come first from links between two real projects (a
  directory with `.git`, a README or a package manifest, as opposed to `~` or
  `~/Downloads`), then from the model's insights once it has run.

Model hand-offs are worked out from the model recorded with each prompt: each
time consecutive prompts in a project were answered by different models.

## Summaries

A model can add a title, summary, themes and action items to each session. By
default that is a local Ollama model (`qwen3.8:27b`), so prompts never leave the
machine. A session gets its first summary once it has been quiet for a minute,
and a refreshed one after it has grown and then sat idle for 15 minutes.

Loading that model takes about 17 GB of VRAM. If less than
`min_free_vram_mib` is free promptloom runs it on CPU instead, which takes a
minute or two per session. Set `gpu_busy = "wait"` to defer instead, or
`backend = "none"` to turn summaries off; you still get capture, search, tags,
notes, the graph and timelines.

### Using a hosted provider with your own API key

```sh
promptloom provider set openrouter --model <model>   # prompts for the key, hidden
promptloom provider set anthropic                    # model defaults to claude-opus-5-5
promptloom provider set openai                       # no --model: lists the provider's models
promptloom provider set custom --base-url https://host/v1 --model <model>
promptloom provider                                  # what is active, which keys are stored
promptloom provider local                            # back to the local model; keys kept
promptloom provider forget openrouter                # delete a stored key
```

Built in: `anthropic`, `openai`, `deepseek`, `openrouter`, `gemini`, `groq`,
`mistral`, `xai`, `together`, and `custom` for any OpenAI-compatible API.
`set` makes one small test request and saves nothing if the key or model is
rejected. Keys live in `~/.config/promptloom/provider.toml`, readable only by
you.

A hosted provider is much faster than the CPU fallback, but your prompts from
every tool are sent to it to be summarised, and each summary is billed to your
key. Pick a small model: a summary is a few thousand tokens in and a couple of
hundred out.

## Obsidian (optional)

promptloom can also write everything into an Obsidian vault: a note per
session, hub notes per project, theme, tool and shared word, a canvas map,
action items, links and insights. Set the vault in
`~/.config/promptloom/config.toml`:

```toml
vault = "~/Obsidian"
folder = "Promptloom"   # the folder inside the vault promptloom owns
```

Then run `promptloom init` and open the vault folder in Obsidian once with
"Open folder as vault". Write below the generated block in any note and it is
kept; tick action items in Obsidian and promptloom picks that up.

## Adding a harness

**A harness that logs JSON lines** needs only config. Point the generic adapter
at its files and say where each field lives:

```toml
[harness.deepseek]
type = "jsonl"
label = "DeepSeek"
glob = "~/.deepseek/sessions/*.jsonl"
session_id = "@stem"          # the file name; or a dotted path into each line
role = "role"
user_value = "user"
assistant_value = "assistant"
text = "content"              # a string or a list of {"text": ...} blocks
timestamp = "timestamp"       # ISO string or epoch seconds/milliseconds
cwd = "cwd"
title = "title"
model = "model"
resume = "deepseek --resume {id}"
```

**Anything else** is a small Python class in
`~/.config/promptloom/harnesses/<name>.py`:

```python
from pathlib import Path
from promptloom.harnesses import JsonlHarness

class MyTool(JsonlHarness):
    type = "mytool"            # harness name
    label = "My Tool"
    resume = "mytool resume {id}"

    def sources(self):
        return Path("~/.mytool/logs").expanduser().glob("*.jsonl")

    def record(self, rec, path, cursor, get):
        if rec.get("kind") != "prompt":
            return None
        session = get(rec["session"])          # created on first use
        session.context = rec.get("dir", "")
        session.set_model(rec.get("model", ""))
        session.add_prompt(rec.get("time", ""), rec["text"])
        return session.session_id
```

Subclass `Harness` instead and implement `read()` for storage that is not
line-delimited JSON (see `promptloom/harnesses/hermes.py` for SQLite and
`copilot.py` for whole JSON documents). The daemon picks up new plugins and
config changes without a restart. Pull requests adding built-in adapters are
welcome.

## Privacy

Everything stays on your machine: prompts in
`~/.local/state/promptloom/state.json`, your notes and tags in `user.json`
beside it. The app listens on 127.0.0.1 only, refuses requests addressed to any
other host name, and refuses writes from other web pages.

Values that look like credentials (`sk-…` keys, GitHub and AWS tokens,
`PASSWORD=…` style assignments, private keys) are shown as `[redacted secret]`
in the app and the vault, but that is a pattern match, not a guarantee: a bare
random token you pasted will be kept. `ignore_contexts` in the config excludes
whole directories.

## Development

```sh
uv run --with pytest pytest
python scripts/demo.py /tmp/promptloom-demo
```

The app is plain HTML, CSS and JavaScript in `promptloom/ui` with no build
step; the server is `promptloom/server.py` (standard library only). The menu
bar items only read `~/.local/state/promptloom/status.json` and call
`promptloom capture`, so the daemon can be developed without touching them.

## License

MIT. See [LICENSE](LICENSE).
