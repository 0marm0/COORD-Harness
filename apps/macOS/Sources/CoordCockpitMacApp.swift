import SwiftUI

@main
struct CoordCockpitMacApp: App {
    @StateObject private var model = CockpitModel()

    var body: some Scene {
        WindowGroup("Coord Cockpit", id: "cockpit") {
            MacCockpitView(model: model)
                .frame(minWidth: 920, minHeight: 620)
                .task { model.startPolling() }
        }

        MenuBarExtra {
            MenuSummaryView(model: model)
        } label: {
            Label(menuLabel, systemImage: model.snapshot?.stale == true ? "gauge.with.dots.needle.33percent" : "gauge.with.dots.needle.67percent")
        }
        .menuBarExtraStyle(.window)
    }

    private var menuLabel: String {
        guard let summary = model.snapshot?.summary else { return "Coord" }
        return "\(summary.running) running, \(summary.attention) attention"
    }
}
