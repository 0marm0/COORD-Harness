import AppKit
import XCTest

final class PowerControlTests: XCTestCase {
    func testBatteryParserPreservesSourceAndElectricalState() throws {
        let ac = #"Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t83%; charging; 0:25 remaining present: true"#
        let battery = #"Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t41%; discharging; 3:10 remaining present: true"#

        XCTAssertEqual(
            try LocalBatteryParser.parse(ac).get(),
            LocalBatterySnapshot(percent: 83, source: "Adapter", detail: "Charging", error: nil)
        )
        XCTAssertEqual(
            try LocalBatteryParser.parse(battery).get(),
            LocalBatterySnapshot(percent: 41, source: "Battery", detail: "Discharging", error: nil)
        )
    }

    func testBatteryParserFailsClosedWithoutInventingPercentage() {
        let result = LocalBatteryParser.parse("Now drawing from 'AC Power'\n no battery attached")
        XCTAssertThrowsError(try result.get()) { error in
            XCTAssertEqual(
                error as? LocalPowerFailure,
                .command("pmset did not return a valid battery percentage.")
            )
        }
    }

    func testChargeLimitParserRequiresOneExactReadback() {
        XCTAssertEqual(LocalChargeLimitParser.parse("battlimit 80"), 80)
        XCTAssertEqual(LocalChargeLimitParser.parse("Charge Limit: 100"), 100)
        XCTAssertNil(LocalChargeLimitParser.parse("No battery level limits set\n"))
        XCTAssertNil(LocalChargeLimitParser.parse("no charge setting"))
        XCTAssertNil(LocalChargeLimitParser.parse("battlimit 80\nbattlimit 100"))

        let nativeLog = """
        PowerUIAgent MCL target: 100%
        PowerUIAgent Charge limit was set to: 80
        PowerUIAgent MCL target: 80%
        """
        XCTAssertEqual(LocalNativeChargeLimitParser.parse(nativeLog), 80)
        XCTAssertEqual(
            LocalNativeChargeLimitParser.parse(
                nativeLog + "\nPowerUIAgent Charge limit was disabled"
            ),
            100
        )
    }

    func testNonCanonicalSeventyFivePercentLimitUnlocksToOneHundred() {
        let snapshot = LocalBatterySnapshot(
            percent: 75,
            source: "Adapter",
            detail: "Charging",
            error: nil,
            chargeLimit: 75,
            chargeLimitMutationAvailable: true
        )
        XCTAssertTrue(snapshot.chargeLimitEnabled)
        XCTAssertEqual(snapshot.nextChargeLimit, 100)
    }

    func testEnergyModeParserRequiresBothExactPowerSources() {
        let custom = """
        Battery Power:
         powermode            1
         sleep                3
        AC Power:
         sleep                0
         powermode            2
        """
        let modes = LocalEnergyModeParser.parse(custom)
        XCTAssertEqual(modes?.battery, LocalEnergyMode.low.rawValue)
        XCTAssertEqual(modes?.adapter, LocalEnergyMode.high.rawValue)
        XCTAssertNil(LocalEnergyModeParser.parse("Battery Power:\n powermode 0"))
        XCTAssertNil(LocalEnergyModeParser.parse("Battery Power:\n powermode 9\nAC Power:\n powermode 0"))
    }

