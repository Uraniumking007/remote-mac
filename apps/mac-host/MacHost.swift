import AppKit
import CoreGraphics
import Darwin
import Security

final class HostApp: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var item: NSStatusItem!
    private var status: NSMenuItem!
    private var permissionsItem: NSMenuItem!
    private var pairingItem: NSMenuItem!
    private var screenItem: NSMenuItem!
    private var accessibilityItem: NSMenuItem!
    private var revokeItem: NSMenuItem!
    private var host: Process?
    private var listenAddress: String?
    private var pythonPath: String?

    private struct HostConfiguration: Decodable {
        let address: String?
        let python: String?
    }

    private struct HostFailure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    private let state = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Remote Mac Host")

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Remote Mac"
        if let icon = Bundle.main.url(forResource: "RemoteMac", withExtension: "icns"),
           let image = NSImage(contentsOf: icon) {
            image.size = NSSize(width: 19, height: 19)
            item.button?.image = image
            item.button?.imagePosition = .imageLeading
        }
        installMenu()
        if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
        if !CGPreflightPostEventAccess() { _ = CGRequestPostEventAccess() }
        do { try startHost() } catch { status.title = error.localizedDescription }
        refreshConnectionMenu()
    }

    private func disabledItem(_ title: String) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        entry.isEnabled = false
        return entry
    }

    private func actionItem(_ title: String, _ action: Selector) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = self
        return entry
    }

    private func installMenu() {
        let menu = NSMenu()
        menu.delegate = self
        status = menu.addItem(withTitle: "Starting…", action: nil, keyEquivalent: "")
        permissionsItem = disabledItem("Checking permissions…")
        menu.addItem(permissionsItem)
        pairingItem = disabledItem("Checking pairing…")
        menu.addItem(pairingItem)
        menu.addItem(.separator())
        menu.addItem(disabledItem("Permissions"))
        screenItem = actionItem("Screen Recording Settings…", #selector(screenSettings))
        menu.addItem(screenItem)
        accessibilityItem = actionItem("Accessibility Settings…", #selector(accessibilitySettings))
        menu.addItem(accessibilityItem)
        menu.addItem(.separator())
        menu.addItem(disabledItem("Connection"))
        menu.addItem(actionItem("Copy host address", #selector(copyHostAddress)))
        menu.addItem(actionItem("Copy certificate fingerprint", #selector(copyFingerprint)))
        menu.addItem(actionItem("Pair Windows PC…", #selector(pairWindows)))
        menu.addItem(actionItem("Show paired key…", #selector(showPairedKey)))
        revokeItem = actionItem("Revoke Windows PC…", #selector(revokeWindows))
        menu.addItem(revokeItem)
        menu.addItem(.separator())
        menu.addItem(disabledItem("Recovery"))
        menu.addItem(actionItem("Open Host Log", #selector(openLog)))
        menu.addItem(actionItem("Quit Remote Mac Host", #selector(quit)))
        item.menu = menu
        refreshConnectionMenu()
    }

    func menuWillOpen(_ menu: NSMenu) {
        refreshConnectionMenu()
    }

    private func pairedPeerAddress() -> String? {
        let keyFile = state.appendingPathComponent("device-key")
        guard FileManager.default.fileExists(atPath: keyFile.path) else { return nil }
        let peer = (try? String(contentsOf: state.appendingPathComponent("allowed-viewer"), encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return peer.isEmpty ? "approved PC" : peer
    }

    private func refreshConnectionMenu() {
        guard permissionsItem != nil else { return }
        let screen = CGPreflightScreenCaptureAccess()
        let accessibility = CGPreflightPostEventAccess()
        permissionsItem.title = "Screen Recording \(screen ? "allowed" : "needed") · Accessibility \(accessibility ? "allowed" : "needed")"
        screenItem.title = screen ? "Screen Recording: Allowed" : "Screen Recording: Needed — Open Settings…"
        accessibilityItem.title = accessibility ? "Accessibility: Allowed" : "Accessibility: Needed — Open Settings…"
        if let peer = pairedPeerAddress() {
            pairingItem.title = "Paired PC \(peer)"
            revokeItem.isEnabled = true
        } else {
            pairingItem.title = "No Windows PC paired"
            revokeItem.isEnabled = false
        }
    }

    private func startHost() throws {
        try bootstrap()
        guard let pythonPath, let listenAddress else { return }
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
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [
            "-B", "-u", resources.appendingPathComponent("stream.py").path, listenAddress, peer,
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
        status.title = "Listening on \(listenAddress)"
    }

    private func bootstrap() throws {
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        try ensureTLS()
        let config = try loadConfig()
        pythonPath = try resolvePython(config.python)
        if listenAddress == nil {
            listenAddress = try resolveAddress(config.address)
        }
    }

    private func loadConfig() throws -> HostConfiguration {
        guard let resources = Bundle.main.resourceURL else { return HostConfiguration(address: nil, python: nil) }
        let url = resources.appendingPathComponent("host-config.json")
        guard FileManager.default.fileExists(atPath: url.path) else { return HostConfiguration(address: nil, python: nil) }
        return try JSONDecoder().decode(HostConfiguration.self, from: Data(contentsOf: url))
    }

    private func isFile(_ url: URL) -> Bool {
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) && !isDirectory.boolValue
    }

    private func ensureTLS() throws {
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        let certificate = state.appendingPathComponent("tls.crt")
        let key = state.appendingPathComponent("tls.key")
        let certificateExists = isFile(certificate)
        let keyExists = isFile(key)
        if certificateExists && keyExists { return }
        if certificateExists || keyExists {
            throw HostFailure(message: "Incomplete LAN certificate. Restore tls.crt and tls.key, or remove both and reopen.")
        }
        let openssl = "/usr/bin/openssl"
        guard FileManager.default.isExecutableFile(atPath: openssl) else {
            throw HostFailure(message: "OpenSSL is missing. Run scripts/setup-mac-lan.sh to create the LAN certificate.")
        }
        let stage = state.appendingPathComponent(".tls-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: stage, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: stage) }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: openssl)
        process.arguments = [
            "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
            "-days", "365", "-subj", "/CN=remote-mac-lan",
            "-keyout", stage.appendingPathComponent("tls.key").path,
            "-out", stage.appendingPathComponent("tls.crt").path
        ]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw HostFailure(message: "Could not create the LAN certificate. Run scripts/setup-mac-lan.sh.")
        }
        let stagedKey = stage.appendingPathComponent("tls.key")
        let stagedCertificate = stage.appendingPathComponent("tls.crt")
        try FileManager.default.moveItem(at: stagedKey, to: key)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: key.path)
        try FileManager.default.moveItem(at: stagedCertificate, to: certificate)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: certificate.path)
    }

    private func pythonSupportsTLS(_ path: String) -> Bool {
        guard FileManager.default.isExecutableFile(atPath: path) else { return false }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = [
            "-B", "-c",
            "import ssl,sys; raise SystemExit(0 if sys.version_info>=(3,10) and ssl.HAS_TLSv1_3 else 1)"
        ]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }

    private func resolvePython(_ configured: String?) throws -> String {
        if let configured, !configured.isEmpty {
            if pythonSupportsTLS(configured) { return configured }
            throw HostFailure(message: "Configured Python lacks TLS 1.3: \(configured)")
        }
        var candidates: [String] = []
        let manager = FileManager.default
        for directory in ["/opt/homebrew/bin", "/usr/local/bin"] {
            guard let names = try? manager.contentsOfDirectory(atPath: directory) else { continue }
            let matches = names.filter { name in
                name == "python3" || name.range(of: #"^python3\.(1[0-9]|[2-9][0-9])$"#, options: .regularExpression) != nil
            }.sorted { pythonRank($0) > pythonRank($1) }
            candidates.append(contentsOf: matches.map { directory + "/" + $0 })
        }
        let versions = "/Library/Frameworks/Python.framework/Versions"
        if let names = try? manager.contentsOfDirectory(atPath: versions) {
            for name in names.sorted(by: >) where name != "Current" {
                candidates.append("\(versions)/\(name)/bin/python3")
            }
        }
        candidates.append("\(versions)/Current/bin/python3")
        candidates.append("/usr/bin/python3")
        if let match = candidates.first(where: { pythonSupportsTLS($0) }) { return match }
        throw HostFailure(message: "Python 3.10 or newer with TLS 1.3 is required. Install Python and reopen Remote Mac Host.")
    }

    private func pythonRank(_ name: String) -> Int {
        guard name != "python3" else { return 0 }
        return Int(name.dropFirst("python3.".count)) ?? 0
    }

    private func isPrivateLAN(_ text: String) -> Bool {
        let parts = text.split(separator: ".").compactMap { UInt8($0) }
        guard parts.count == 4 else { return false }
        if parts[0] == 10 { return true }
        if parts[0] == 172 && (16...31).contains(parts[1]) { return true }
        return parts[0] == 192 && parts[1] == 168
    }

    private func lanAddresses() -> [String] {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let head else { return [] }
        defer { freeifaddrs(head) }
        var found: [String] = []
        var cursor: UnsafeMutablePointer<ifaddrs>? = head
        while let interface = cursor {
            let flags = interface.pointee.ifa_flags
            if let sa = interface.pointee.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET),
               flags & UInt32(IFF_UP) != 0, flags & UInt32(IFF_LOOPBACK) == 0 {
                var addr = sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
                var buffer = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                if inet_ntop(AF_INET, &addr, &buffer, socklen_t(INET_ADDRSTRLEN)) != nil {
                    let text = String(cString: buffer)
                    if isPrivateLAN(text) && !found.contains(text) { found.append(text) }
                }
            }
            cursor = interface.pointee.ifa_next
        }
        return found
    }

    private func resolveAddress(_ configured: String?) throws -> String {
        if let configured {
            let address = configured.trimmingCharacters(in: .whitespacesAndNewlines)
            if !address.isEmpty {
                guard isPrivateLAN(address) else {
                    throw HostFailure(message: "Configured host address is not a private LAN IPv4 address.")
                }
                return address
            }
        }
        let available = lanAddresses()
        let savedURL = state.appendingPathComponent("listen-address")
        if let saved = try? String(contentsOf: savedURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines), available.contains(saved) {
            return saved
        }
        let choice: String
        if available.count == 1 {
            choice = available[0]
        } else if available.isEmpty {
            throw HostFailure(message: "No private LAN IPv4 address found. Connect to a network and reopen.")
        } else {
            choice = try promptAddress(available)
        }
        try choice.write(to: savedURL, atomically: true, encoding: .utf8)
        return choice
    }

    private func promptAddress(_ addresses: [String]) throws -> String {
        let alert = NSAlert()
        alert.messageText = "Choose the Mac's LAN address"
        alert.informativeText = "Windows uses this address to reach this Mac. The pairing key is chosen separately."
        let popup = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        addresses.forEach { popup.addItem(withTitle: $0) }
        alert.accessoryView = popup
        alert.addButton(withTitle: "Use address")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate()
        guard alert.runModal() == .alertFirstButtonReturn, let title = popup.titleOfSelectedItem else {
            throw HostFailure(message: "Choose a LAN address to share with Windows.")
        }
        return title
    }

    private func certificateFingerprint() throws -> String {
        let certificate = state.appendingPathComponent("tls.crt")
        guard isFile(certificate) else { throw HostFailure(message: "LAN certificate is missing.") }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/openssl")
        process.arguments = ["x509", "-in", certificate.path, "-noout", "-fingerprint", "-sha256"]
        process.standardInput = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output
        process.standardError = Pipe()
        try process.run()
        process.waitUntilExit()
        let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard process.terminationStatus == 0, let separator = text.firstIndex(of: "=") else {
            throw HostFailure(message: "Could not read the certificate fingerprint.")
        }
        let value = text[text.index(after: separator)...].trimmingCharacters(in: .whitespacesAndNewlines)
        let hex = CharacterSet(charactersIn: "0123456789abcdefABCDEF:")
        guard value.unicodeScalars.allSatisfy({ hex.contains($0) }), value.split(separator: ":").count == 32 else {
            throw HostFailure(message: "Could not read the certificate fingerprint.")
        }
        return value
    }

    private func showNotice(_ message: String) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
    }

    private func presentShare(_ title: String, _ value: String, copyTitle: String) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = "Share this with the Windows PC. The pairing key stays under Pair Windows PC… and Show paired key…."
        let field = NSTextField(labelWithString: value)
        field.isSelectable = true
        field.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        field.frame = NSRect(x: 0, y: 0, width: 460, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: copyTitle)
        alert.addButton(withTitle: "Done")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let clipboard = NSPasteboard.general
        clipboard.clearContents()
        if clipboard.setString(value, forType: .string) {
            showNotice("Copied. Paste it into the Windows viewer.")
        } else {
            showNotice("Could not copy. Select the value and copy it manually.")
        }
    }

    @objc private func copyHostAddress() {
        do {
            try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
            if listenAddress == nil { listenAddress = try resolveAddress(try loadConfig().address) }
            guard let listenAddress else { return }
            presentShare("Mac LAN address", listenAddress, copyTitle: "Copy host address")
        } catch {
            showNotice(error.localizedDescription)
        }
    }

    @objc private func copyFingerprint() {
        do {
            try ensureTLS()
            presentShare("Certificate fingerprint", try certificateFingerprint(), copyTitle: "Copy certificate fingerprint")
        } catch {
            showNotice(error.localizedDescription)
        }
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
        do { try bootstrap() } catch {
            status.title = error.localizedDescription
            return
        }
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
        guard ["tls.crt", "tls.key"].allSatisfy({ isFile(state.appendingPathComponent($0)) }) else {
            status.title = "LAN certificate is missing. Reopen Remote Mac Host or run scripts/setup-mac-lan.sh."
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
        presentPairingKey(token)
    }

    @objc private func showPairedKey() {
        NSApp.activate()
        let keyFile = state.appendingPathComponent("device-key")
        guard let key = try? String(contentsOf: keyFile, encoding: .utf8) else {
            let alert = NSAlert()
            alert.messageText = "No Windows PC is paired"
            alert.informativeText = "Choose Pair Windows PC… to approve a PC."
            alert.runModal()
            return
        }
        presentPairingKey(key.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private func presentPairingKey(_ key: String) {
        let alert = NSAlert()
        alert.messageText = "Enter this pairing key on Windows"
        alert.informativeText = "Transfer the key privately to your approved PC."
        let field = NSTextField(labelWithString: key)
        field.isSelectable = true
        field.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        field.frame = NSRect(x: 0, y: 0, width: 460, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Copy pairing key")
        alert.addButton(withTitle: "Done")
        if alert.runModal() == .alertFirstButtonReturn {
            let clipboard = NSPasteboard.general
            clipboard.clearContents()
            if clipboard.setString(key, forType: .string) {
                showNotice("Copied the pairing key.")
            } else {
                let error = NSAlert()
                error.messageText = "Could not copy the pairing key"
                error.informativeText = "Open Show paired key… to select and copy it manually."
                error.runModal()
            }
        }
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
