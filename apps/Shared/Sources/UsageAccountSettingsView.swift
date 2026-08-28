import SwiftUI

struct UsageAccountSettingsView: View {
    @StateObject private var model: UsageAccountSettingsModel
    @State private var newClaudeProfile = ""
    @State private var newCodexProfile = ""
    let onOpenCORDSettings: (() -> Void)?
    let onDone: (() -> Void)?
    private let baseURL: URL?
    @Environment(\.dismiss) private var dismiss

    init(baseURL: URL?, onOpenCORDSettings: (() -> Void)? = nil, onDone: (() -> Void)? = nil) {
        _model = StateObject(wrappedValue: UsageAccountSettingsModel(
            client: UsageAccountActionClient(baseURL: baseURL)
        ))
        self.onOpenCORDSettings = onOpenCORDSettings
        self.onDone = onDone
        self.baseURL = baseURL
    }

    init(model: UsageAccountSettingsModel, onOpenCORDSettings: (() -> Void)? = nil, onDone: (() -> Void)? = nil) {
        _model = StateObject(wrappedValue: model)
        self.onOpenCORDSettings = onOpenCORDSettings
        self.onDone = onDone
        self.baseURL = nil
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("CORD forwards these fixed account actions to the configured provider login service. CORD never receives or displays credentials.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    NavigationLink {
                        ProviderManagementView(baseURL: baseURL)
                    } label: {
                        Label("Services & Intelligent Routing", systemImage: "point.3.connected.trianglepath.dotted")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)

                    accountCard(
                        title: "Codex",
                        symbol: "terminal",
                        status: model.codexStatusLabel
                    ) {
                        profileControls(
                            .codex,
                            profiles: model.profiles?.codex,
                            newLabel: $newCodexProfile,
                            tint: .purple
                        )
                        Button {
                            Task { await model.startCodexLogin() }
                        } label: {
                            Label("Start browser sign-in", systemImage: "safari")
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(!model.canStartCodexLogin)

                        if model.isCodexLoginPending {
                            Button(role: .cancel) {
                                Task { await model.cancelCodexLogin() }
                            } label: {
                                Label("Cancel pending sign-in", systemImage: "xmark.circle")
                                    .fixedSize(horizontal: false, vertical: true)
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)
                            .disabled(!model.canCancelCodexLogin)
                        }

                        Button {
                            Task { await model.refresh() }
                        } label: {
                            Label("Refresh account status", systemImage: "arrow.clockwise")
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .disabled(model.isBusy)
                    }

                    accountCard(
                        title: "Claude",
                        symbol: "sparkles",
                        status: model.claude?.state.safeStatusLabel ?? "Account status unavailable"
                    ) {
                        Text(model.claude?.state.safeStatusCopy ?? "Refresh to check Claude connection status.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)

                        profileControls(
                            .claude,
                            profiles: model.profiles?.claude,
                            newLabel: $newClaudeProfile,
                            tint: .orange
                        )

                        Button {
                            Task { await model.connectClaude() }
                        } label: {
                            Label(
                                model.claude?.state == .connected ? "Open Claude settings" : "Open Claude Code sign-in",
                                systemImage: "person.badge.key"
                            )
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(!model.canConnectClaude)
                        .accessibilityHint("Requests direct Claude Code sign-in through the local provider service")
                    }

                    if model.isBusy {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Updating account status...")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        }
                        .accessibilityElement(children: .combine)
                    }

                    if let notice = model.notice {
                        Label(
                            notice.text,
                            systemImage: notice == .actionUnavailable
                                ? "exclamationmark.triangle"
                                : "checkmark.circle"
                        )
                        .font(.callout)
                        .foregroundStyle(notice == .actionUnavailable ? .orange : .secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityLabel(notice.text)
                    }

                    if let onOpenCORDSettings {
                        Divider()
                        Button("Open display and panel settings") {
                            dismiss()
                            DispatchQueue.main.async { onOpenCORDSettings() }
                        }
                        .buttonStyle(.borderless)
                    }
                }
                .frame(maxWidth: 620, alignment: .leading)
                .padding(20)
            }
            .background(Color.clear)
            .navigationTitle("Accounts & Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done", action: finish)
                }
            }
        }
        .frame(idealWidth: 520, idealHeight: 620)
        .task { await model.refresh() }
    }

    private func finish() {
        if let onDone {
            onDone()
        } else {
            dismiss()
        }
    }

    private func accountCard<Actions: View>(
        title: String,
        symbol: String,
        status: String,
        @ViewBuilder actions: () -> Actions
    ) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: symbol)
                .font(.title3.bold())
            Text(status)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 10) {
                actions()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(Color.primary.opacity(0.12), lineWidth: 1)
        }
    }

    @ViewBuilder
    private func profileControls(
        _ provider: UsageAccountProvider,
        profiles: UsageProviderProfileCollection?,
        newLabel: Binding<String>,
        tint: Color
    ) -> some View {
        if let profiles {
            VStack(alignment: .leading, spacing: 8) {
                Text("ACCOUNT PROFILE")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.secondary)
                ForEach(profiles.profiles) { profile in
                    HStack(spacing: 8) {
                        Button {
                            Task { await model.mutateProfile(.select(provider: provider, profileID: profile.id)) }
                        } label: {
                            Label(profile.label, systemImage: profile.active ? "checkmark.circle.fill" : "circle")
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(profile.active ? tint : .primary)
                        .disabled(model.isBusy || profile.active)
                        if profile.isolated {
                            Button(role: .destructive) {
                                Task { await model.mutateProfile(.remove(provider: provider, profileID: profile.id)) }
                            } label: {
                                Image(systemName: "minus.circle")
                            }
                            .buttonStyle(.plain)
                            .disabled(model.isBusy)
                            .help("Remove this selector entry; provider credentials remain recoverable on disk")
                        }
                    }
                }
                HStack(spacing: 8) {
                    TextField("New account name", text: newLabel)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { addProfile(provider, label: newLabel) }
                    Button("Add") { addProfile(provider, label: newLabel) }
                        .buttonStyle(.bordered)
                        .disabled(model.isBusy || newLabel.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                Text("Each isolated profile persists its official CLI session. Login and quota reads follow the selected profile.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(10)
            .background(tint.opacity(0.06), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
    }

    private func addProfile(_ provider: UsageAccountProvider, label: Binding<String>) {
        let value = label.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        label.wrappedValue = ""
        Task { await model.mutateProfile(.add(provider: provider, label: value)) }
    }
}
