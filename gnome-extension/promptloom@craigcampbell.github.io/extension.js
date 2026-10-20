// Top bar indicator for promptloom. It only reads the status file the daemon
// writes, opens the built-in app or Obsidian, and shells out to the
// `promptloom` CLI for captures.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const STATE_DIR = GLib.build_filenamev([GLib.get_user_state_dir(), 'promptloom']);
const STATUS_NAME = 'status.json';
const PANEL_TITLE_CHARS = 32;

function ago(iso) {
    const then = Date.parse(iso);
    if (Number.isNaN(then))
        return '';
    const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
    if (minutes < 1)
        return 'now';
    if (minutes < 60)
        return `${minutes}m ago`;
    if (minutes < 60 * 24)
        return `${Math.round(minutes / 60)}h ago`;
    return `${Math.round(minutes / (60 * 24))}d ago`;
}

function clip(text, limit) {
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function openInObsidian(path, problem) {
    if (!path)
        return;
    if (problem) {
        // Obsidian would only answer with its "Vault not found" dialog.
        Main.notify('Promptloom', problem);
        return;
    }
    try {
        Gio.AppInfo.launch_default_for_uri(
            `obsidian://open?path=${encodeURIComponent(path)}`, null);
    } catch (e) {
        Main.notify('Promptloom', `Could not open Obsidian: ${e.message}`);
    }
}

function openUri(uri) {
    try {
        Gio.AppInfo.launch_default_for_uri(uri, null);
    } catch (e) {
        Main.notify('Promptloom', `Could not open ${uri}: ${e.message}`);
    }
}

function promptloomBinary() {
    return GLib.find_program_in_path('promptloom') ??
        GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'promptloom']);
}