    @MainActor
    func testEnergyModeActionPublishesOnlyVerifiedReadback() async {
        let initial = LocalBatterySnapshot(
            percent: 75,
            source: "Adapter",
            detail: "Charging",
            error: nil,
            batteryModeRaw: 1,
            adapterModeRaw: 0,
            highPowerModeSupported: true,
            energyModeMutationAvailable: true
        )
        let verified = LocalBatterySnapshot(
            percent: 75,
            source: "Adapter",
            detail: "Charging",
            error: nil,
            batteryModeRaw: 1,
            adapterModeRaw: 2,
            highPowerModeSupported: true,
            energyModeMutationAvailable: true
        )
        var request: (LocalPowerSource, LocalEnergyMode, Int, Int)?
        let controller = LocalPowerController(
            batteryReader: { .success(initial) },
            energyModeWriter: { source, mode, expected, other in
                request = (source, mode, expected, other)
                return .success(verified)
            }
        )
        let refreshed = expectation(description: "initial battery read")
        var didObserveInitialRead = false
        controller.onChange = { _, _ in
            guard !didObserveInitialRead else { return }
            didObserveInitialRead = true
            refreshed.fulfill()
        }
        controller.refreshBattery()
        await fulfillment(of: [refreshed], timeout: 2)

        let completed = expectation(description: "verified energy mode outcome")
        controller.onOutcome = { ok, _ in
            if ok { completed.fulfill() }
        }
        controller.setEnergyMode(source: .adapter, mode: .high, expected: 0)
        await fulfillment(of: [completed], timeout: 2)

        XCTAssertEqual(request?.0, .adapter)
        XCTAssertEqual(request?.1, .high)
        XCTAssertEqual(request?.2, 0)
        XCTAssertEqual(request?.3, 1)
        XCTAssertEqual(controller.battery, verified)
    }

    func testAdapterPoweredBatteryStatusTintStaysGreenAcrossChargingLabels() {
        for detail in ["Charging", "Plugged in", "Charged", "Finishing charge"] {
            let snapshot = LocalBatterySnapshot(
                percent: 80,
                source: "Adapter",
                detail: detail,
                error: nil
            )
            XCTAssertTrue(CoordBatteryStatusItemPresentation.tint(for: snapshot).isEqual(NSColor.systemGreen))
        }
        let battery = LocalBatterySnapshot(
            percent: 80,
            source: "Battery",
            detail: "Discharging",
            error: nil
        )
        XCTAssertTrue(CoordBatteryStatusItemPresentation.tint(for: battery).isEqual(NSColor.labelColor))
    }

    @MainActor
    func testHeaderPowerControlsExposeBatteryCaffeineAndThreeRealModeStops() {
        let view = CoordPowerControlsView(
            frame: NSRect(
                x: 0, y: 0,
                width: CoordPowerControlsLayout.width,
                height: CoordPowerControlsLayout.height
            ),
            battery: LocalBatterySnapshot(
                percent: 78,
                source: "Adapter",
                detail: "Charging",
                error: nil,
                chargeLimit: 80,
                chargeLimitMutationAvailable: true
            ),
            caffeineActive: true,
            mode: "medium"
        )
        var batteryRequests: [(Int, Int)] = []
        var caffeineClicks = 0
        var modes: [String] = []
        view.onToggleChargeLimit = { batteryRequests.append(($0, $1))}
        view.onToggleCaffeine = { caffeineClicks += 1 }
        view.onSetMode = { modes.append($0) }

        let battery = try! XCTUnwrap(descendant(in: view, identifier: "coord.header.battery") as? NSButton)
        let caffeine = try! XCTUnwrap(descendant(in: view, identifier: "coord.header.caffeine") as? NSButton)
        let slider = try! XCTUnwrap(descendant(in: view, identifier: "coord.header.mode-control") as? ModeSlider)
        battery.performClick(nil)
        caffeine.performClick(nil)
        slider.onSetMode?("pause")
        slider.onSetMode?("medium")
        slider.onSetMode?("full")

        XCTAssertEqual(batteryRequests.count, 1)
        XCTAssertEqual(batteryRequests.first?.0, 80)
        XCTAssertEqual(batteryRequests.first?.1, 100)
        XCTAssertEqual(caffeineClicks, 1)
        XCTAssertEqual(modes, ["pause", "medium", "full"])
        XCTAssertTrue(caffeine.accessibilityLabel()?.contains("on") == true)
        XCTAssertEqual(caffeine.frame, CoordPowerControlsLayout.caffeineFrame)
        XCTAssertEqual(battery.frame, CoordPowerControlsLayout.batteryFrame)
        XCTAssertEqual(slider.frame, CoordPowerControlsLayout.sliderFrame)
        XCTAssertEqual(caffeine.frame.width, caffeine.frame.height)
        XCTAssertEqual(CoordPowerControlsLayout.controlRects.map(\.height), [24, 24, 24])
        XCTAssertEqual(CoordPowerControlsLayout.width, 114)
        XCTAssertLessThanOrEqual(caffeine.frame.maxX, battery.frame.minX)
        XCTAssertLessThanOrEqual(battery.frame.maxX, slider.frame.minX)
    }

