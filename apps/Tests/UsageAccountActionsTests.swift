import Foundation
import XCTest
@testable import CoordCockpitMac

final class UsageAccountActionsTests: XCTestCase {
    override func tearDown() {
        UsageAccountURLProtocolStub.handler = nil
        super.tearDown()
    }

    func testExactPublicContractDecodesOnlyFixedPresentationFields() throws {
        let response = try decodeResponse(
            codexState: "waiting_browser",
            canStart: false,
            canCancel: true,
            reasonCode: "login_interrupted",
            connectAvailable: true,
            claudeState: "connected",
            includeSensitiveExtras: true
        )

        XCTAssertEqual(response.codex.state, .waitingBrowser)
        XCTAssertFalse(response.codex.canStart)
        XCTAssertTrue(response.codex.canCancel)
        XCTAssertEqual(response.codex.reasonCode, .loginInterrupted)
        XCTAssertEqual(response.claude.state, .connected)
        XCTAssertEqual(response.claude.state.safeStatusLabel, "Connected")
        XCTAssertTrue(response.claude.state.safeStatusCopy.contains("local provider service"))
        XCTAssertFalse(response.claude.state.safeStatusCopy.contains("CodexBar"))
        XCTAssertTrue(response.claude.connectAvailable)
        XCTAssertNil(response.ok)
        XCTAssertNil(response.result)
    }

