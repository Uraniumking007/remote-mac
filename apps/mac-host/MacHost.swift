import AppKit
import CoreGraphics
import Security

final class HostApp: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private var status: NSMenuItem!
    private var host: Process?

    private struct HostConfiguration: Decodable {
        let address: String
        let python: String
    }
    private let state = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Remote Mac Host")

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Remote Mac"
        let menu = NSMenu()
        status = menu.addItem(withTitle: "Starting…", action: nil, keyEquivalent: "")
        for (title, action) in [
            ("Screen Recording Settings…", #selector(screenSettings)),
            ("Accessibility Settings…", #selector(accessibilitySettings)),
            ("Pair Windows PC…", #selector(pairWindows)),
            ("Revoke Windows PC…", #selector(revokeWindows)),
            ("Open Host Log", #selector(openLog)),
            ("Quit Remote Mac Host", #selector(quit))
        ] {
            let entry = menu.addItem(withTitle: title, action: action, keyEquivalent: "")
            entry.target = self
        }
        item.menu = menu
        if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
        if !CGPreflightPostEventAccess() { _ = CGRequestPostEventAccess() }
        do { try startHost() } catch { status.title = error.localizedDescription }
    }

    private func startHost() throws {
        guard let resources = Bundle.main.resourceURL, let executables = Bundle.main.executableURL else { return }
        let bin = executables.deletingLastPathComponent()
        let keyFile = state.appendingPathComponent("device-key")
        guard FileManager.default.fileExists(atPath: keyFile.path) else {
            status.title = "No paired PC · Pair Windows PC…"
            return
        }
        let peer = try String(contentsOf: state.appendingPathComponent("allowed-viewer"), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let log = state.appendingPathComponent("host.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        let output = try FileHandle(forWritingTo: log)
        let process = Process()
        let configURL = resources.appendingPathComponent("host-config.json")
        let config = try JSONDecoder().decode(HostConfiguration.self, from: Data(contentsOf: configURL))
        process.executableURL = URL(fileURLWithPath: config.python)
        process.arguments = [
            "-u", resources.appendingPathComponent("stream.py").path, config.address, peer,
            "--lan-cert", state.appendingPathComponent("tls.crt").path,
            "--lan-key", state.appendingPathComponent("tls.key").path,
            "--device-key-file", keyFile.path,
            "--capture", bin.appendingPathComponent("capture").path,
            "--control", bin.appendingPathComponent("control").path
        ]
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { [weak self] child in
            DispatchQueue.main.async {
                if self?.host === child { self?.status.title = "Stopped (\(child.terminationStatus)) · Open Host Log" }
            }
        }
        try process.run()
        host = process
        status.title = "Listening on \(config.address)"
    }

    private func stopHost() {
        let child = host
        host = nil
        if child?.isRunning == true {
            child?.interrupt()
            child?.waitUntilExit()
        }
    }

    @objc private func pairWindows() {
        NSApp.activate()
        let prompt = NSAlert()
        prompt.messageText = "Pair a Windows PC"
        prompt.informativeText = "This replaces the existing pairing and disconnects any active viewer."
        let address = NSTextField(string: (try? String(contentsOf: state.appendingPathComponent("allowed-viewer"),
                                                        encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)) ?? "")
        address.frame = NSRect(x: 0, y: 0, width: 280, height: 24)
        address.placeholderString = "Windows LAN IPv4 address"
        prompt.accessoryView = address
        prompt.addButton(withTitle: "Approve PC")
        prompt.addButton(withTitle: "Cancel")
        guard prompt.runModal() == .alertFirstButtonReturn else { return }
        let peer = address.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        var ipv4 = in_addr()
        guard peer.withCString({ inet_pton(AF_INET, $0, &ipv4) }) == 1 else {
            status.title = "Enter a valid Windows LAN IPv4 address"
            return
        }
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { return }
        let token = bytes.map { String(format: "%02x", $0) }.joined()
        guard ["tls.crt", "tls.key"].allSatisfy({
            FileManager.default.fileExists(atPath: state.appendingPathComponent($0).path)
        }) else {
            status.title = "Missing LAN certificate · Run setup-mac-lan.sh"
            return
        }
        stopHost()
        let keyFile = state.appendingPathComponent("device-key")
        do {
            try? FileManager.default.removeItem(at: keyFile)
            try peer.write(to: state.appendingPathComponent("allowed-viewer"), atomically: true, encoding: .utf8)
            guard FileManager.default.createFile(atPath: keyFile.path, contents: Data(token.utf8),
                                                  attributes: [.posixPermissions: 0o600]) else {
                throw CocoaError(.fileWriteUnknown)
            }
            try startHost()
        } catch {
            status.title = error.localizedDescription
            return
        }
        let result = NSAlert()
        result.messageText = "Enter this pairing key on Windows"
        let field = NSTextField(labelWithString: token)
        field.isSelectable = true
        field.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        field.frame = NSRect(x: 0, y: 0, width: 440, height: 24)
        result.accessoryView = field
        result.addButton(withTitle: "Done")
        result.runModal()
    }

    @objc private func revokeWindows() {
        NSApp.activate()
        let prompt = NSAlert()
        prompt.messageText = "Revoke the paired Windows PC?"
        prompt.informativeText = "This ends the active session and requires local approval to reconnect."
        prompt.addButton(withTitle: "Revoke")
        prompt.addButton(withTitle: "Cancel")
        guard prompt.runModal() == .alertFirstButtonReturn else { return }
        stopHost()
        let keyFile = state.appendingPathComponent("device-key")
        do {
            if FileManager.default.fileExists(atPath: keyFile.path) { try FileManager.default.removeItem(at: keyFile) }
            status.title = "No paired PC · Pair Windows PC…"
        } catch { status.title = error.localizedDescription }
    }

    @objc private func screenSettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
    }

    @objc private func accessibilitySettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }

    @objc private func openLog() { NSWorkspace.shared.open(state.appendingPathComponent("host.log")) }
    @objc private func quit() { NSApp.terminate(nil) }

    func applicationWillTerminate(_ notification: Notification) {
        if host?.isRunning == true { host?.interrupt() }
    }
}

let app = NSApplication.shared
let delegate = HostApp()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
