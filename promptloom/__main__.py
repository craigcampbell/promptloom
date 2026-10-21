"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import quote

from . import __version__, brief, config, desktop, extract, gitlocal, launch, prs
from .engine import Loom
from .store import Store
from .vault import obsidian_problem

# Graph view colours so sessions, projects, themes and harnesses read apart.
GRAPH_GROUPS = [("Themes", 0xE5A50A), ("Projects", 0x33D17A), ("Harnesses", 0xC061CB),
                ("Sessions", 0x62A0EA), ("Keywords", 0xF66151)]


open_uri = desktop.open_uri


def cmd_init(args) -> int:
    created = config.write_default()
    cfg = config.load()
    print(f"config: {config.CONFIG_FILE}" + (" (created)" if created else ""))
    if not cfg["vault"]:
        print("Obsidian output is off; `promptloom ui` opens the built-in app. "
              "Set vault in the config to also write an Obsidian vault.")
        return 0
    vault = Path(cfg["vault"])
    (vault / cfg["folder"]).mkdir(parents=True, exist_ok=True)
    graph = vault / ".obsidian" / "graph.json"
    if not graph.exists():
        graph.parent.mkdir(exist_ok=True)
        groups = [{"query": f'path:"{cfg["folder"]}/{name}"', "color": {"a": 1, "rgb": rgb}}
                  for name, rgb in GRAPH_GROUPS]
        graph.write_text(json.dumps({"colorGroups": groups, "showTags": False}, indent=2))
    print(f"vault:  {vault}")
    if obsidian_problem(vault, cfg["folder"]):
        print("Open that folder in Obsidian once (Open folder as vault) so links from the top bar work.")
    return 0


def cmd_sync(args) -> int:
    loom = Loom()
    if args.reenrich:
        with loom.store.lock():
            loom.store.load()
            for record in loom.store.records.values():
                record.enrich = {}
                record.retry_at = 0
            loom.store.save()
    if args.no_enrich:
        loom.backend = None
        limit = 0
    else:
        limit = 10**6 if (args.all or args.reenrich) else 1

    def progress(record):
        print(f"  summarised: {record.title}  [{', '.join(record.themes)}]", flush=True)

    result = loom.sync(enrich_limit=limit, force_enrich=limit > 1, progress=progress)
    status = result["status"]
    print(f"{status['sessions']} sessions, {result['changed']} changed, "
          f"{result['enriched']} summarised, {status['summarising']} waiting for a summary")
    if status["waiting"]:
        print(f"summarising paused: {status['waiting']}")
    for error in loom.errors:
        print(f"warning: {error}", file=sys.stderr)
    return 0


def cmd_watch(args) -> int:
    from . import server

    loom = Loom()
    stamp = _config_stamp()
    print(f"promptloom {__version__} watching: "
          f"{', '.join(h.name for h in loom.harnesses) or 'no harnesses'}", flush=True)
    httpd = None
    if loom.cfg.get("ui", {}).get("enabled", True):
        httpd = server.start_thread(loom.cfg)
        print(f"app: {loom.ui_url()}" if httpd else
              f"app: port {loom.cfg['ui'].get('port', 8765)} is taken; not serving", flush=True)
    for error in loom.errors:
        print(f"warning: {error}", flush=True)
    while True:
        try:
            if _config_stamp() != stamp:
                stamp = _config_stamp()
                loom = Loom()
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                    httpd = server.start_thread(loom.cfg)
                print("configuration reloaded", flush=True)
            result = loom.sync(enrich_limit=1)
            if result["enriched"] or result["insights"]:
                print(f"summarised {result['enriched']}, insights={result['insights']}", flush=True)
        except Exception:
            traceback.print_exc()
        try:
            prs.tick(loom.cfg, loom.store.dir, loom.store.by_recency())
        except Exception:  # GitHub being away must not stop the capture
            traceback.print_exc()
        time.sleep(loom.cfg.get("poll_seconds", 5))


def _config_stamp() -> list:
    stamps = []
    paths = [config.CONFIG_FILE, config.PROVIDER_FILE, config.SETTINGS_FILE]
    if config.PLUGIN_DIR.is_dir():
        paths += sorted(config.PLUGIN_DIR.glob("*.py"))
    for path in paths:
        try:
            stamps.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            pass
    return stamps