    @MainActor
    func testBatteryDetailsUtilityButtonsShareMeterRowAndDispatch() {
        let view = CoordBatteryDetailsView(
            snapshot: LocalBatterySnapshot(
                percent: 64,
                source: "Battery",
                detail: "Discharging",
                error: nil
            )
        )
        var refreshes = 0
        var closes = 0
        view.onRefresh = { refreshes += 1 }
        view.onClose = { closes += 1 }

        let meter = try! XCTUnwrap(descendant(in: view, identifier: "coord.battery.meter"))
        let refresh = try! XCTUnwrap(descendant(in: view, identifier: "coord.battery.refresh") as? NSButton)
        let close = try! XCTUnwrap(descendant(in: view, identifier: "coord.battery.close") as? NSButton)
        refresh.performClick(nil)
        close.performClick(nil)

        XCTAssertEqual(meter.frame.width, 164)
        XCTAssertEqual(refresh.frame.midY, meter.frame.midY)
        XCTAssertEqual(close.frame.midY, meter.frame.midY)
        XCTAssertEqual(refreshes, 1)
        XCTAssertEqual(closes, 1)
    }

    @MainActor
    func testBatteryDetailsDispatchesChargeLockAndPerSourceEnergyModes() {
        let view = CoordBatteryDetailsView(
            snapshot: LocalBatterySnapshot(
                percent: 75,
                source: "Adapter",
                detail: "Charging",
                error: nil,
                chargeLimit: 75,
                chargeLimitMutationAvailable: true,
                batteryModeRaw: 1,
                adapterModeRaw: 0,
                highPowerModeSupported: true,
                energyModeMutationAvailable: true
            )
        )
        var charge: (Int, Int)?
        var energy: (LocalPowerSource, LocalEnergyMode, Int)?
        view.onToggleChargeLimit = { charge = ($0, $1) }
        view.onSetEnergyMode = { energy = ($0, $1, $2) }

        let lock = try! XCTUnwrap(
            descendant(in: view, identifier: "coord.battery.charge-limit") as? NSButton
        )
        let batteryAutomatic = try! XCTUnwrap(
            descendant(in: view, identifier: "coord.battery.battery.mode.0") as? NSButton
        )
        let adapterHigh = try! XCTUnwrap(
            descendant(in: view, identifier: "coord.battery.adapter.mode.2") as? NSButton
        )

        lock.performClick(nil)
        XCTAssertEqual(charge?.0, 75)
        XCTAssertEqual(charge?.1, 100)
        batteryAutomatic.performClick(nil)
        XCTAssertEqual(energy?.0, .battery)
        XCTAssertEqual(energy?.1, .automatic)
        XCTAssertEqual(energy?.2, 1)
        adapterHigh.performClick(nil)
        XCTAssertEqual(energy?.0, .adapter)
        XCTAssertEqual(energy?.1, .high)
        XCTAssertEqual(energy?.2, 0)
    }

    @MainActor
    func testCaffeineLaunchFailureIsSurfacedAndNeverMarksActive() {
        let controller = LocalPowerController(
            batteryReader: { .success(.unavailable) },
            caffeineLauncher: { throw LocalPowerFailure.command("launch denied") }
        )
        var outcome: (Bool, String)?
        controller.onOutcome = { outcome = ($0, $1) }

        controller.toggleCaffeine()

        XCTAssertFalse(controller.caffeineActive)
        XCTAssertEqual(outcome?.0, false)
        XCTAssertTrue(outcome?.1.contains("launch denied") == true)
    }

