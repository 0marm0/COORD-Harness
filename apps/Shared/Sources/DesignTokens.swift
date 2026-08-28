import SwiftUI

enum DesignTokens {
    static let cornerRadius: CGFloat = 12
    static let compactSpacing: CGFloat = 8
    static let sectionSpacing: CGFloat = 16
    static let pagePadding: CGFloat = 20

    static func statusColor(for status: String) -> Color {
        switch status.lowercased() {
        case "running", "active": .blue
        case "attention", "blocked", "failed": .orange
        case "done", "complete", "completed": .green
        default: .secondary
        }
    }
}

struct StatusBadge: View {
    let status: String

    var body: some View {
        Text(status)
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .foregroundStyle(DesignTokens.statusColor(for: status))
            .background(DesignTokens.statusColor(for: status).opacity(0.12), in: Capsule())
    }
}

struct EmptySnapshotView: View {
    let message: String

    var body: some View {
        ContentUnavailableView(
            "No snapshot yet",
            systemImage: "rectangle.stack",
            description: Text(message)
        )
    }
}
