import XCTest
import Foundation
import Darwin

/// One source for iOS and tvOS, built by bridge/project.yml (xcodegen) via bridge/build.sh.
/// Drives an already-installed app by bundle id, so it needs no edits to the app's own Xcode project.
/// NOT a regression test. A long-lived command server so the simdecision runtime
/// can drive the real app through XCUITest: read the accessibility tree,
/// press Siri Remote buttons, launch / terminate, screenshot. XCUITest is the only sanctioned way to drive
/// the tvOS simulator (never osascript keystrokes - see TVOS-FOCUS-CHECKLIST.md).
///
/// Skips unless QA_PORT is set, so the normal UI-test run never blocks on it. Start it with
///   TEST_RUNNER_QA_PORT=8765 xcodebuild test-without-building ... -only-testing:QABridgeUITests/QABridge/testServe
/// (xcodebuild strips the TEST_RUNNER_ prefix). One client at a time; every endpoint is a GET returning JSON.
///   /ping  /state  /launch?args=<json array>  /terminate  /activate  /screen  /key?name=up&times=1
///   /hold?name=select&secs=1.2  /type?text=...  /delete?count=N  /submit  /screenshot?path=/abs/file.png  /quit
final class QABridge: XCTestCase {
    private var bundleId = "com.apple.Preferences"    // overridden by QA_BUNDLE_ID (simdecision.yaml app.<platform>.bundle)
    private var app: XCUIApplication!
    private var launchArgs: [String] = []   // set per launch via /launch?args=[...]

