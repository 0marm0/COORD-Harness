import SwiftUI

struct UsageDashboardView: View {
    @ObservedObject var model: CockpitModel
#if os(macOS)
    @State private var showingAccounts = false
#endif

    var body: some View {
#if os(macOS)
        UsageDashboardContent(
            state: model.usageState,
            onOpenSettings: { showingAccounts = true }
        )
        .sheet(isPresented: $showingAccounts) {
            UsageAccountSettingsView(baseURL: model.baseURL)
        }
        .navigationTitle("Usage")
#else
        UsageDashboardContent(state: model.usageState)
            .navigationTitle("Usage")
#endif
    }
}
