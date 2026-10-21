#!/usr/bin/env bash
# Installs promptloom for the current user: the CLI, the background service and
# the menu bar indicator (a GNOME top bar extension on Linux, a menu bar app on
# macOS). Nothing here needs sudo. Safe to run again.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== CLI"
# pywebview gives `promptloom app` a native window on macOS and Windows (on
# Linux it needs the system's PyGObject, so Chrome's app mode is used there).
extras=(--with anthropic)
case "$(uname -s)" in Darwin) extras+=(--with pywebview) ;; esac
if command -v uv >/dev/null; then
    uv tool install --force "${extras[@]}" --editable "$here"
elif command -v pipx >/dev/null; then
    pipx install --force --editable "$here"
    pipx inject promptloom anthropic
    case "$(uname -s)" in Darwin) pipx inject promptloom pywebview ;; esac
else
    echo "Install uv (https://docs.astral.sh/uv/) or pipx first." >&2
    exit 1
fi
"$HOME/.local/bin/promptloom" init

install_linux() {
    local uuid="promptloom@craigcampbell.github.io"
    local old_uuid="promptloom@craig.local"

    echo "== service"
    install -D -m 644 "$here/systemd/promptloom.service" "$HOME/.config/systemd/user/promptloom.service"
    systemctl --user daemon-reload
    systemctl --user enable promptloom.service
    systemctl --user restart promptloom.service

    echo "== app launcher"
    "$HOME/.local/bin/promptloom" app --install

    echo "== top bar extension"
    local extensions="$HOME/.local/share/gnome-shell/extensions"
    if [[ -L "$extensions/$old_uuid" ]]; then
        gnome-extensions disable "$old_uuid" 2>/dev/null || true
        rm -f "$extensions/$old_uuid"
    fi
    if ! command -v gnome-extensions >/dev/null; then
        echo "GNOME Shell not found; skipping the top bar extension."
        return
    fi
    mkdir -p "$extensions"
    ln -sfn "$here/gnome-extension/$uuid" "$extensions/$uuid"
    if ! gnome-extensions enable "$uuid" 2>/dev/null; then
        # A running Wayland session only discovers new extensions at login, so
        # mark it enabled for next time.
        local current
        current="$(gsettings get org.gnome.shell enabled-extensions)"
        if [[ "$current" != *"$uuid"* ]]; then
            if [[ "$current" == "@as []" || "$current" == "[]" ]]; then
                gsettings set org.gnome.shell enabled-extensions "['$uuid']"
            else
                gsettings set org.gnome.shell enabled-extensions "${current%]}, '$uuid']"
            fi
        fi
        echo "Log out and back in for the top bar indicator to appear."
    fi
}

install_macos() {
    local label="io.github.craigcampbell.promptloom"
    local agent="$HOME/Library/LaunchAgents/$label.plist"
    local domain="gui/$(id -u)"

    echo "== service"
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    sed "s|__HOME__|$HOME|g" "$here/macos/$label.plist" > "$agent"
    launchctl bootout "$domain/$label" 2>/dev/null || true
    launchctl bootstrap "$domain" "$agent"

    echo "== menu bar app"
    if ! command -v swiftc >/dev/null; then
        echo "swiftc not found. Install the Xcode Command Line Tools with"
        echo "'xcode-select --install', then run this script again for the menu bar app."
        return
    fi
    local built
    built="$("$here/macos/build.sh" "$here/macos/build" | tail -1)"
    mkdir -p "$HOME/Applications"
    pkill -x Promptloom 2>/dev/null || true
    rm -rf "$HOME/Applications/Promptloom.app"
    cp -R "$built" "$HOME/Applications/Promptloom.app"
    open "$HOME/Applications/Promptloom.app"
    # Start it at login. Asking System Events may prompt for permission once.
    osascript >/dev/null 2>&1 <<EOF || echo "Could not add a login item; add Promptloom in System Settings > General > Login Items."
tell application "System Events"
    if not (exists login item "Promptloom") then
        make login item at end with properties {path:"$HOME/Applications/Promptloom.app", hidden:true}
    end if
end tell
EOF
}

case "$(uname -s)" in
    Linux) install_linux ;;
    Darwin) install_macos ;;
    *) echo "No service or indicator for $(uname -s) yet; run 'promptloom watch' yourself." ;;
esac

echo
echo "Done. 'promptloom ui' opens the app; 'promptloom status' shows what has been captured."
