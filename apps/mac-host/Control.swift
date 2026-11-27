import AppKit
import CoreGraphics

guard CGPreflightPostEventAccess() || CGRequestPostEventAccess() else {
    fputs("Grant Accessibility permission to the control helper and reconnect.\n", stderr)
    exit(1)
}

var keys = Set<UInt16>()
var buttons = Set<Int>()
var position = CGPoint.zero
var targetDisplay = CGMainDisplayID()
let modifierKeys: [UInt16: CGEventFlags] = [
    54: .maskCommand, 55: .maskCommand, 56: .maskShift, 60: .maskShift,
    58: .maskAlternate, 61: .maskAlternate, 59: .maskControl, 62: .maskControl,
    // Virtual key 63 is Fn. Posting it does not perform Globe gestures.
    63: .maskSecondaryFn
]

let keyboardSource = CGEventSource(stateID: .hidSystemState)
let deviceModifierBits: [UInt16: UInt64] = [
    54: 0x10, 55: 0x08, 56: 0x02, 60: 0x04,
    58: 0x20, 61: 0x40, 59: 0x01, 62: 0x2000
]
let managedModifiers = CGEventFlags(rawValue: 0x001e207f | CGEventFlags.maskSecondaryFn.rawValue)

func key(_ code: UInt16, down: Bool) {
    let repeated = down && keys.contains(code)
    if down { keys.insert(code) } else { keys.remove(code) }
    let event = CGEvent(keyboardEventSource: keyboardSource, virtualKey: code, keyDown: down)
    if modifierKeys[code] != nil { event?.type = .flagsChanged }
    let baseFlags = (event?.flags ?? []).subtracting(managedModifiers)
    event?.flags = keys.reduce(into: baseFlags) { flags, held in
        if let modifier = modifierKeys[held] { flags.insert(modifier) }
        if let bits = deviceModifierBits[held] { flags.insert(CGEventFlags(rawValue: bits)) }
    }
    if repeated { event?.setIntegerValueField(.keyboardEventAutorepeat, value: 1) }
    event?.post(tap: .cghidEventTap)
}

func mouse(_ button: Int, _ type: CGEventType, clicks: Int = 0) {
    guard let id = CGMouseButton(rawValue: UInt32(button)) else { return }
    let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: position, mouseButton: id)
    event?.flags = keys.reduce(into: CGEventFlags()) { flags, held in
        if let modifier = modifierKeys[held] { flags.insert(modifier) }
    }
    if clicks > 0 { event?.setIntegerValueField(.mouseEventClickState, value: Int64(clicks)) }
    event?.post(tap: .cghidEventTap)
}

let downTypes: [CGEventType] = [.leftMouseDown, .rightMouseDown, .otherMouseDown]
let upTypes: [CGEventType] = [.leftMouseUp, .rightMouseUp, .otherMouseUp]
let dragTypes: [CGEventType] = [.leftMouseDragged, .rightMouseDragged, .otherMouseDragged]

func release() {
    for button in Array(buttons) { mouse(button, upTypes[button]) }
    buttons.removeAll()
    for code in Array(keys) { key(code, down: false) }
}

print("ready")
fflush(stdout)
while let line = readLine(), let data = line.data(using: .utf8) {
    guard let event = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let kind = event["kind"] as? String else { continue }
    switch kind {
    case "display-mode":
        guard let mode = event["mode"] as? String, ["mirror", "extend", "primary"].contains(mode) else {
            continue
        }
        release()
        let display = mode.withCString { RemoteMacSetDisplay($0) }
        if display != 0 { targetDisplay = display }
        print("{\"display\":\(display)}")
        fflush(stdout)
    case "key":
        if let code = event["code"] as? UInt16, code < 128, let down = event["down"] as? Bool {
            key(code, down: down)
        }
    case "move", "button":
        guard let x = event["x"] as? Double, let y = event["y"] as? Double,
              (0...1).contains(x), (0...1).contains(y) else { continue }
        let bounds = CGDisplayBounds(targetDisplay)
        guard !bounds.isEmpty else { continue }
        position = CGPoint(x: bounds.minX + x * (bounds.width - 1),
                           y: bounds.minY + y * (bounds.height - 1))
        if kind == "move" {
            if let button = buttons.min() { mouse(button, dragTypes[button]) }
            else { mouse(0, .mouseMoved) }
        } else if let button = event["button"] as? Int, (0...2).contains(button),
                  let down = event["down"] as? Bool {
            let clicks = min(3, max(1, event["clicks"] as? Int ?? 1))
            if down { buttons.insert(button) } else { buttons.remove(button) }
            mouse(button, down ? downTypes[button] : upTypes[button], clicks: clicks)
        }
    case "scroll":
        if let x = event["x"] as? Int32, let y = event["y"] as? Int32 {
            CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2,
                    wheel1: y, wheel2: x, wheel3: 0)?.post(tap: .cghidEventTap)
        }
    case "release":
        release()
    case "clipboard-set":
        if let value = event["text"] as? String {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(value, forType: .string)
        }
    case "clipboard-get":
        let value = NSPasteboard.general.string(forType: .string) ?? ""
        let reply = ["text": value.utf8.count <= 128_000 ? value : ""]
        if let encoded = try? JSONSerialization.data(withJSONObject: reply),
           let line = String(data: encoded, encoding: .utf8) {
            print(line)
            fflush(stdout)
        }
    default:
        break
    }
}
release()
RemoteMacRemoveDisplay()