    func testWrongSchemaAndUnknownStateFailClosed() throws {
        let wrongSchema = Data(#"""
        {
          "schema":"coord.usage-account-action.v1",
          "codex":{"state":"idle","can_start":true,"can_cancel":false},
          "claude":{"state":"manual_connect_required","connect_available":true}
        }
        """#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(UsageAccountActionResponse.self, from: wrongSchema))

        let unknownState = Data(#"""
        {
          "schema":"coord.usage-account-actions.v1",
          "codex":{"state":"revealing_secret","can_start":true,"can_cancel":false},
          "claude":{"state":"manual_connect_required","connect_available":true}
        }
        """#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(UsageAccountActionResponse.self, from: unknownState))
    }


    func testUnknownAndIncoherentResultsFailClosed() throws {
        let unknownResult = Data(#"""
        {
          "schema":"coord.usage-account-actions.v1",
          "codex":{"state":"idle","can_start":true,"can_cancel":false},
          "claude":{"state":"manual_connect_required","connect_available":true},
          "ok":true,
          "result":"authenticated_with_secret"
        }
        """#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(UsageAccountActionResponse.self, from: unknownResult))

        let falseConnectSuccess = Data(#"""
        {
          "schema":"coord.usage-account-actions.v1",
          "codex":{"state":"idle","can_start":true,"can_cancel":false},
          "claude":{"state":"manual_connect_required","connect_available":true,"opened":false},
          "ok":true,
          "result":"connect_window_opened"
        }
        """#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(UsageAccountActionResponse.self, from: falseConnectSuccess))
    }

    func testPostUsesStrictLoopbackOriginFixedHeaderAndExactActionBody() async throws {
        let payload = try responseData(
            codexState: "waiting_browser",
            canStart: false,
            canCancel: true,
            connectAvailable: true,
            ok: true,
            result: "browser_opened"
        )
        UsageAccountURLProtocolStub.handler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.absoluteString, EndpointTestFixtures.loopbackUsageActions)
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Origin"),
                EndpointTestFixtures.loopbackOrigin
            )
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-Coord-Usage-Action"), "v1")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
            XCTAssertFalse(request.httpShouldHandleCookies)
            let body = try request.bodyData()
            XCTAssertEqual(
                try JSONSerialization.jsonObject(with: body) as? [String: String],
                ["action": "codex_login_start"]
            )
            return (
                try XCTUnwrap(HTTPURLResponse(
                    url: request.url!,
                    statusCode: 202,
                    httpVersion: "HTTP/1.1",
                    headerFields: ["Content-Type": "application/json"]
                )),
                payload
            )
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UsageAccountURLProtocolStub.self]
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        let client = UsageAccountActionClient(
            baseURL: URL(string: EndpointTestFixtures.loopbackIgnoredBasePath),
            session: URLSession(configuration: configuration)
        )

        let response = try await client.perform(.codexLoginStart)

        XCTAssertEqual(response.result, .browserOpened)
        XCTAssertEqual(response.codex.state, .waitingBrowser)
    }

    func testNonLoopbackAndCredentialBearingEndpointsAreRejectedBeforeNetwork() async {
        for raw in [
            "https://example.com",
            EndpointTestFixtures.privateNetworkOrigin,
            EndpointTestFixtures.credentialedLoopbackOrigin,
            "file:///tmp/coord",
        ] {
            do {
                _ = try await UsageAccountActionClient(baseURL: URL(string: raw)).status()
                XCTFail("Expected local-board rejection for \(raw)")
            } catch {
                XCTAssertEqual(error as? UsageAccountActionError, .localBoardRequired)
            }
        }
    }

    @MainActor
    func testModelUsesServerActionFlagsAndFixedNotices() async throws {
        let initial = try decodeResponse(
            codexState: "idle",
            canStart: true,
            canCancel: false,
            connectAvailable: true
        )
        let started = try decodeResponse(
            codexState: "waiting_browser",
            canStart: false,
            canCancel: true,
            connectAvailable: true,
            ok: true,
            result: "browser_opened"
        )
        let model = UsageAccountSettingsModel(
            client: UsageAccountActionStub(statusResponse: initial, actionResponse: started)
        )

        await model.refresh()
        XCTAssertTrue(model.canStartCodexLogin)
        XCTAssertFalse(model.canCancelCodexLogin)
        XCTAssertTrue(model.canConnectClaude)
        XCTAssertEqual(model.codexStatusLabel, "Ready for browser sign-in")

        await model.startCodexLogin()
        XCTAssertFalse(model.canStartCodexLogin)
        XCTAssertTrue(model.canCancelCodexLogin)
        XCTAssertEqual(model.codexStatusLabel, "Browser sign-in pending")
        XCTAssertEqual(model.notice, .signInStarted)
    }


    @MainActor
    func testModelConnectClaudeReportsDirectClaudeCodeSignInViaProviderService() async throws {
        let initial = try decodeResponse(
            codexState: "idle",
            canStart: true,
            canCancel: false,
            connectAvailable: true
        )
        let opened = try decodeResponse(
            codexState: "idle",
            canStart: true,
            canCancel: false,
            connectAvailable: true,
            ok: true,
            result: "connect_window_opened",
            claudeOpened: true
        )
        let model = UsageAccountSettingsModel(
            client: UsageAccountActionStub(statusResponse: initial, actionResponse: opened)
        )

        await model.refresh()
        await model.connectClaude()

        XCTAssertEqual(model.claude?.state, .manualConnectRequired)
        XCTAssertEqual(model.notice, .connectWindowOpened)
        XCTAssertEqual(
            model.notice?.text,
            "Claude Code sign-in opened. Finish the provider-owned browser flow."
        )
    }

    func testSettingsModelSourceDeclaresCombineForObservableObjectCompilation() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: projectRoot.appendingPathComponent(
                "apps/Shared/Sources/UsageAccountSettingsModel.swift"
            ),
            encoding: .utf8
        )

        XCTAssertTrue(source.hasPrefix("import Combine\n"))
    }

    @MainActor
    func testRecognizedNonOKCodexOutcomesKeepSpecificNotices() async throws {
        let ready = try decodeResponse(
            codexState: "idle",
            canStart: true,
            canCancel: false,
            connectAvailable: true
        )
        let alreadyActive = try decodeResponse(
            codexState: "waiting_browser",
            canStart: false,
            canCancel: true,
            connectAvailable: true,
            ok: false,
            result: "login_already_active"
        )
        let startingModel = UsageAccountSettingsModel(
            client: UsageAccountActionStub(statusResponse: ready, actionResponse: alreadyActive)
        )
        await startingModel.refresh()
        await startingModel.startCodexLogin()
        XCTAssertEqual(startingModel.notice, .signInAlreadyActive)
        XCTAssertTrue(startingModel.canCancelCodexLogin)

        let pending = try decodeResponse(
            codexState: "waiting_browser",
            canStart: false,
            canCancel: true,
            connectAvailable: true
        )
        let noActive = try decodeResponse(
            codexState: "idle",
            canStart: true,
            canCancel: false,
            connectAvailable: true,
            ok: false,
            result: "no_active_login"
        )
        let cancellingModel = UsageAccountSettingsModel(
            client: UsageAccountActionStub(statusResponse: pending, actionResponse: noActive)
        )
        await cancellingModel.refresh()
        await cancellingModel.cancelCodexLogin()
        XCTAssertEqual(cancellingModel.notice, .noActiveSignIn)
        XCTAssertTrue(cancellingModel.canStartCodexLogin)
    }

    @MainActor
    func testWaitingUserClaudeStateIsNormalizedAndCannotLaunchDuplicateFlow() async throws {
        let waiting = try decodeResponse(
            codexState: "idle",
            canStart: true,
            canCancel: false,
            connectAvailable: true,
            claudeState: "waiting_user"
        )
        let model = UsageAccountSettingsModel(
            client: UsageAccountActionStub(statusResponse: waiting, actionResponse: waiting)
        )

        await model.refresh()

        XCTAssertEqual(model.claude?.state, .waitingUser)
        XCTAssertEqual(model.claude?.state.safeStatusLabel, "Waiting for sign-in")
        XCTAssertEqual(
            model.claude?.state.safeStatusCopy,
            "Claude Code sign-in is open. Finish the provider-owned browser flow."
        )
        XCTAssertFalse(model.canConnectClaude)
    }

    @MainActor
    func testModelNeverPublishesRawTransportErrors() async {
        let model = UsageAccountSettingsModel(client: FailingUsageAccountActionStub())

        await model.refresh()

        XCTAssertNil(model.codex)
        XCTAssertNil(model.claude)
        XCTAssertEqual(model.codexStatusLabel, "Account status unavailable")
        XCTAssertEqual(model.notice?.text, "Account controls are temporarily unavailable.")
    }

    func testAppKitSettingsOpensProviderAccountsDirectlyAndReturnsScreenFit() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let settings = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/UI/SettingsView.swift"),
            encoding: .utf8
        )
        let popover = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/App/PopoverController.swift"),
            encoding: .utf8
        )
        let accountView = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageAccountSettingsView.swift"),
            encoding: .utf8
        )
        let installedUsage = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/Usage/InstalledUsageDashboard.swift"),
            encoding: .utf8
        )
        let routeStart = try XCTUnwrap(popover.range(of: "private func showProviderAccountsFromSettings()"))
        let routeEnd = try XCTUnwrap(popover.range(of: "private func showUsage()", range: routeStart.upperBound..<popover.endIndex))
        let route = String(popover[routeStart.lowerBound..<routeEnd.lowerBound])

        XCTAssertTrue(settings.contains("accounts.title = \"Accounts · Services · Routing…\""))
        XCTAssertTrue(settings.contains("accounts.action = #selector(doOpenProviderAccounts)"))
        XCTAssertTrue(settings.contains("multi-account, service, Keychain, and routing controls"))
        XCTAssertTrue(popover.contains("v.onOpenProviderAccounts = { [weak self] in self?.showProviderAccountsFromSettings() }"))
        XCTAssertTrue(route.contains("rootView: UsageAccountSettingsView("))
        XCTAssertTrue(route.contains("baseURL: HarnessEndpoint.url(\"/\")"))
        XCTAssertTrue(route.contains("onOpenCORDSettings: { [weak self] in self?.showSettings() }"))
        XCTAssertTrue(route.contains("onDone: { [weak self] in self?.showSettings() }"))
        XCTAssertTrue(route.contains("let width = detachedVisible ? max(500"))
        XCTAssertTrue(route.contains(": min(620, availablePopoverHeight())"))
        XCTAssertTrue(route.contains("popover.contentSize = NSSize(width: width, height: height)"))
        XCTAssertTrue(accountView.contains("let onDone: (() -> Void)?"))
        XCTAssertTrue(accountView.contains("onDone: (() -> Void)? = nil"))
        XCTAssertTrue(accountView.contains("Button(\"Done\", action: finish)"))
        XCTAssertTrue(accountView.contains("if let onDone"))
        XCTAssertTrue(accountView.contains("else {"))
        XCTAssertTrue(accountView.contains("dismiss()"))
        XCTAssertTrue(installedUsage.contains(".sheet(isPresented: $showingAccounts)"))
        XCTAssertTrue(installedUsage.contains("UsageAccountSettingsView("))
    }

    func testNativeSettingsAndPopoverUseNeutralGroupedDarkGlassUI() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        func source(_ path: String) throws -> String {
            try String(contentsOf: projectRoot.appendingPathComponent(path), encoding: .utf8)
        }
        let settings = try source("apps/menubar/Sources/UI/SettingsView.swift")
        let popover = try source("apps/menubar/Sources/App/PopoverController.swift")
        let config = try source("apps/menubar/Sources/Data/Config.swift")
        let accountActions = try source("apps/Shared/Sources/UsageAccountActions.swift")
        let accountView = try source("apps/Shared/Sources/UsageAccountSettingsView.swift")
        let usage = try source("apps/Shared/Sources/UsageIntelligence.swift")

        for heading in ["Provider services", "Usage display", "Work display", "Data / Compatibility"] {
            XCTAssertTrue(settings.contains("section(\"\(heading)\")"), heading)
        }
        XCTAssertTrue(settings.contains("Show inline Usage details"))
        XCTAssertTrue(settings.contains("cfg.usagePeekCollapsed = showInlineUsage.state != .on"))
        XCTAssertTrue(settings.contains("Quota bars + progress"))
        XCTAssertTrue(settings.contains("Legacy file compatibility"))
        XCTAssertTrue(settings.contains("layer?.backgroundColor = NSColor.clear.cgColor"))

        XCTAssertTrue(popover.contains("final class GlassBackground: NSView"))
        XCTAssertTrue(popover.contains("a < 0.01 ? NSColor.clear"))
        XCTAssertFalse(popover.contains("final class GlassBackground: NSVisualEffectView"))
        XCTAssertFalse(popover.contains("layer?.borderColor"))
        XCTAssertTrue(popover.contains("scroll.drawsBackground = false"))
        XCTAssertTrue(popover.contains("let viewportHeight = min(h, availablePopoverHeight())"))
        XCTAssertTrue(config.contains("var glassMaterial: String = \"under_window\""))
        XCTAssertTrue(config.contains("var glassAlpha: Double = 0.0"))
        XCTAssertTrue(config.contains("storedGlassMaterial == \"hud\", storedGlassAlpha == 0.72"))

        XCTAssertFalse(settings.localizedCaseInsensitiveContains("codexbar"))
        XCTAssertFalse(accountActions.localizedCaseInsensitiveContains("codexbar"))
        XCTAssertFalse(accountView.localizedCaseInsensitiveContains("codexbar"))
        XCTAssertTrue(usage.contains("case \"codexbar_local_projection\":"), "Internal source key remains exact")
        XCTAssertTrue(usage.contains("return \"Legacy compatibility pace projection\""))
        XCTAssertFalse(usage.contains("return \"CodexBar"), "Donor branding must not be emitted as UI copy")
    }

    private func decodeResponse(
        codexState: String,
        canStart: Bool,
        canCancel: Bool,
        reasonCode: String? = nil,
        connectAvailable: Bool,
        claudeState: String = "manual_connect_required",
        ok: Bool? = nil,
        result: String? = nil,
        claudeOpened: Bool? = nil,
        includeSensitiveExtras: Bool = false
    ) throws -> UsageAccountActionResponse {
        try JSONDecoder().decode(
            UsageAccountActionResponse.self,
            from: responseData(
                codexState: codexState,
                canStart: canStart,
                canCancel: canCancel,
                reasonCode: reasonCode,
                connectAvailable: connectAvailable,
                claudeState: claudeState,
                ok: ok,
                result: result,
                claudeOpened: claudeOpened,
                includeSensitiveExtras: includeSensitiveExtras
            )
        )
    }

    private func responseData(
        codexState: String,
        canStart: Bool,
        canCancel: Bool,
        reasonCode: String? = nil,
        connectAvailable: Bool,
        claudeState: String = "manual_connect_required",
        ok: Bool? = nil,
        result: String? = nil,
        claudeOpened: Bool? = nil,
        includeSensitiveExtras: Bool = false
    ) throws -> Data {
        var codex: [String: Any] = [
            "state": codexState,
            "can_start": canStart,
            "can_cancel": canCancel,
        ]
        if let reasonCode { codex["reason_code"] = reasonCode }
        var claude: [String: Any] = [
            "state": claudeState,
            "connect_available": connectAvailable,
        ]
        if let claudeOpened { claude["opened"] = claudeOpened }
        var document: [String: Any] = [
            "schema": "coord.usage-account-actions.v1",
            "codex": codex,
            "claude": claude,
        ]
        if let ok { document["ok"] = ok }
        if let result { document["result"] = result }
        if includeSensitiveExtras {
            document["auth_url"] = "must-not-render"
            document["login_id"] = "must-not-render"
            document["token"] = "must-not-render"
            document["email"] = "must-not-render"
            document["path"] = "must-not-render"
            document["error"] = "must-not-render"
        }
        return try JSONSerialization.data(withJSONObject: document)
    }
}

private struct UsageAccountActionStub: UsageAccountActionServing {
    let statusResponse: UsageAccountActionResponse
    let actionResponse: UsageAccountActionResponse

    func status() async throws -> UsageAccountActionResponse { statusResponse }
    func perform(_ action: UsageAccountAction) async throws -> UsageAccountActionResponse {
        actionResponse
    }
}

private struct FailingUsageAccountActionStub: UsageAccountActionServing {
    func status() async throws -> UsageAccountActionResponse {
        throw URLError(.cannotConnectToHost)
    }

    func perform(_ action: UsageAccountAction) async throws -> UsageAccountActionResponse {
        throw URLError(.cannotConnectToHost)
    }
}

private final class UsageAccountURLProtocolStub: URLProtocol {
    static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        do {
            let handler = try XCTUnwrap(Self.handler)
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private extension URLRequest {
    func bodyData() throws -> Data {
        if let httpBody { return httpBody }
        let stream = try XCTUnwrap(httpBodyStream)
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1_024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count < 0 { throw stream.streamError ?? URLError(.cannotDecodeContentData) }
            if count == 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
