// Menu bar indicator for promptloom. Like the GNOME extension, it only reads
// the status file the daemon writes and shells out to `promptloom capture`.

import AppKit
import SwiftUI

private let menuBarTitleChars = 32

@MainActor
final class StatusModel: ObservableObject {
    @Published var status = Status()
    @Published var now = Date()
    private var timer: Timer?
    private var lastModified: Date?

    init() {
        reload()
        // The daemon replaces the file atomically, which file watchers lose track
        // of; a cheap stat every two seconds is simpler and just as quick.
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.reload() }
        }
    }

    func reload() {
        now = Date()
        let attrs = try? FileManager.default.attributesOfItem(atPath: Paths.statusFile.path)
        let modified = attrs?[.modificationDate] as? Date
        if modified != nil && modified == lastModified { return }
        // Missing or mid-write: keep showing the last good status.
        if let loaded = loadStatus() {
            status = loaded
            lastModified = modified
        }
    }
}

@main
struct PromptloomApp: App {
    @StateObject private var model = StatusModel()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model)
        } label: {
            if let title = model.status.active?.title, !title.isEmpty {
                Text(Format.clip(title, menuBarTitleChars))
            } else {
                Image(systemName: "text.bubble")
            }
        }
        .menuBarExtraStyle(.menu)
    }
}

struct MenuContent: View {
    @ObservedObject var model: StatusModel

    var body: some View {
        let status = model.status
        let active = status.active
        let recent = (status.recent ?? []).filter { $0.key == nil || $0.key != active?.key }

        Button("Quick capture…") { Actions.capture(todo: false) }
            .keyboardShortcut("n")
        Button("Add to-do…") { Actions.capture(todo: true) }
            .keyboardShortcut("t")

        Divider()

        if let active = active {
            Text("Active")
            SessionButton(session: active, active: true, status: status)
        }
        if !recent.isEmpty {
            Text("Recent")
            ForEach(recent) { session in
                SessionButton(session: session, active: false, status: status)
            }
        }
        if active == nil && recent.isEmpty {
            Text(status.updated == nil ? "Waiting for the promptloom daemon…" : "No sessions captured yet")
        }

        Divider()

        if Format.uiBase(status.ui) != nil {
            Button("New prompt…") { Actions.openApp("compose") }
                .keyboardShortcut("n")
            Button("Open Promptloom") { Actions.openApp("") }
                .keyboardShortcut("o")
            Button("Search…") { Actions.openApp("search") }
                .keyboardShortcut("f")
        }
        if let vault = status.vault, !vault.isEmpty {
            Menu("Open in Obsidian") {
                Button("Action items (\(status.open_actions ?? 0) open)") {
                    Actions.obsidian(status.actions, problem: status.vault_problem)
                }
                Button("Connections") { Actions.obsidian(status.connections, problem: status.vault_problem) }
                Button("Insights") { Actions.obsidian(status.insights, problem: status.vault_problem) }
                Button("Vault home") { Actions.obsidian(status.home, problem: status.vault_problem) }
            }
        }
        if let id = active?.session_id, !id.isEmpty {
            Button("Copy active session ID") { Actions.copy(id) }
        }

        let notes = Self.notes(status)
        if !notes.isEmpty {
            Divider()
            ForEach(notes, id: \.self) { note in
                Text(Format.clip(note, 90))
            }
        }

        Divider()
        Button("Quit Promptloom menu") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    static func notes(_ status: Status) -> [String] {
        var notes: [String] = []
        if let problem = status.vault_problem, !problem.isEmpty { notes.append(problem) }
        if let waiting = status.waiting, !waiting.isEmpty {
            notes.append("Summaries paused: \(waiting)")
        } else if let pending = status.summarising, pending > 0 {
            notes.append("\(pending) sessions awaiting a summary")
        }
        notes += status.errors ?? []
        return notes
    }
}

struct SessionButton: View {
    let session: SessionEntry
    let active: Bool
    let status: Status

    var body: some View {
        Button {
            Actions.openSession(session, status: status)
        } label: {
            // A menu item shows the first Text as its title and the second beneath it
            // on macOS 14; on 13 both run together on one line, which still reads fine.
            Text(Format.clip(session.title ?? "Untitled session", 60))
            Text(Format.meta(session, active: active))
        }
    }
}

@MainActor
enum Actions {
    static func open(_ link: String) {
        guard let url = URL(string: link) else { return }
        NSWorkspace.shared.open(url)
    }

    /// The app in its own window (`promptloom app PAGE`); the browser if that fails.
    static func openApp(_ page: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: Paths.promptloomBinary())
        process.arguments = ["app"] + (page.isEmpty ? [] : [page])
        do {
            try process.run()
        } catch {
            open("http://127.0.0.1:8765/" + (page.isEmpty ? "" : "#/" + page))
        }
    }

    static func openSession(_ session: SessionEntry, status: Status) {
        if Format.uiBase(status.ui) != nil, let key = session.key {
            openApp("session/" + Format.uriComponent(key))
        } else {
            obsidian(session.note, problem: status.vault_problem)
        }
    }

    static func obsidian(_ path: String?, problem: String?) {
        guard let path = path, !path.isEmpty else { return }
        if let problem = problem, !problem.isEmpty {
            // Obsidian would only answer with its "Vault not found" dialog.
            notify(problem)
            return
        }
        open("obsidian://open?path=" + Format.uriComponent(path))
    }

    static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    static func capture(todo: Bool) {
        // A menu bar app is never frontmost on its own; without this the alert
        // opens behind other windows.
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = todo ? "Add a to-do" : "Quick capture"
        alert.informativeText = todo
            ? "Saved as an action item against the active session."
            : "Saved against the active session. Start with \"todo:\" to make it an action item."
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.placeholderString = todo ? "What needs doing" : "A note, or \"todo …\""
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let text = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: Paths.promptloomBinary())
        process.arguments = ["capture"] + (todo ? ["--todo"] : []) + ["--", text]
        do {
            try process.run()
        } catch {
            notify("Capture failed: \(error.localizedDescription)")
        }
    }

    static func notify(_ message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Promptloom"
        alert.informativeText = message
        alert.runModal()
    }
}
