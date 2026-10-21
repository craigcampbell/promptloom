"""Talking to the desktop: finding programs, opening terminals and URLs, the
clipboard, and showing the app in a window of its own with a dock icon.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

TERMINALS = [
    ("ptyxis", ["ptyxis", "--new-window", "--"]),
    ("gnome-terminal", ["gnome-terminal", "--"]),
    ("kgx", ["kgx", "--"]),
    ("konsole", ["konsole", "-e"]),
    ("xfce4-terminal", ["xfce4-terminal", "-x"]),
    ("kitty", ["kitty"]),
    ("alacritty", ["alacritty", "-e"]),
    ("wezterm", ["wezterm", "start", "--"]),
    ("foot", ["foot"]),
    ("x-terminal-emulator", ["x-terminal-emulator", "-e"]),
]

# Browsers with an "app" mode: one page in a window of its own, no address bar.
CHROMIUMS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
             "brave-browser", "microsoft-edge", "microsoft-edge-stable", "vivaldi"]
WIN_CHROMIUMS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"]
MAC_CHROMIUMS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium",
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
# What Chromium names an --app window, so the dock can match it to our entry.
APP_WM_CLASS = "chrome-127.0.0.1__-Default"
ICON = Path(__file__).parent / "ui" / "app-icon.svg"
DESKTOP_ID = "io.github.craigcampbell.promptloom"


class DesktopError(Exception):
    pass


def search_path() -> str:
    """PATH plus the places tools installed by npm, bun, cargo or Homebrew end up."""
    home = Path.home()
    extra = [home / ".local/bin", home / ".npm-global/bin", home / ".bun/bin",
             home / ".cargo/bin", Path("/usr/local/bin"), Path("/opt/homebrew/bin")]
    extra += [Path(p) for p in sorted(glob.glob(str(home / ".nvm/versions/node/*/bin")))[-1:]]
    parts = os.environ.get("PATH", "").split(os.pathsep)
    parts += [str(p) for p in extra if p.is_dir() and str(p) not in parts]
    return os.pathsep.join(p for p in parts if p)


def child_env() -> dict:
    return {**os.environ, "PATH": search_path()}


def which(program: str) -> str:
    if os.path.isabs(program):
        return program if os.access(program, os.X_OK) else ""
    return shutil.which(program, path=search_path()) or ""


def terminal(cfg: dict) -> list[str]:
    """How to open a terminal window running a command given after these words."""
    configured = cfg.get("terminal") or cfg.get("prs", {}).get("terminal") or []
    if configured:
        return list(configured)
    if sys.platform == "darwin":
        return ["open", "-a", "Terminal"]
    if sys.platform == "win32":
        # Windows Terminal when it is there, else a console window that stays open.
        return ["wt", "new-tab", "--"] if which("wt") else ["cmd", "/c", "start", "", "cmd", "/k"]
    for program, argv in TERMINALS:
        if which(program):
            return argv
    raise DesktopError('no terminal found: set terminal = ["gnome-terminal", "--"] '
                       "(or your own) in config.toml")


def spawn(argv: list[str], cwd: str | None = None) -> None:
    """Start a program that outlives the daemon (a restart would kill its children)."""
    env = child_env()
    if sys.platform.startswith("linux") and which("systemd-run") and env.get("XDG_RUNTIME_DIR"):
        keep = ("PATH", "HOME", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CURRENT_DESKTOP",
                "GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST")
        argv = ["systemd-run", "--user", "--collect", "--quiet",
                f"--working-directory={cwd or Path.home()}",
                *(f"--setenv={k}={env[k]}" for k in keep if env.get(k)), "--", *argv]
    extra = {}
    if sys.platform == "win32":
        extra["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                  | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        extra["start_new_session"] = True
    subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra)


def open_uri(uri: str) -> None:
    if sys.platform == "darwin":
        opener = ["open", uri]
    elif sys.platform == "win32":
        os.startfile(uri)  # noqa: S606 - a URL we built, not user input
        return
    else:
        opener = ["xdg-open", uri]
    subprocess.Popen(opener, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def open_terminal(cfg: dict, path: str) -> None:
    """A terminal with a shell in the folder."""
    if sys.platform == "darwin":
        spawn(["open", "-a", "Terminal", path])
        return
    if sys.platform == "win32":
        spawn(["wt", "-d", path] if which("wt") else ["cmd", "/c", "start", "", "cmd", "/k"],
              cwd=path)
        return
    spawn([*terminal(cfg), "sh", "-c", 'cd "$1" && exec "${SHELL:-/bin/sh}"', "sh", path],
          cwd=path)


def copy_to_clipboard(text: str) -> bool:
    """Put text on the clipboard with whatever this desktop has. True if it worked."""
    for argv in (["pbcopy"], ["clip"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                 ["xsel", "--clipboard", "--input"]):
        if which(argv[0]):
            try:
                subprocess.run(argv, input=text, text=True, check=True, timeout=5,
                               env=child_env())
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


# the app in a window of its own

def chromium() -> str:
    candidates = (MAC_CHROMIUMS if sys.platform == "darwin"
                  else WIN_CHROMIUMS if sys.platform == "win32" else [])
    for program in [*CHROMIUMS, "chrome", "msedge", *candidates]:
        found = which(program)
        if found:
            return found
    return ""


DESKTOP_APPS = {
    "darwin": ["/Applications/Promptloom.app", "~/Applications/Promptloom.app"],
    "win32": ["~/AppData/Local/Programs/Promptloom/Promptloom.exe"],
    "linux": ["promptloom-desktop", "/opt/Promptloom/promptloom-desktop",
              "~/Applications/Promptloom.AppImage"],
}


def desktop_app() -> list[str]:
    """How to start the installed desktop app (the Electron one), or []."""
    key = "darwin" if sys.platform == "darwin" else "win32" if sys.platform == "win32" else "linux"
    for item in DESKTOP_APPS[key]:
        path = Path(item).expanduser()
        if "/" in item and path.exists():
            return ["open", "-a", str(path)] if key == "darwin" else [str(path)]
        if "/" not in item and which(item):
            return [item]
    return []


def open_window(cfg: dict, url: str) -> str:
    """Show the app in a window of its own. Returns how, for the caller to report.

    The desktop app (desktop/, an Electron app with a menu bar icon) comes
    first when it is installed. Otherwise pywebview, then a Chromium-family
    browser in app mode, then the default browser.
    """
    how = cfg.get("ui", {}).get("window", "auto")
    page = url.split("#/", 1)[1] if "#/" in url else ""
    if how in ("auto", "desktop"):
        argv = desktop_app()
        if argv:
            spawn(argv + ([f"promptloom://{page}"] if page else []))
            return "desktop"
        if how == "desktop":
            raise DesktopError("the Promptloom desktop app is not installed")
    if how in ("auto", "webview"):
        try:
            import webview  # noqa: F401
        except ImportError:
            if how == "webview":
                raise DesktopError("pywebview is not installed: "
                                   "uv tool install --with pywebview --editable .")
        else:
            spawn([sys.executable, "-m", "promptloom", "app", "--webview", url])
            return "webview"
    if how in ("auto", "chrome"):
        browser = chromium()
        if browser:
            profile = config.STATE_DIR / "app-window"
            profile.mkdir(parents=True, exist_ok=True)
            spawn([browser, f"--app={url}", "--class=Promptloom",
                   f"--user-data-dir={profile}", "--no-first-run",
                   "--no-default-browser-check", "--window-size=1280,860"])
            return "chrome"
        if how == "chrome":
            raise DesktopError("no Chrome, Chromium, Brave or Edge found")
    open_uri(url)
    return "browser"


def run_webview(url: str) -> None:
    """The window itself, in this process: pywebview blocks until it is closed."""
    import webview

    webview.create_window("Promptloom", url, width=1280, height=860, min_size=(720, 480))
    webview.start()


def desktop_entry(port: int) -> str:
    launcher = which("promptloom") or f"{sys.executable} -m promptloom"
    return f"""[Desktop Entry]
