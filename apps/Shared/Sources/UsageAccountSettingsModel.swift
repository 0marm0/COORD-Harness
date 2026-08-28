import Combine
import Foundation

@MainActor
final class UsageAccountSettingsModel: ObservableObject {
    enum Activity: Equatable {
        case idle
        case refreshing
        case performing(UsageAccountAction)
    }

    enum Notice: Equatable {
        case signInStarted
        case signInAlreadyActive
        case signInCancelled
        case noActiveSignIn
        case connectWindowOpened
        case connectAlreadyActive
        case alreadyConnected
        case actionUnavailable
        case profileUpdated

        var text: String {
            switch self {
            case .signInStarted: "Browser sign-in started. Return here and refresh when it completes."
            case .signInAlreadyActive: "A browser sign-in is already pending."
            case .signInCancelled: "The pending browser sign-in was cancelled."
            case .noActiveSignIn: "No pending browser sign-in was active."
            case .connectWindowOpened: "Claude Code sign-in opened. Finish the provider-owned browser flow."
            case .connectAlreadyActive: "A Claude Code sign-in is already pending."
            case .alreadyConnected: "Claude is already connected through Claude Code."
            case .actionUnavailable: "Account controls are temporarily unavailable."
            case .profileUpdated: "Active provider account updated. Sign-in and quota reads now use this profile."
            }
        }
    }

    @Published private(set) var codex: UsageCodexAccountStatus?
    @Published private(set) var claude: UsageClaudeAccountStatus?
    @Published private(set) var profiles: UsageProviderProfiles?
    @Published private(set) var activity: Activity = .idle
    @Published private(set) var profileMutationInProgress = false
    @Published private(set) var notice: Notice?

    private let client: any UsageAccountActionServing

    init(client: any UsageAccountActionServing) {
        self.client = client
    }

    var isBusy: Bool { activity != .idle || profileMutationInProgress }
    var canStartCodexLogin: Bool { !isBusy && codex?.canStart == true }
    var canCancelCodexLogin: Bool { !isBusy && codex?.canCancel == true }
    var canConnectClaude: Bool {
        !isBusy && claude?.connectAvailable == true
            && claude?.state != .connected && claude?.state != .waitingUser
    }
    var isCodexLoginPending: Bool {
        codex?.state == .starting || codex?.state == .waitingBrowser
    }

    var codexStatusLabel: String {
        guard let state = codex?.state else { return "Account status unavailable" }
        return switch state {
        case .idle: "Ready for browser sign-in"
        case .starting: "Starting browser sign-in"
        case .waitingBrowser: "Browser sign-in pending"
        case .completed: "Browser sign-in completed"
        case .failed: "Browser sign-in did not complete"
        case .cancelled: "Browser sign-in cancelled"
        case .expired: "Browser sign-in expired"
        case .unavailable: "Account status unavailable"
        }
    }

    func refresh() async {
        guard !isBusy else { return }
        activity = .refreshing
        defer { activity = .idle }
        do {
            apply(try await client.status())
            notice = nil
        } catch {
            codex = nil
            claude = nil
            notice = .actionUnavailable
        }
    }

    func startCodexLogin() async {
        guard canStartCodexLogin else { return }
        await perform(.codexLoginStart)
    }

    func cancelCodexLogin() async {
        guard canCancelCodexLogin else { return }
        await perform(.codexLoginCancel)
    }

    func connectClaude() async {
        guard canConnectClaude else { return }
        await perform(.claudeConnectOpen)
    }

    func mutateProfile(_ mutation: UsageProfileMutation) async {
        guard !isBusy else { return }
        profileMutationInProgress = true
        defer { profileMutationInProgress = false }
        do {
            apply(try await client.perform(mutation))
            notice = .profileUpdated
        } catch {
            notice = .actionUnavailable
        }
    }

    private func perform(_ action: UsageAccountAction) async {
        activity = .performing(action)
        defer { activity = .idle }
        do {
            let response = try await client.perform(action)
            apply(response)
            let mappedNotice = notice(
                for: response.result,
                action: action,
                claudeOpened: response.claude.opened
            )
            guard response.ok == true || mappedNotice != .actionUnavailable else {
                notice = .actionUnavailable
                return
            }
            notice = mappedNotice
        } catch {
            notice = .actionUnavailable
        }
    }

    private func apply(_ response: UsageAccountActionResponse) {
        codex = response.codex
        claude = response.claude
        profiles = response.profiles
    }

    private func notice(
        for result: UsageAccountActionResult?,
        action: UsageAccountAction,
        claudeOpened: Bool?
    ) -> Notice {
        switch (action, result) {
        case (.codexLoginStart, .browserOpened): .signInStarted
        case (.codexLoginStart, .loginAlreadyActive): .signInAlreadyActive
        case (.codexLoginCancel, .cancelled): .signInCancelled
        case (.codexLoginCancel, .noActiveLogin): .noActiveSignIn
        case (.claudeConnectOpen, .connectWindowOpened) where claudeOpened == true: .connectWindowOpened
        case (.claudeConnectOpen, .connectAlreadyActive): .connectAlreadyActive
        case (.claudeConnectOpen, .connectAlreadyConnected): .alreadyConnected
        default: .actionUnavailable
        }
    }
}