    func testHarnessControlRequiresHTTPAndPayloadAcceptanceBeforeSuccess() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let client = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/Data/HarnessClient.swift"),
            encoding: .utf8
        )
        let popover = try String(
            contentsOf: root.appendingPathComponent("apps/menubar/Sources/App/PopoverController.swift"),
            encoding: .utf8
        )

        XCTAssertTrue(client.contains("guard (200..<300).contains(http.statusCode)"))
        XCTAssertTrue(client.contains("if object?[\"ok\"] as? Bool == false"))
        XCTAssertTrue(popover.contains("alert.messageText = \"COORD action failed\""))
        XCTAssertTrue(popover.contains("HarnessControl.setMode(m)"))
        XCTAssertTrue(popover.contains("HarnessControl.pauseAll(jobIds: ids)"))
    }

    @MainActor
    func testSeparateBatteryItemIsPersistedOptionalAndOwnsRealDropdownActions() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let app = try String(contentsOf: root.appendingPathComponent("apps/menubar/Sources/App/AppDelegate.swift"))
        let config = try String(contentsOf: root.appendingPathComponent("apps/menubar/Sources/Data/Config.swift"))
        let persistence = try String(contentsOf: root.appendingPathComponent("apps/menubar/Sources/Data/ConfigPersistence.swift"))
        let settings = try String(contentsOf: root.appendingPathComponent("apps/menubar/Sources/UI/SettingsView.swift"))
        let power = try String(contentsOf: root.appendingPathComponent("apps/menubar/Sources/App/LocalPowerControls.swift"))
        XCTAssertTrue(settings.contains("Show Battery as a separate menu-bar item"))
        for category in ["General", "Display", "Usage", "Power", "Advanced"] {
            XCTAssertTrue(settings.contains("case \(category.lowercased()) = \"\(category)\""))
        }
        XCTAssertTrue(settings.contains("Focused controls · changes apply instantly"))
        XCTAssertTrue(settings.contains("Omar Motala 2026"))
        XCTAssertTrue(settings.contains("height: CGFloat = 660"))
        XCTAssertTrue(settings.contains("Open Full Cockpit Window…"))
        XCTAssertTrue(config.contains("var batteryStatusItemEnabled: Bool = false"))
        XCTAssertTrue(config.contains("MenuBarVisibilityPersistence(from: decoder)"))
        XCTAssertTrue(config.contains("visibility.batteryStatusItemEnabled"))
        XCTAssertTrue(persistence.contains("var batteryStatusItemEnabled: Bool?"))
        XCTAssertTrue(persistence.contains("case batteryStatusItemEnabled"))
        XCTAssertTrue(app.contains("batteryStatusItem.setEnabled(config.batteryStatusItemEnabled)"))
        XCTAssertTrue(app.contains("batteryStatusItem.onRefresh"))
        XCTAssertTrue(power.contains("final class CoordBatteryStatusItemController"))
        XCTAssertTrue(power.contains("view.onRefresh = { [weak self] in self?.onRefresh?() }"))
        XCTAssertTrue(power.contains("view.onClose = { [weak self] in self?.popover.close() }"))
        XCTAssertTrue(power.contains("button.image = CoordBatteryStatusItemPresentation.image(for: snapshot)"))
        XCTAssertTrue(power.contains("snapshot.adapterPowered ? .systemGreen : .labelColor"))
        XCTAssertTrue(power.contains("static let width: CGFloat = 23"))
        XCTAssertTrue(power.contains("button.imagePosition = .imageOnly"))
    }

    @MainActor
    private func descendant(in view: NSView, identifier: String) -> NSView? {
        if view.identifier?.rawValue == identifier { return view }
        for child in view.subviews {
            if let match = descendant(in: child, identifier: identifier) { return match }
        }
        return nil
    }
}
