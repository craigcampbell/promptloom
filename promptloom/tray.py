#!/usr/bin/env python3
"""A top bar icon for promptloom on Linux, the way Claude and ChatGPT do it.

This is a StatusNotifierItem over D-Bus (what the Ubuntu AppIndicator
extension and KDE draw) with a dbusmenu, so it appears at once, with no shell
extension and no log-out. It only reads status.json and runs `promptloom app`.

It runs under the system Python, which has PyGObject; the daemon starts it
with `promptloom tray` (see __main__) and it exits on its own if another copy
is running or nothing on the desktop can show it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)  # register_object still works

try:
    import gi

    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError):
    sys.exit("promptloom tray: PyGObject (python3-gi) is not available")

BUS_NAME = "io.github.craigcampbell.promptloom.Tray"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"
ICON_SIZES = (22, 24, 32, 48)
RECENT = 6

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewToolTip"/><signal name="NewStatus"><arg type="s"/></signal>
  </interface>
</node>"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/><arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/><arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/><arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/><arg name="name" type="s" direction="in"/><arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/><arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/><arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/><arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow"><arg name="id" type="i" direction="in"/><arg name="needUpdate" type="b" direction="out"/></method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/><arg name="updatesNeeded" type="ai" direction="out"/><arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
    <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
  </interface>
</node>"""


def icon_pixmaps() -> list:
    """The loom icon in white, as ARGB pixmaps at the sizes panels ask for."""
    out = []
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return out
    svg = str(Path(__file__).parent / "ui" / "tray-icon.svg")
    for size in ICON_SIZES:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(svg, size, size, True)
        except GLib.Error:
            return out
        if not pixbuf.get_has_alpha():
            pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
        rgba = pixbuf.get_pixels()
        stride, width, height = pixbuf.get_rowstride(), pixbuf.get_width(), pixbuf.get_height()
        argb = bytearray()
        for y in range(height):
            row = rgba[y * stride:y * stride + width * 4]
            for x in range(0, width * 4, 4):
                r, g, b, a = row[x:x + 4]
                argb += bytes((a, r, g, b))  # the SNI spec wants ARGB32, network order
        out.append((width, height, bytes(argb)))
    return out