    func testServe() throws {
        let env = ProcessInfo.processInfo.environment
        guard let portText = env["QA_PORT"], let port = UInt16(portText) else {
            throw XCTSkip("QABridge only runs when QA_PORT is set (it is a driver, not a test)")
        }
        continueAfterFailure = true
        bundleId = env["QA_BUNDLE_ID"] ?? bundleId
        app = XCUIApplication(bundleIdentifier: bundleId)
        let deadline = Date().addingTimeInterval(TimeInterval(env["QA_MAX_SECONDS"].flatMap(Double.init) ?? 3 * 3600))

        let server = socket(AF_INET, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(server, 0, "socket() failed")
        var yes: Int32 = 1
        setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")   // loopback only: the runtime is on the same Mac
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(server, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        XCTAssertEqual(bound, 0, "bind(\(port)) failed: \(String(cString: strerror(errno)))")
        listen(server, 4)
        // Poll accept() so the deadline and /quit are honoured without a blocked thread.
        var flags = fcntl(server, F_GETFL)
        flags |= O_NONBLOCK
        _ = fcntl(server, F_SETFL, flags)
        NSLog("QABridge listening on 127.0.0.1:%d", Int(port))

        var running = true
        while running && Date() < deadline {
            let client = accept(server, nil, nil)
            if client < 0 { Thread.sleep(forTimeInterval: 0.05); continue }
            var cflags = fcntl(client, F_GETFL)
            cflags &= ~O_NONBLOCK
            _ = fcntl(client, F_SETFL, cflags)
            var buf = [UInt8](repeating: 0, count: 16384)
            let n = read(client, &buf, buf.count)
            var body: Data
            if n > 0, let req = String(bytes: buf[0..<n], encoding: .utf8), let line = req.components(separatedBy: "\r\n").first {
                let parts = line.split(separator: " ")
                let target = parts.count > 1 ? String(parts[1]) : "/"
                let (json, quit) = handle(target)
                running = !quit
                body = json
            } else {
                body = Data("{\"ok\":false,\"error\":\"empty request\"}".utf8)
            }
            let head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
            var out = Data(head.utf8); out.append(body)
            out.withUnsafeBytes { _ = write(client, $0.baseAddress, out.count) }
            close(client)
        }
        close(server)
        NSLog("QABridge stopped")
    }

    // MARK: - routing

    private func handle(_ target: String) -> (Data, Bool) {
        let comps = URLComponents(string: "http://x" + target)
        let path = comps?.path ?? "/"
        var q: [String: String] = [:]
        for item in comps?.queryItems ?? [] { q[item.name] = item.value ?? "" }
        do {
            switch path {
            case "/ping", "/state":
                return (try json(["ok": true, "state": stateName(app.state), "bundle": bundleId]), false)
            case "/launch":
                if let raw = q["args"], let data = raw.data(using: .utf8),
                   let list = try? JSONSerialization.jsonObject(with: data) as? [String] { launchArgs = list }
                if app.state != .notRunning { app.terminate() }
                app.launchArguments = launchArgs
                app.launch()
                return (try json(["ok": true, "state": stateName(app.state)]), false)
            case "/terminate":
                app.terminate()
                return (try json(["ok": true, "state": stateName(app.state)]), false)
            case "/activate":
                app.activate()
                return (try json(["ok": true, "state": stateName(app.state)]), false)
            case "/screen":
                return (try screen(), false)
            case "/tree":   // debugging: every element of any type, with its accessibility fields
                var rows: [[String: Any]] = []
                dumpAll(try app.snapshot(), 0, &rows)
                return (try json(["ok": true, "rows": rows]), false)
#if os(tvOS)
            case "/key":
                let times = Int(q["times"] ?? "1") ?? 1
                guard let button = button(q["name"] ?? "") else { return (try json(["ok": false, "error": "unknown key \(q["name"] ?? "")"]), false) }
                for _ in 0..<max(1, times) {
                    XCUIRemote.shared.press(button)
                    Thread.sleep(forTimeInterval: Double(q["delay"] ?? "0.35") ?? 0.35)
                }
                return (try json(["ok": true]), false)
#if os(tvOS)
            case "/hold":
                guard let button = button(q["name"] ?? "") else { return (try json(["ok": false, "error": "unknown key"]), false) }
                XCUIRemote.shared.press(button, forDuration: Double(q["secs"] ?? "1.0") ?? 1.0)
                return (try json(["ok": true]), false)
#endif
#else
            case "/key":   // iOS has no remote: hardware buttons only
                switch q["name"] ?? "" {
                case "home": XCUIDevice.shared.press(.home)
                default: return (try json(["ok": false, "error": "iOS has no '\(q["name"] ?? "")' key (only home exists in the Simulator)"]), false)
                }
                return (try json(["ok": true]), false)
            case "/tap", "/double_tap", "/long_press":
                let p = point(q, "x", "y")
                if path == "/tap" { p.tap() }
                else if path == "/double_tap" { p.doubleTap() }
                else { p.press(forDuration: Double(q["secs"] ?? "0.9") ?? 0.9) }
                return (try json(["ok": true]), false)
            case "/swipe":
                let a = point(q, "x1", "y1"), b = point(q, "x2", "y2")
                a.press(forDuration: Double(q["secs"] ?? "0.05") ?? 0.05, thenDragTo: b)
                return (try json(["ok": true]), false)
            case "/rotate":
                XCUIDevice.shared.orientation = (q["to"] == "landscape") ? .landscapeLeft : .portrait
                return (try json(["ok": true]), false)
#endif
            case "/type":
                app.typeText(q["text"] ?? "")
                return (try json(["ok": true]), false)
            case "/delete":
                let n = max(1, min(500, Int(q["count"] ?? "1") ?? 1))
                app.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: n))
                return (try json(["ok": true]), false)
            case "/submit":
                app.typeText("\n")
                return (try json(["ok": true]), false)
            case "/screenshot":
                let png = XCUIScreen.main.screenshot().pngRepresentation
                if let p = q["path"] { try png.write(to: URL(fileURLWithPath: p)); return (try json(["ok": true, "path": p]), false) }
                return (try json(["ok": true, "base64": png.base64EncodedString()]), false)
            case "/quit":
                return (try json(["ok": true]), true)
            default:
                return (try json(["ok": false, "error": "unknown endpoint \(path)"]), false)
            }
        } catch {
            return ((try? json(["ok": false, "error": "\(error)"])) ?? Data(), false)
        }
    }

#if os(tvOS)
    private func button(_ name: String) -> XCUIRemote.Button? {
        switch name {
        case "up": return .up
        case "down": return .down
        case "left": return .left
        case "right": return .right
        case "select", "ok", "enter": return .select
        case "menu", "back": return .menu
        case "play_pause", "playpause": return .playPause
        case "home": return .home
        default: return nil
        }
    }
#endif

#if !os(tvOS)
    private func point(_ q: [String: String], _ kx: String, _ ky: String) -> XCUICoordinate {
        let x = Double(q[kx] ?? "0") ?? 0, y = Double(q[ky] ?? "0") ?? 0
        return app.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0)).withOffset(CGVector(dx: x, dy: y))
    }
