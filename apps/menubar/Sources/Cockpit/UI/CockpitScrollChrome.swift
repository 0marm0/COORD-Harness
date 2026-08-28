import AppKit

final class CockpitEdgeScrollView: NSScrollView {
    private var trackingAreaRef: NSTrackingArea?
    private var hideWorkItem: DispatchWorkItem?
    private let revealInset: CGFloat = 28

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        CockpitScrollChrome.apply(to: self)
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        CockpitScrollChrome.apply(to: self)
    }

    override func updateTrackingAreas() {
        if let trackingAreaRef {
            removeTrackingArea(trackingAreaRef)
        }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.activeInKeyWindow, .mouseEnteredAndExited, .mouseMoved, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(area)
        trackingAreaRef = area
        super.updateTrackingAreas()
    }

    override func mouseMoved(with event: NSEvent) {
        super.mouseMoved(with: event)
        updateReveal(at: convert(event.locationInWindow, from: nil))
    }

    override func mouseExited(with event: NSEvent) {
        super.mouseExited(with: event)
        scheduleHide()
    }

    override func scrollWheel(with event: NSEvent) {
        setScrollerVisibility(true)
        scheduleHide(after: 0.85)
        super.scrollWheel(with: event)
    }

    private func updateReveal(at point: NSPoint) {
        let nearVertical = hasVerticalScroller && point.x >= bounds.width - revealInset
        let nearHorizontal = hasHorizontalScroller && point.y <= revealInset
        if nearVertical || nearHorizontal {
            setScrollerVisibility(true)
        } else {
            scheduleHide(after: 0.35)
        }
    }

    private func setScrollerVisibility(_ visible: Bool) {
        hideWorkItem?.cancel()
        for scroller in [verticalScroller, horizontalScroller].compactMap({ $0 }) {
            scroller.controlSize = .mini
            scroller.alphaValue = visible ? 0.34 : 0.0
            scroller.isHidden = !visible
        }
    }

    private func scheduleHide(after delay: TimeInterval = 0.55) {
        hideWorkItem?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.setScrollerVisibility(false)
        }
        hideWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }
}

enum CockpitScrollChrome {
    static func apply(to scrollView: NSScrollView) {
        scrollView.drawsBackground = false
        scrollView.borderType = .noBorder
        scrollView.autohidesScrollers = true
        scrollView.scrollerStyle = .overlay
        scrollView.verticalScroller?.controlSize = .mini
        scrollView.horizontalScroller?.controlSize = .mini
        scrollView.verticalScroller?.alphaValue = 0
        scrollView.horizontalScroller?.alphaValue = 0
        scrollView.verticalScroller?.isHidden = true
        scrollView.horizontalScroller?.isHidden = true
    }
}