class Tray:
    def __init__(self, status_path: Path, binary: str):
        self.status_path = status_path
        self.binary = binary
        self.status: dict = {}
        self.items: list[dict] = []  # dbusmenu items, by index + 1 as id
        self.revision = 1
        self.pixmaps = icon_pixmaps()
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        self.menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.bus.register_object(ITEM_PATH, self.item_info, self.on_item_call,
                                 self.on_item_get, None)
        self.bus.register_object(MENU_PATH, self.menu_info, self.on_menu_call,
                                 self.on_menu_get, None)
        self.load()
        monitor = Gio.File.new_for_path(str(status_path.parent)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        monitor.connect("changed", self.on_changed)
        self._monitor = monitor

    # the status file

    def load(self) -> None:
        try:
            self.status = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            self.status = {}
        self.rebuild_menu()

    def on_changed(self, _monitor, file, other, _event) -> None:
        names = {file.get_basename(), other.get_basename() if other else None}
        if "status.json" in names:
            self.load()
            self.emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)
            self.emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewTitle", None)

    def title(self) -> str:
        active = self.status.get("active")
        return f"Promptloom: {active['title']}" if active else "Promptloom"

    def tooltip(self) -> str:
        parts = [f"{self.status.get('sessions', 0)} sessions"]
        prs = self.status.get("prs") or {}
        need = (prs.get("waiting") or 0) + (prs.get("asked") or 0)
        if need:
            parts.append(f"{need} pull request{'s' if need > 1 else ''} need you")
        if self.status.get("waiting"):
            parts.append(f"summaries paused: {self.status['waiting']}")
        return " · ".join(parts)

    # the menu

    def rebuild_menu(self) -> None:
        active = self.status.get("active")
        prs = self.status.get("prs") or {}
        need = (prs.get("waiting") or 0) + (prs.get("asked") or 0)
        items: list[dict] = []
        if active:
            items.append({"label": f"Now: {active['title'][:48]}", "page": f"session/{active['key']}"})
            items.append({"sep": True})
        items += [
            {"label": "New prompt…", "page": "compose"},
            {"label": "Open Promptloom", "page": ""},
            {"label": "Search prompts", "page": "search"},
            {"label": "Threads", "page": "threads"},
        ]
        if prs.get("enabled"):
            items.append({"label": f"Pull requests ({need} need you)" if need else "Pull requests",
                          "page": "prs"})
        recent = [s for s in self.status.get("recent", []) if not active or s["key"] != active["key"]]
        if recent:
            items.append({"sep": True})
            items += [{"label": s["title"][:48], "page": f"session/{s['key']}"} for s in recent[:RECENT]]
        items.append({"sep": True})
        items.append({"label": "Quit this icon", "quit": True})
        self.items = items
        self.revision += 1
        self.emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                  GLib.Variant("(ui)", (self.revision, 0)))

    def props(self, item: dict) -> dict:
        if item.get("sep"):
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", item["label"]), "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True)}

    def layout(self) -> GLib.Variant:
        children = [GLib.Variant("(ia{sv}av)", (i + 1, self.props(item), []))
                    for i, item in enumerate(self.items)]
        root = {"children-display": GLib.Variant("s", "submenu")}
        return GLib.Variant("(u(ia{sv}av))", (self.revision, (0, root, children)))

    def activate(self, item_id: int) -> None:
        if not 1 <= item_id <= len(self.items):
            return
        item = self.items[item_id - 1]
        if item.get("quit"):
            self.loop.quit()
            return
        if "page" in item:
            self.open(item["page"])

    def open(self, page: str) -> None:
        argv = [self.binary, "app"] + ([page] if page else [])
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    # D-Bus plumbing

    def emit(self, path: str, interface: str, name: str, params) -> None:
        try:
            self.bus.emit_signal(None, path, interface, name, params)
        except GLib.Error:
            pass

    def on_item_get(self, _conn, _sender, _path, _iface, name):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "promptloom"),
            "Title": GLib.Variant("s", self.title()),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", "" if self.pixmaps else "utilities-terminal-symbolic"),
            "IconPixmap": GLib.Variant("a(iiay)", self.pixmaps),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], self.title(), self.tooltip())),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def on_item_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "Activate":
            self.open("")
        elif method == "SecondaryActivate":
            self.open("compose")
        invocation.return_value(None)

    def on_menu_get(self, _conn, _sender, _path, _iface, name):
        values = {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                  "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}
        return values.get(name)

    def on_menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(self.layout())
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or range(1, len(self.items) + 1)
            rows = [(i, self.props(self.items[i - 1])) for i in ids if 1 <= i <= len(self.items)]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (rows,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            value = self.props(self.items[item_id - 1]).get(name) if 1 <= item_id <= len(self.items) else None
            invocation.return_value(GLib.Variant("(v)", (value or GLib.Variant("s", ""),)))
        elif method == "Event":
            item_id, event, _data, _ts = params.unpack()
            if event == "clicked":
                self.activate(item_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _ts in params.unpack()[0]:
                if event == "clicked":
                    self.activate(item_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def register(self) -> bool:
        try:
            self.bus.call_sync(WATCHER, "/StatusNotifierWatcher", WATCHER,
                               "RegisterStatusNotifierItem",
                               GLib.Variant("(s)", (self.bus.get_unique_name(),)),
                               None, Gio.DBusCallFlags.NONE, 5000, None)
            return True
        except GLib.Error as exc:
            print(f"promptloom tray: no status notifier host: {exc.message}", file=sys.stderr)
            return False

    def run(self) -> int:
        self.loop = GLib.MainLoop()
        taken = []

        def on_lost(*_):
            taken.append(True)
            self.loop.quit()

        Gio.bus_own_name_on_connection(self.bus, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                                       None, on_lost)
        # Let the name request settle: another copy already running means we leave.
        GLib.timeout_add(300, lambda: (taken or self._start()) and False)
        self.loop.run()
        return 0

    def _start(self) -> bool:
        if not self.register():
            self.loop.quit()
            return True
        # The watcher comes back after a shell restart; register again then.
        Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                         lambda *_: self.register(), None)
        return True


def extension_active() -> bool:
    """True when promptloom's own GNOME Shell extension already draws an indicator."""
    try:
        out = subprocess.run(["gnome-extensions", "info", "promptloom@craigcampbell.github.io"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "State: ACTIVE" in out


def main(argv: list[str]) -> int:
    if extension_active():
        return 0  # the top bar already has one
    status = Path(argv[1]) if len(argv) > 1 else Path(
        os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "promptloom/status.json"
    binary = argv[2] if len(argv) > 2 else "promptloom"
    return Tray(status, binary).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
