import SwiftUI

/// Empty host app. An XCUITest bundle needs an app target to hang off; the bridge drives the REAL app by bundle id.
@main struct QABridgeHostApp: App {
    var body: some Scene { WindowGroup { Text("QABridge host") } }
}