Type=Application
Name=Promptloom
Comment=Every prompt you have typed into your AI coding tools, and what connects them
Exec={launcher} app %u
Icon={DESKTOP_ID}
Terminal=false
Categories=Development;
Keywords=AI;prompts;Claude;Codex;Copilot;
StartupNotify=true
StartupWMClass={APP_WM_CLASS}
SingleMainWindow=true
Actions=New;Search;PullRequests;

[Desktop Action New]
Name=New prompt
Exec={launcher} app --at compose

[Desktop Action Search]
Name=Search prompts
Exec={launcher} app --at search

[Desktop Action PullRequests]
Name=Pull requests
Exec={launcher} app --at prs
"""


def install_desktop_entry(port: int) -> Path:
    """A launcher in the applications menu and dock (Linux). Returns its path."""
    data = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    icons = data / "icons" / "hicolor" / "scalable" / "apps"
    icons.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, icons / f"{DESKTOP_ID}.svg")
    apps = data / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    path = apps / f"{DESKTOP_ID}.desktop"
    path.write_text(desktop_entry(port))
    for argv in (["update-desktop-database", str(apps)],
                 ["gtk-update-icon-cache", "-q", str(data / "icons" / "hicolor")]):
        if which(argv[0]):
            subprocess.run(argv, capture_output=True)
    return path


def windows_toast(title: str, body: str) -> None:
    """A notification through PowerShell (Windows has no notify-send)."""
    script = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$env:PL_TITLE</text><text>$env:PL_BODY</text></binding></visual></toast>")
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Promptloom").Show((New-Object Windows.UI.Notifications.ToastNotification $xml))
"""
    env = {**os.environ, "PL_TITLE": title, "PL_BODY": body}
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def remove_desktop_entry() -> None:
    data = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    for path in (data / "applications" / f"{DESKTOP_ID}.desktop",
                 data / "icons" / "hicolor" / "scalable" / "apps" / f"{DESKTOP_ID}.svg"):
        path.unlink(missing_ok=True)
