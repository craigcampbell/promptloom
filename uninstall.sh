#!/usr/bin/env bash
# Removes the service, the menu bar indicator and the CLI. The vault, the
# config and the captured state are left in place.
set -uo pipefail

case "$(uname -s)" in
    Linux)
        systemctl --user disable --now promptloom.service 2>/dev/null
        rm -f "$HOME/.config/systemd/user/promptloom.service"
        systemctl --user daemon-reload
        for uuid in promptloom@craigcampbell.github.io promptloom@craig.local; do
            gnome-extensions disable "$uuid" 2>/dev/null
            rm -f "$HOME/.local/share/gnome-shell/extensions/$uuid"
        done
        state="~/.local/state/promptloom"
        ;;
    Darwin)
        label="io.github.craigcampbell.promptloom"
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
        rm -f "$HOME/Library/LaunchAgents/$label.plist"
        osascript -e 'tell application "System Events" to delete login item "Promptloom"' >/dev/null 2>&1
        pkill -x Promptloom 2>/dev/null
        rm -rf "$HOME/Applications/Promptloom.app"
        state="~/.local/state/promptloom and ~/Library/Logs/promptloom.log"
        ;;
    *)
        state="~/.local/state/promptloom"
        ;;
esac

if command -v uv >/dev/null; then uv tool uninstall promptloom; else pipx uninstall promptloom; fi
echo "Removed. Left in place: your vault, ~/.config/promptloom and $state."