const SessionItem = GObject.registerClass(
class SessionItem extends PopupMenu.PopupBaseMenuItem {
    _init(session, active, open) {
        super._init();
        const box = new St.BoxLayout({vertical: true, x_expand: true});
        const title = new St.Label({text: clip(session.title, 60)});
        if (active)
            title.add_style_class_name('promptloom-active-title');
        const meta = [session.harness_label, session.project, ago(session.updated)];
        if (session.model)
            meta.push(session.model);
        if (active)
            meta.push(session.session_id);
        box.add_child(title);
        box.add_child(new St.Label({
            text: meta.filter(part => part).join(' · '),
            style_class: 'promptloom-meta',
        }));
        this.add_child(box);
        this.connect('activate', () => open(session));
    }
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Promptloom', false);
        this._status = {};

        const box = new St.BoxLayout();
        const iconFile = extension.dir.get_child('icons').get_child('promptloom-symbolic.svg');
        box.add_child(new St.Icon({
            gicon: new Gio.FileIcon({file: iconFile}),
            style_class: 'system-status-icon',
        }));
        this._label = new St.Label({
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'promptloom-label',
            visible: false,
        });
        box.add_child(this._label);
        this.add_child(box);

        // Quick capture
        const captureItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._entry = new St.Entry({
            hint_text: 'Capture a note, or "todo …"',
            can_focus: true,
            x_expand: true,
            style_class: 'promptloom-entry',
        });
        this._entry.clutter_text.connect('activate', () => this._capture());
        captureItem.add_child(this._entry);
        this.menu.addMenuItem(captureItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._sessions = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._sessions);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // The built-in app.
        this._appItem = this.menu.addAction('Open Promptloom', () => openUri(this._status.ui));
        this._searchItem = this.menu.addAction('Search prompts', () => openUri(`${this._status.ui}#/search`));
        this._graphItem = this.menu.addAction('Connections graph', () => openUri(`${this._status.ui}#/graph`));

        // Obsidian, when a vault is configured.
        this._obsidian = new PopupMenu.PopupSubMenuMenuItem('Obsidian');
        this.menu.addMenuItem(this._obsidian);
        const sub = this._obsidian.menu;
        this._actionsItem = sub.addAction('Action items', () => this._open(this._status.actions));
        sub.addAction('Connections map', () => this._open(this._status.map));
        sub.addAction('Insights', () => this._open(this._status.insights));
        sub.addAction('Inbox', () => this._open(this._status.inbox));
        sub.addAction('Vault home', () => this._open(this._status.home));
        this._copyItem = this.menu.addAction('Copy active session ID', () => {
            const id = this._status.active?.session_id;
            if (id)
                St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, id);
        });
        this._noteItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._noteItem.label.add_style_class_name('promptloom-meta');
        this.menu.addMenuItem(this._noteItem);

        this.menu.connect('open-state-changed', (_menu, open) => {
            if (!open)
                return;
            this._render();  // refresh the "5m ago" stamps
            this._focusId = GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                this._focusId = 0;
                this._entry.grab_key_focus();
                return GLib.SOURCE_REMOVE;
            });
        });

        // The daemon replaces the file atomically, so watch the directory.
        GLib.mkdir_with_parents(STATE_DIR, 0o755);
        this._dir = Gio.File.new_for_path(STATE_DIR);
        this._monitor = this._dir.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
        this._monitor.connect('changed', (_m, file, other) => {
            if (file.get_basename() === STATUS_NAME || other?.get_basename() === STATUS_NAME)
                this._load();
        });
        this._load();
    }

    _load() {
        const file = this._dir.get_child(STATUS_NAME);
        file.load_contents_async(null, (source, result) => {
            try {
                const [, bytes] = source.load_contents_finish(result);
                this._status = JSON.parse(new TextDecoder().decode(bytes));
            } catch (_e) {
                return;  // missing or mid-write: keep showing the last good status
            }
            if (this._sessions)
                this._render();
        });
    }

    _render() {
        const status = this._status;
        const active = status.active;

        this._label.text = active ? clip(active.title, PANEL_TITLE_CHARS) : '';
        this._label.visible = Boolean(active);

        this._sessions.removeAll();
        const recent = status.recent ?? [];
        const open = session => this._openSession(session);
        if (active)
            this._sessions.addMenuItem(new SessionItem(active, true, open));
        for (const session of recent) {
            if (active && session.key === active.key)
                continue;
            this._sessions.addMenuItem(new SessionItem(session, false, open));
        }
        if (recent.length === 0) {
            this._sessions.addMenuItem(new PopupMenu.PopupMenuItem(
                status.updated ? 'No sessions captured yet' : 'Waiting for the promptloom daemon…',
                {reactive: false}));
        }

        this._actionsItem.label.text = `Action items (${status.open_actions ?? 0} open)`;
        this._copyItem.visible = Boolean(active);
        for (const item of [this._appItem, this._searchItem, this._graphItem])
            item.visible = Boolean(status.ui);
        this._obsidian.visible = Boolean(status.vault);

        const notes = [];
        if (status.vault_problem)
            notes.push(status.vault_problem);
        if (status.waiting)
            notes.push(`Summaries paused: ${status.waiting}`);
        else if (status.summarising)
            notes.push(`${status.summarising} sessions awaiting a summary`);
        for (const error of status.errors ?? [])
            notes.push(error);
        this._noteItem.label.text = notes.join('\n');
        this._noteItem.visible = notes.length > 0;
    }

    _open(path) {
        openInObsidian(path, this._status.vault_problem);
    }

    _openSession(session) {
        if (this._status.ui)
            openUri(`${this._status.ui}#/session/${encodeURIComponent(session.key)}`);
        else
            this._open(session.note);
    }

    _capture() {
        const text = this._entry.get_text().trim();
        if (!text)
            return;
        try {
            Gio.Subprocess.new([promptloomBinary(), 'capture', '--', text],
                Gio.SubprocessFlags.NONE);
            this._entry.set_text('');
            this.menu.close();
        } catch (e) {
            Main.notify('Promptloom', `Capture failed: ${e.message}`);
        }
    }

    destroy() {
        if (this._focusId) {
            GLib.source_remove(this._focusId);
            this._focusId = 0;
        }
        this._monitor?.cancel();
        this._monitor = null;
        this._sessions = null;
        super.destroy();
    }
});

export default class PromptloomExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
