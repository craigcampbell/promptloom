// The status file the promptloom daemon writes, and helpers that need only
// Foundation. Every field is optional: an older or newer daemon may leave some out.

import Foundation

struct SessionEntry: Decodable, Identifiable {
    var key: String?
    var title: String?
    var harness_label: String?
    var project: String?
    var updated: String?
    var session_id: String?
    var note: String?
    var model: String?

    var id: String { key ?? session_id ?? UUID().uuidString }
}

struct Status: Decodable {
    var updated: String?
    var active: SessionEntry?
    var recent: [SessionEntry]?
    var sessions: Int?
    var open_actions: Int?
    var summarising: Int?
    var waiting: String?
    var ui: String?
    var vault: String?
    var vault_problem: String?
    var home: String?
    var actions: String?
    var insights: String?
    var inbox: String?
    var connections: String?
    var map: String?
    var errors: [String]?
}

enum Paths {
    static var stateDir: URL {
        let env = ProcessInfo.processInfo.environment
        if let xdg = env["XDG_STATE_HOME"], !xdg.isEmpty {
            return URL(fileURLWithPath: xdg).appendingPathComponent("promptloom")
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/promptloom")
    }

    static var statusFile: URL { stateDir.appendingPathComponent("status.json") }

    /// `promptloom` on PATH, else where uv and pipx put it.
    static func promptloomBinary() -> String {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser.path
        // Apps launched from Finder get a minimal PATH, so look in the usual places too.
        var dirs = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        dirs += ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
        for dir in dirs {
            let path = "\(dir)/promptloom"
            if fm.isExecutableFile(atPath: path) {
                return path
            }
        }
        return "\(home)/.local/bin/promptloom"
    }
}

enum Format {
    static func clip(_ text: String, _ limit: Int) -> String {
        text.count > limit ? String(text.prefix(limit - 1)) + "…" : text
    }

    static func date(_ iso: String?) -> Date? {
        guard let iso = iso, !iso.isEmpty else { return nil }
        let plain = ISO8601DateFormatter()
        if let date = plain.date(from: iso) {
            return date
        }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: iso)
    }

    static func ago(_ iso: String?, now: Date = Date()) -> String {
        guard let then = date(iso) else { return "" }
        let minutes = max(0, Int((now.timeIntervalSince(then) / 60).rounded()))
        if minutes < 1 { return "now" }
        if minutes < 60 { return "\(minutes)m ago" }
        if minutes < 60 * 24 { return "\(Int((Double(minutes) / 60).rounded()))h ago" }
        return "\(Int((Double(minutes) / (60 * 24)).rounded()))d ago"
    }

    static func meta(_ session: SessionEntry, active: Bool) -> String {
        var parts = [session.harness_label, session.model, session.project, ago(session.updated)]
        if active { parts.append(session.session_id) }
        return parts.compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// Same set of characters JavaScript's encodeURIComponent leaves alone.
    static func uriComponent(_ text: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-_.!~*'()")
        return text.addingPercentEncoding(withAllowedCharacters: allowed) ?? text
    }

    /// The built-in UI's base URL, with a trailing slash, or nil when the daemon serves none.
    static func uiBase(_ ui: String?) -> String? {
        guard let ui = ui, !ui.isEmpty else { return nil }
        return ui.hasSuffix("/") ? ui : ui + "/"
    }
}

func loadStatus(from url: URL = Paths.statusFile) -> Status? {
    guard let data = try? Data(contentsOf: url) else { return nil }
    return try? JSONDecoder().decode(Status.self, from: data)
}
