# promptloom on macOS

The daemon and the CLI are the same Python package as on Linux. What differs is
how they run: a launchd agent instead of a systemd service, and a menu bar app
instead of the GNOME top bar extension.

## Requirements

- macOS 13 (Ventura) or later
- [uv](https://docs.astral.sh/uv/) or pipx, for the CLI
- The Xcode Command Line Tools, for the menu bar app: `xcode-select --install`

## Install

From the repository root:

```sh
./install.sh
```

This installs the `promptloom` command, starts the daemon as the launchd agent
`io.github.craigcampbell.promptloom`, builds `Promptloom.app` with `swiftc`,
copies it to `~/Applications`, opens it and adds it as a login item. macOS may
ask once whether Terminal can control System Events; that is the login item.

`./uninstall.sh` removes the agent, the app and the command. Your config,
captured state and any vault stay where they are.

To build the app alone: `macos/build.sh` writes `macos/build/Promptloom.app`.

## The menu bar

The menu bar shows the title of the session you are in, or an icon when none
is active. The menu has:

- Quick capture and Add to-do, which save text against the active session
- The active session and recent ones, with harness, model, project and age.
  Clicking one opens it in the built-in Promptloom UI, or in Obsidian when the
  UI is off.
- Open Promptloom and Search, in the browser
- Open in Obsidian, when a vault is configured
- Why summaries are paused, and recent errors, when there are any

Like the GNOME extension, the app only reads
`~/.local/state/promptloom/status.json` and calls `promptloom capture`. It does
no capturing itself.

## Logs

```sh
tail -f ~/Library/Logs/promptloom.log
launchctl print gui/$(id -u)/io.github.craigcampbell.promptloom
```

## Status

The menu bar app has not yet been run on a Mac. It was written against stable
SwiftUI and AppKit APIs and its Foundation code was tested on Linux, but expect
rough edges. Reports welcome.