def cmd_status(args) -> int:
    loom = Loom()
    status = loom.read_status()
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    if not status:
        print("No status yet. Run `promptloom sync` or start the daemon.")
        return 1
    active = status.get("active")
    print(f"vault:    {status['vault']}")
    print(f"sessions: {status['sessions']}   open action items: {status['open_actions']}   "
          f"awaiting summary: {status['summarising']}")
    if status.get("waiting"):
        print(f"paused:   {status['waiting']}")
    if status.get("vault_problem"):
        print(f"obsidian: {status['vault_problem']}")
    if active:
        print(f"active:   {active['title']}  ({active['harness_label']}, {active['project']}, "
              f"{active['session_id']})")
    print("recent:")
    for entry in status.get("recent", []):
        print(f"  {extract.local(entry['updated'])}  {entry['harness_label']:<12} "
              f"{entry['project']:<22} {entry['title']}")
    return 0


def cmd_capture(args) -> int:
    text = " ".join(args.text).strip()
    if not text:
        text = sys.stdin.read().strip()
    if not text:
        print("nothing to capture", file=sys.stderr)
        return 1
    capture = Loom().capture(text, todo=args.todo, context=args.context or "")
    print(("todo: " if capture["todo"] else "captured: ") + capture["text"])
    return 0


def cmd_insights(args) -> int:
    loom = Loom()
    with loom.store.lock():
        loom.store.load()
        if loom.write_insights(force=True):
            loom.vault.render()
            loom.store.save()
            print(f"wrote {loom.vault.file(loom.vault.rel('Insights'))}")
            return 0
    print(loom.waiting or "Not enough summarised sessions yet (or no model backend).")
    return 1


def cmd_harnesses(args) -> int:
    loom = Loom()
    loom.store.load()
    counts: dict[str, int] = {}
    for record in loom.store.records.values():
        if record.session.prompts:
            counts[record.session.harness] = counts.get(record.session.harness, 0) + 1
    for harness in loom.harnesses:
        try:
            files = len(list(harness.sources()))
        except Exception as exc:
            files = f"error: {exc}"
        found = f"{files} source files" if files else "not found on this machine"
        print(f"{harness.name:<14} {harness.label:<14} {found}, "
              f"{counts.get(harness.name, 0)} sessions captured")
    for error in loom.errors:
        print(f"warning: {error}", file=sys.stderr)
    return 0


def cmd_provider(args) -> int:
    from . import providers
    from .enrich import EnrichError

    stored = providers.load_file(config.PROVIDER_FILE)
    keys = stored.setdefault("keys", {})
    action = args.action or "show"

    if action == "show":
        cfg = config.load()["enrich"]
        if cfg["backend"] == "api":
            name = cfg["provider"]
            print(f"summaries: {providers.label(name)} ({name}), model {cfg.get('model') or 'default'}")
        else:
            print(f"summaries: {cfg['backend']} (local settings in {config.CONFIG_FILE})")
        for name in providers.names():
            note = f"key {providers.mask(keys[name])}" if name in keys else "no key"
            print(f"  {name:<11} {providers.label(name):<14} {note}")
        print("Set one with: promptloom provider set NAME [--model MODEL]")
        return 0

    if action == "local":
        for key in ("provider", "model", "base_url"):
            stored.pop(key, None)
        providers.save_file(config.PROVIDER_FILE, stored)
        print("Summaries are back on the backend in config.toml. Stored keys were kept.")
        return 0

    name = args.name or stored.get("provider")
    if not name:
        print("Which provider? One of: " + ", ".join(providers.names()), file=sys.stderr)
        return 1
    if name not in providers.names():
        print(f"Unknown provider '{name}'. One of: {', '.join(providers.names())}. "
              "Use 'custom' with --base-url for any OpenAI-compatible API.", file=sys.stderr)
        return 1

    if action == "forget":
        keys.pop(name, None)
        if stored.get("provider") == name:
            for key in ("provider", "model", "base_url"):
                stored.pop(key, None)
        providers.save_file(config.PROVIDER_FILE, stored)
        print(f"Removed the {providers.label(name)} key.")
        return 0

    # set / models: both need a key
    if args.key_stdin:
        keys[name] = sys.stdin.readline().strip()
    elif name not in keys or args.rekey:
        import getpass

        keys[name] = getpass.getpass(f"{providers.label(name)} API key (not shown): ").strip()
    if not keys.get(name):
        print("No key given.", file=sys.stderr)
        return 1

    same = stored.get("provider") == name
    base_url = args.base_url or (stored.get("base_url", "") if same else "")
    model = args.model or (stored.get("model", "") if same else "")
    if name == "custom" and not base_url:
        print("A custom provider needs --base-url (the URL ending in /v1).", file=sys.stderr)
        return 1
    if name == "anthropic" and not model:
        model = providers.ANTHROPIC_DEFAULT_MODEL

    cfg = config.load()
    cfg["keys"] = keys
    cfg["enrich"].update(backend="api", provider=name, model=model or "-", base_url=base_url)
    try:
        backend = providers.build(cfg)
        if action == "models" or not model:
            listing = backend.models()
            # The key worked: keep it even though no model is chosen yet.
            providers.save_file(config.PROVIDER_FILE, stored)
            print("\n".join(listing))
            if action == "set":
                print(f"\nPick one: promptloom provider set {name} --model MODEL", file=sys.stderr)
                return 1
            return 0
        if not args.no_test:
            schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
            backend.complete('Reply with the JSON object {"ok": true}.', schema)
    except EnrichError as exc:
        print(f"Not saved: {exc}", file=sys.stderr)
        return 1

    stored.update(provider=name, model=model, base_url=base_url)
    providers.save_file(config.PROVIDER_FILE, stored)
    print(f"Summaries now use {providers.label(name)}, model {model}. "
          f"Key stored in {config.PROVIDER_FILE} (readable only by you).")
    print("Prompts from every harness will be sent to this provider to be summarised. "
          "`promptloom provider local` switches back.")
    return 0


