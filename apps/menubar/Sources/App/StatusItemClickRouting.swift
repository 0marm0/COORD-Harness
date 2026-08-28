import CoreGraphics

enum StatusItemClickDestination: Equatable {
    case primaryPanel
}

enum StatusItemClickRouting {
    // Status-item geometry is intentionally irrelevant. Every normal click opens
    // the full panel; Stats detail is entered only from the panel's explicit row.
    static func leftClickDestination(locationX: CGFloat, buttonBounds: CGRect) -> StatusItemClickDestination {
        .primaryPanel
    }
}