#endif

    private func stateName(_ s: XCUIApplication.State) -> String {
        switch s {
        case .notRunning: return "notRunning"
        case .runningBackgroundSuspended: return "backgroundSuspended"
        case .runningBackground: return "background"
        case .runningForeground: return "foreground"
        default: return "unknown"
        }
    }

    // MARK: - accessibility tree

    private static let interactive: Set<XCUIElement.ElementType> = [
        .button, .cell, .textField, .secureTextField, .searchField, .switch, .slider, .link, .tab,
        .segmentedControl, .menuItem, .picker, .pickerWheel, .toggle, .stepper, .key, .keyboard, .textView,
    ]

    private func screen() throws -> Data {
        guard app.state == .runningForeground else {
            return try json(["ok": true, "state": stateName(app.state), "items": [], "texts": [], "w": 0, "h": 0])
        }
        let snap = try app.snapshot()
        var items: [[String: Any]] = [], texts: [String] = []
        walk(snap, &items, &texts)
        let f = snap.frame
        return try json(["ok": true, "state": "foreground", "w": Int(f.width), "h": Int(f.height),
                         "items": items, "texts": Array(texts.prefix(24))])
    }

    private func walk(_ el: XCUIElementSnapshot, _ items: inout [[String: Any]], _ texts: inout [String]) {
#if !os(tvOS)
        if el.elementType == .keyboard { return }   // ~40 key buttons: text goes in via /type, never by key
#endif
        let label = [el.label, el.title, el.identifier].first(where: { !$0.isEmpty }) ?? ""
        let r = el.frame
        let onScreen = r.width > 0 && r.height > 0
        if onScreen {
            if el.hasFocus || Self.interactive.contains(el.elementType) {
                let editable = [.textField, .secureTextField, .searchField, .textView].contains(el.elementType)
                items.append([
                    "label": label, "id": el.identifier, "type": Int(el.elementType.rawValue),
                    "x": Int(r.minX), "y": Int(r.minY), "w": Int(r.width), "h": Int(r.height),
                    "focused": el.hasFocus, "enabled": el.isEnabled, "editable": editable,
                    "text": editable ? ((el.value as? String) ?? "") : "",
                ])
            } else if el.elementType == .staticText, !label.isEmpty {
                texts.append(label)
            }
        }
        for child in el.children { walk(child, &items, &texts) }
    }

    private func dumpAll(_ el: XCUIElementSnapshot, _ depth: Int, _ rows: inout [[String: Any]]) {
        guard rows.count < 600 else { return }
        let r = el.frame
        rows.append(["d": depth, "type": Int(el.elementType.rawValue), "label": el.label, "id": el.identifier,
                     "value": (el.value as? String) ?? "", "ph": el.placeholderValue ?? "",
                     "x": Int(r.minX), "y": Int(r.minY), "w": Int(r.width), "h": Int(r.height), "enabled": el.isEnabled])
        for c in el.children { dumpAll(c, depth + 1, &rows) }
    }

    private func json(_ obj: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: obj, options: [])
    }
}