def cmd_open(args) -> int:
    loom = Loom()
    if not loom.vault.enabled:
        print("Obsidian output is off (no vault in the config). `promptloom ui` opens the "
              "built-in app.", file=sys.stderr)
        return 1
    status = loom.read_status()
    target = status.get(args.what)
    if not target:
        print("No status yet. Run `promptloom sync` first.", file=sys.stderr)
        return 1
    problem = obsidian_problem(loom.vault.root, loom.vault.folder)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    open_uri("obsidian://open?path=" + quote(target))
    return 0


def _ui_running(url: str) -> bool:
    from urllib.request import urlopen

    try:
        with urlopen(url + "api/status", timeout=1):
            return True
    except OSError:
        return False


def cmd_ui(args) -> int:
    """Open the built-in app, serving it here if the daemon is not."""
    from . import server

    cfg = config.load()
    port = args.port or cfg.get("ui", {}).get("port", 8765)
    url = f"http://127.0.0.1:{port}/" + (f"#/search?q={quote(args.query)}" if args.query else "")
    base = f"http://127.0.0.1:{port}/"
    if _ui_running(base):
        if not args.no_open:
            open_uri(url)
        print(url)
        return 0
    httpd = server.serve(cfg, port=port)
    print(f"serving {base} (Ctrl+C to stop)", flush=True)
    if not args.no_open:
        open_uri(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_app(args) -> int:
    """The app in a window of its own, with a dock icon; serving it if needed."""
    from . import server

    cfg = config.load()
    port = cfg.get("ui", {}).get("port", 8765)
    if args.install:
        if sys.platform != "linux":
            print("A desktop entry is for Linux; on macOS use the menu bar app.", file=sys.stderr)
            return 1
        print(f"wrote {desktop.install_desktop_entry(port)}")
        return 0
    if args.uninstall:
        desktop.remove_desktop_entry()
        return 0
    base = f"http://127.0.0.1:{port}/"
    at = args.at_flag or args.at or ""
    if at.startswith("http"):
        url = at  # a URL handed over by the desktop entry's %u
    else:
        url = base + (f"#/{at.lstrip('#/')}" if at else "")
    if args.webview:
        desktop.run_webview(url)
        return 0
    if not _ui_running(base):
        desktop.spawn([sys.executable, "-m", "promptloom", "ui", "--no-open"])
        for _ in range(40):
            if _ui_running(base):
                break
            time.sleep(0.25)
    try:
        how = desktop.open_window(cfg, url)
    except desktop.DesktopError as exc:
        print(f"promptloom: {exc}", file=sys.stderr)
        return 1
    print({"webview": "opened in a window", "chrome": "opened in a window (app mode)",
           "browser": "opened in the browser"}[how] + f": {url}")
    return 0


def cmd_new(args) -> int:
    """Start a tool on a prompt from the command line."""
    cfg = config.load()
    prompt = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not prompt:
        print("nothing to send", file=sys.stderr)
        return 1
    try:
        done = launch.start(cfg, config.STATE_DIR, args.tool, prompt, args.model or "",
                            args.context or os.getcwd())
    except (launch.LaunchError, desktop.DesktopError) as exc:
        print(f"promptloom: {exc}", file=sys.stderr)
        return 1
    print(f"{done['tool']} in {done['dir']}" + (
        " (the prompt is on the clipboard)" if done["how"] == "gui" else ""))
    return 0


def cmd_brief(args) -> int:
    """Print a project's brief: paste it, pipe it, or point an assistant at it."""
    from .userdata import UserData

    store = Store(config.STATE_DIR)
    store.load()
    records = store.by_recency()
    project = args.project or Path(os.getcwd()).name
    if not any(r.session.project == project for r in records):
        print(f"No project named {project!r}. Known: "
              + ", ".join(sorted({r.session.project for r in records})), file=sys.stderr)
        return 1
    user = UserData(config.STATE_DIR)
    user.load()
    own = user.briefs.get(project, {})
    auto = brief.draft_brief(project, records, own.get("hidden", []))
    text = own.get("text", "")
    print(brief.for_prompt(auto, text) if args.short else brief.render_brief(auto, text), end="")
    return 0


def cmd_log(args) -> int:
    """Record a prompt from any tool, for the api harness to pick up."""
    from .harnesses.api import harness_name
    from .server import append_ingest

    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        print("nothing to log", file=sys.stderr)
        return 1
    item = {"harness": harness_name(args.harness), "prompt": text,
            "context": str(Path(args.context or os.getcwd()).expanduser()),
            "ts": extract.now_ts()}
    for key in ("model", "session", "title", "branch"):
        if getattr(args, key):
            item[key] = getattr(args, key)
    if args.reply:
        item["reply"] = args.reply
    append_ingest(config.STATE_DIR, [json.dumps(item, ensure_ascii=False)])
    print(f"logged for {item['harness']} in {item['context']}")
    return 0


def cmd_search(args) -> int:
    from . import analysis
    from .store import Store
    from .userdata import UserData

    store, user = Store(config.STATE_DIR), UserData(config.STATE_DIR)
    store.load()
    user.load()
    result = analysis.search(store.by_recency(), user, " ".join(args.query), args.limit)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    for hit in result["hits"]:
        print(f"{extract.local(hit['ts'])}  {hit['project']:<18} {hit['model'] or '?':<20} "
              f"{hit['title']}")
        print(f"    {hit['text']}")
    for note in result["notes"]:
        print(f"note  {note['title']}: {note['text']}")
    print(f"{result['total']} prompts, {len(result['notes'])} notes")
    return 0


def cmd_mcp(args) -> int:
    from . import mcp

    return mcp.run()


def _records() -> list:
    store = Store(config.STATE_DIR)
    store.load()
    return store.by_recency()


def _print_prs(state: dict) -> None:
    print(f"@{state.get('login', '?')} · checked {extract.local(state.get('checked', ''))}")
    if state.get("error"):
        print(f"error: {state['error']}")
    by_project: dict[str, list[dict]] = {}
    for pr in state["prs"]:
        by_project.setdefault(pr["project"], []).append(pr)
    for project, group in sorted(by_project.items()):
        where = group[0]["checkout"] or "no local checkout"
        print(f"\n{project}  ({group[0]['repo']} · {where})")
        for pr in group:
            whose = "mine" if pr["mine"] else ("review" if pr["asked"] else pr["author"])
            notes = [n for n in ("draft" if pr["draft"] else "",
                                 pr["checks"].lower(), pr["decision"].lower().replace("_", " "),
                                 f"{pr['waiting']} new" if pr["waiting"] else "") if n]
            print(f"  {whose:<10} #{pr['number']:<5} {pr['title'][:60]}"
                  + (f"  [{', '.join(notes)}]" if notes else ""))
            print(f"  {'':<10} {pr['url']}")
    if not state["prs"]:
        print("\nNo open pull requests.")


def cmd_pr(args) -> int:
    cfg = config.load()
    state_dir = config.STATE_DIR
    what = args.what or "list"
    try:
        if what in ("on", "off"):
            config.save_settings("prs", {"enabled": what == "on"})
            if what == "off":
                print("Pull requests: off.")
                return 0
            cfg["prs"]["enabled"] = True
            what = "check"
        if what == "check":
            state = prs.refresh(cfg, state_dir, _records())
            _print_prs(state)
            return 1 if state.get("error") else 0
        if what == "list":
            state = prs.load(state_dir)
            if not state.get("checked"):
                print("Not checked yet: `promptloom pr on`, or `promptloom pr check` once.")
                return 1
            _print_prs(state)
            return 0
        if not args.url:
            print(f"promptloom pr {what} needs a pull request URL", file=sys.stderr)
            return 2
        if what == "review":
            run = prs.start_review(cfg, state_dir, args.url, args.agent or "", args.add or "")
            print(f"Reviewing in the background ({run['id']}); you will get a notification.")
        elif what == "fix":
            run = prs.start_fix(cfg, state_dir, args.url, args.agent or "", args.add or "",
                                False if args.no_worktree else None)
            print(f"Opened a terminal for the fix ({run['id']}).")
        elif what == "worktree":
            print(prs.worktree_for(state_dir, args.url))
        elif what == "_run":  # the background half of `review`
            run = prs.run_review(cfg, state_dir, args.url)
            return 0 if run["status"] == "done" else 1
        elif what == "_notify":  # waits for a click on a notification's button
            prs.wait_for_action(cfg, state_dir, args.title, args.body,
                                args.buttons or [], args.url, args.run or "")
        else:
            print(f"unknown action {what!r}", file=sys.stderr)
            return 2
    except (prs.PRError, gitlocal.GitError) as exc:
        print(f"promptloom: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_worktree(args) -> int:
    cfg = config.load()
    paths = prs.local_checkouts(cfg, _records())
    try:
        if args.what == "add":
            if not args.project or not args.branch:
                print("promptloom worktree add PROJECT BRANCH [--base REF]", file=sys.stderr)
                return 2
            checkout = next((p for p in paths if p.name == args.project
                             or str(p) == args.project), None)
            if checkout is None:
                print(f"no checkout called {args.project!r}", file=sys.stderr)
                return 1
            print(gitlocal.add(checkout, args.branch, args.base or ""))
            return 0
        listing = [item for item in gitlocal.overview(paths) if item["worktrees"]]
        stale = [t for item in listing for t in item["worktrees"] if t["stale"]]
        if args.what == "clean":
            if not stale:
                print("No stale worktrees.")
                return 0
            for tree in stale:
                print(f"  {tree['path']}  ({tree['reason']})")
            if not args.yes and input(f"Remove these {len(stale)}? [y/N] ").lower() != "y":
                return 1
            for result in gitlocal.clean(paths, [t["path"] for t in stale],
                                         args.delete_branches):
                print(("  " if result["ok"] else "  failed: ") + result["message"])
            return 0
        for item in listing:
            print(f"{item['name']}  ({item['checkout']}, {item['branch'] or 'detached'})")
            for tree in item["worktrees"]:
                mark = "stale" if tree["stale"] else "keep "
                print(f"  {mark}  {tree['branch'] or 'detached':<24} {tree['path']}")
                print(f"         {tree['reason']}")
        if not listing:
            print("No worktrees besides the checkouts themselves.")
        elif stale:
            print(f"\n{len(stale)} stale: `promptloom worktree clean` removes them.")
    except gitlocal.GitError as exc:
        print(f"promptloom: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="promptloom", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the config file and vault folder").set_defaults(fn=cmd_init)

    p = sub.add_parser("sync", help="capture new prompts and update the vault once")
    p.add_argument("--all", action="store_true", help="summarise every pending session now")
    p.add_argument("--no-enrich", action="store_true", help="skip model summaries")
    p.add_argument("--reenrich", action="store_true", help="discard and redo all summaries")
    p.set_defaults(fn=cmd_sync)

    sub.add_parser("watch", help="run continuously (what the service does)").set_defaults(fn=cmd_watch)

    p = sub.add_parser("status", help="show the active and recent sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("capture", help="save a note or to-do against the active session")
    p.add_argument("text", nargs="*")
    p.add_argument("--todo", action="store_true", help="record it as an action item")
    p.add_argument("--context", help="attach to this directory instead of the active session")
    p.set_defaults(fn=cmd_capture)

    sub.add_parser("insights", help="rewrite Insights.md now").set_defaults(fn=cmd_insights)
    sub.add_parser("harnesses", help="list configured harnesses").set_defaults(fn=cmd_harnesses)

    p = sub.add_parser("provider", help="use a hosted model with your own API key")
    p.add_argument("action", nargs="?", choices=["show", "set", "models", "local", "forget"])
    p.add_argument("name", nargs="?", help="anthropic, openai, deepseek, openrouter, gemini, "
                   "groq, mistral, xai, together or custom")
    p.add_argument("--model")
    p.add_argument("--base-url", help="for 'custom': any OpenAI-compatible API")
    p.add_argument("--rekey", action="store_true", help="replace the stored key")
    p.add_argument("--key-stdin", action="store_true", help="read the key from stdin")
    p.add_argument("--no-test", action="store_true", help="save without a test request")
    p.set_defaults(fn=cmd_provider)

    p = sub.add_parser("ui", help="open the built-in app (search, notes, graph, timelines)")
    p.add_argument("query", nargs="?", help="start with this search")
    p.add_argument("--port", type=int)
    p.add_argument("--no-open", action="store_true", help="serve without opening a browser")
    p.set_defaults(fn=cmd_ui)

    p = sub.add_parser("app", help="open the app in a window of its own (dock icon)")
    p.add_argument("at", nargs="?", help="page to open: compose, search, prs, …")
    p.add_argument("--at", dest="at_flag", help=argparse.SUPPRESS)
    p.add_argument("--install", action="store_true", help="add it to the applications menu (Linux)")
    p.add_argument("--uninstall", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--webview", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("new", help="start a tool on a prompt: promptloom new -t claude 'fix …'")
    p.add_argument("text", nargs="*", help="the prompt (or on stdin)")
    p.add_argument("-t", "--tool", default="claude",
                   help="claude, codex, copilot, hermes, pi, openclaw, vscode, cursor")
    p.add_argument("-m", "--model")
    p.add_argument("-C", "--context", help="project folder (default: the current one)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("brief", help="what an assistant should know before working on a project")
    p.add_argument("project", nargs="?", help="directory name (default: the current folder's)")
    p.add_argument("--short", action="store_true", help="cut to fit at the top of a prompt")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("search", help="search every prompt and note")
    p.add_argument("query", nargs="*", help='words, plus filters: model:opus project:shop '
                   'harness:codex tag:bug theme:auth after:2026-01-01 before:2026-02-01')
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("log", help="record a prompt from any tool or script")
    p.add_argument("text", nargs="*", help="the prompt (or on stdin)")
    p.add_argument("--harness", default="cli", help="name of the tool it came from")
    p.add_argument("--model")
    p.add_argument("--context", help="project directory (default: the current one)")
    p.add_argument("--session", help="group prompts into this session id")
    p.add_argument("--title")
    p.add_argument("--branch")
    p.add_argument("--reply", help="the answer, if you have it")
    p.set_defaults(fn=cmd_log)

    sub.add_parser("mcp", help="MCP server on stdio, so assistants can search your "
                   "prompt history").set_defaults(fn=cmd_mcp)

    p = sub.add_parser("pr", help="pull requests: list, check, review or fix with an agent")
    p.add_argument("what", nargs="?", choices=["list", "on", "off", "check", "review", "fix",
                                              "worktree", "_run", "_notify"])
    p.add_argument("url", nargs="?", help="the pull request")
    p.add_argument("--agent", help="claude, codex or copilot (default: [prs] agent)")
    p.add_argument("--add", help="anything to add to the agent's prompt")
    p.add_argument("--no-worktree", action="store_true",
                   help="fix in the checkout itself rather than a worktree of its own")
    p.add_argument("--title", default="", help=argparse.SUPPRESS)
    p.add_argument("--body", default="", help=argparse.SUPPRESS)
    p.add_argument("--action", dest="buttons", action="append", help=argparse.SUPPRESS)
    p.add_argument("--run", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_pr)

    p = sub.add_parser("worktree", help="list worktrees, add one, or remove stale ones")
    p.add_argument("what", nargs="?", default="list", choices=["list", "add", "clean"])
    p.add_argument("project", nargs="?", help="add: the checkout (its folder name)")
    p.add_argument("branch", nargs="?", help="add: the branch, new or existing")
    p.add_argument("--base", help="add: where a new branch starts (default: HEAD)")
    p.add_argument("--yes", action="store_true", help="clean: do not ask first")
    p.add_argument("--delete-branches", action="store_true",
                   help="clean: also delete the local branches (git refuses unmerged ones)")
    p.set_defaults(fn=cmd_worktree)

    p = sub.add_parser("open", help="open a promptloom note in Obsidian")
    p.add_argument("what", nargs="?", default="home",
                   choices=["home", "actions", "insights", "inbox", "connections", "map"])
    p.set_defaults(fn=cmd_open)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
