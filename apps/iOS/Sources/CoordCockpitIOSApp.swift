import SwiftUI

@main
struct CoordCockpitIOSApp: App {
    @StateObject private var model = CockpitModel()

    var body: some Scene {
        WindowGroup {
            IOSRootView(model: model)
                .task { model.startPolling() }
        }
    }
}
