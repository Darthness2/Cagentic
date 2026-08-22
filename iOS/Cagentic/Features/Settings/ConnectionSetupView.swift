import SwiftUI
import UIKit

struct ConnectionSetupView: View {
    enum Mode: Equatable {
        case onboarding
        case add
        case edit
    }

    @Bindable var model: AppModel
    @Environment(\.dismiss) private var dismiss

    private let profileID: ServerProfileID?
    private let mode: Mode
    private let originalEndpoint: String?
    private let originalKind: ServerKind?
    private let hasSavedCredential: Bool

    @State private var serverName: String
    @State private var serverURL: String
    @State private var kind: ServerKind
    @State private var bearerToken: String
    @State private var connectsWithoutBearerToken = false
    @State private var isAuthenticationExpanded: Bool
    @State private var isConnecting = false
    @State private var inlineError: String?
    @State private var connectionTask: Task<Void, Never>?
    @State private var connectionAttemptToken: UUID?
    @State private var ownsConnectionAttempt = false
    @AccessibilityFocusState private var isErrorFocused: Bool

    init(
        model: AppModel,
        profileID: ServerProfileID? = nil,
        mode: Mode? = nil
    ) {
        self.model = model
        let resolvedMode = mode
            ?? (model.settings.hasCompletedOnboarding ? .edit : .onboarding)
        let resolvedProfileID = profileID
            ?? (resolvedMode == .add ? nil : model.activeServerProfile?.id)
        let profile = resolvedProfileID.flatMap { id in
            model.serverProfiles.first(where: { $0.id == id })
        }
        let hasSavedCredential = profile?.authentication == .bearerToken

        self.profileID = resolvedProfileID
        self.mode = resolvedMode
        originalEndpoint = profile?.endpoint
        originalKind = profile?.kind
        self.hasSavedCredential = hasSavedCredential
        _serverName = State(
            initialValue: resolvedMode == .add
                ? ""
                : profile?.displayName ?? model.settings.serverName
        )
        _serverURL = State(
            initialValue: resolvedMode == .add
                ? ""
                : profile?.endpoint ?? model.settings.serverURL
        )
        _kind = State(initialValue: profile?.kind ?? .ollama)
        _bearerToken = State(initialValue: "")
        _isAuthenticationExpanded = State(
            initialValue: hasSavedCredential
        )
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
                    introduction
                    fields
                    setupSteps
                    securityNote
                }
                .frame(maxWidth: 620, alignment: .leading)
                .padding(CagenticTheme.Spacing.lg)
                .frame(maxWidth: .infinity)
            }
            .background(CagenticTheme.background)
            .navigationTitle(navigationTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if isConnectionInProgress {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Stop", role: .cancel) {
                            cancelConnectionAttempt()
                        }
                    }
                } else if canDismiss {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel", role: .cancel) {
                            dismiss()
                        }
                    }
                }

                if isConnectionInProgress {
                    ToolbarItem(placement: .confirmationAction) {
                        ProgressView()
                            .controlSize(.small)
                            .accessibilityLabel(progressTitle)
                    }
                } else {
                    ToolbarItem(placement: .confirmationAction) {
                        Button(actionTitle) {
                            connect()
                        }
                        .fontWeight(.semibold)
                        .disabled(!canSubmit)
                    }
                }
            }
            .interactiveDismissDisabled(!canDismiss || isConnectionInProgress)
            .onDisappear {
                if ownsConnectionAttempt {
                    model.cancelConnectionAttempt()
                }
                connectionTask?.cancel()
                connectionTask = nil
            }
        }
    }

    private var introduction: some View {
        HStack(alignment: .top, spacing: CagenticTheme.Spacing.md) {
            BrandMark(size: 38)
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                Text(introductionTitle)
                    .font(CagenticTheme.FontStyle.displaySmall)
                    .foregroundStyle(CagenticTheme.textPrimary)
                Text(introductionMessage)
                    .font(CagenticTheme.FontStyle.body)
                    .foregroundStyle(CagenticTheme.textSecondary)
            }
        }
    }

    private var fields: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.md) {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                Picker("Server type", selection: $kind) {
                    ForEach(ServerKind.allCases) { option in
                        Text(option.displayName).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .accessibilityLabel("Server type")

                Text(kindHelp)
                    .font(CagenticTheme.FontStyle.caption)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .onChange(of: kind) { _, _ in
                inlineError = nil
            }

            Divider()

            LabeledContent("Connection name") {
                TextField("Studio PC", text: $serverName)
                    .multilineTextAlignment(.trailing)
                    .textInputAutocapitalization(.words)
                    .accessibilityLabel("Connection name")
            }

            Divider()

            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                Text("Server address")
                    .font(CagenticTheme.FontStyle.subheadlineSemibold)
                TextField(addressPlaceholder, text: $serverURL)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textContentType(.URL)
                    .accessibilityLabel("Server address")
                    .accessibilityHint(
                        "The computer's LAN IP or local hostname, not localhost or 0.0.0.0"
                    )
                    .padding(CagenticTheme.Spacing.sm)
                    .background(CagenticTheme.background, in: .rect(cornerRadius: CagenticTheme.Radius.control))
                Text(addressHelp)
                    .font(CagenticTheme.FontStyle.caption)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            DisclosureGroup(
                credentialSectionTitle,
                isExpanded: $isAuthenticationExpanded
            ) {
                VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                    SecureField(credentialFieldPrompt, text: $bearerToken)
                        .textContentType(.password)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .disabled(connectsWithoutBearerToken && kind == .ollama)
                        .accessibilityLabel(credentialSectionTitle)
                        .accessibilityHint(
                            kind == .gateway
                                ? "The gateway.token value from the computer's Cagentic config"
                                : "Optional token for an authenticated reverse proxy"
                        )
                        .padding(CagenticTheme.Spacing.sm)
                        .background(CagenticTheme.background, in: .rect(cornerRadius: CagenticTheme.Radius.control))
                        .onChange(of: bearerToken) { _, token in
                            if !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                                connectsWithoutBearerToken = false
                            }
                        }
                    if hasSavedCredential, kind == .ollama {
                        Toggle("Connect without a bearer token", isOn: $connectsWithoutBearerToken)
                            .font(CagenticTheme.FontStyle.callout)
                            .onChange(of: connectsWithoutBearerToken) { _, connectsWithoutToken in
                                if connectsWithoutToken {
                                    bearerToken = ""
                                }
                            }
                    }
                    Text(authenticationHelp)
                        .font(CagenticTheme.FontStyle.caption)
                        .foregroundStyle(CagenticTheme.textSecondary)
                }
                .padding(.top, CagenticTheme.Spacing.xs)
            }

            if cleartextTokenWarningApplies {
                Label {
                    Text("This gateway address is not encrypted. The token will cross your local network in the clear, so use it only on a network you trust.")
                        .font(CagenticTheme.FontStyle.caption)
                } icon: {
                    Image(systemName: "lock.open")
                        .foregroundStyle(CagenticTheme.warning)
                }
                .foregroundStyle(CagenticTheme.textSecondary)
                .accessibilityElement(children: .combine)
            }

            if let inlineError {
                Label(inlineError, systemImage: "exclamationmark.triangle.fill")
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.error)
                    .accessibilityLabel("Connection error: \(inlineError)")
                    .accessibilityFocused($isErrorFocused)
            }
        }
        .cagenticCard()
    }

    @ViewBuilder
    private var setupSteps: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.md) {
            Text("On the computer")
                .font(CagenticTheme.FontStyle.heading)

            switch kind {
            case .ollama:
                SetupStep(number: 1, text: "Set OLLAMA_HOST to 0.0.0.0:11434, then restart Ollama.")
                SetupStep(number: 2, text: "Allow TCP 11434 through the private-network firewall.")
                SetupStep(number: 3, text: "Keep both devices on the same trusted Wi-Fi and enter the computer's LAN address above.")
            case .gateway:
                SetupStep(number: 1, text: "In ~/.config/cagentic/config.json, set gateway.lan to true so the gateway listens on the network rather than only on the computer itself.")
                SetupStep(number: 2, text: "In the same gateway section, set a long random gateway.token. Without a pinned token the gateway invents a new one every restart and this device stops working.")
                SetupStep(number: 3, text: "Restart the gateway, allow its port (8700 by default) through the private-network firewall, then enter the address and that token above.")
            }
        }
    }

    private var securityNote: some View {
        Label {
            Text(securityMessage)
                .font(CagenticTheme.FontStyle.callout)
        } icon: {
            Image(systemName: "lock.shield")
                .foregroundStyle(CagenticTheme.warning)
        }
        .cagenticCard()
        .accessibilityElement(children: .combine)
    }

    private func connect() {
        guard !isConnectionInProgress else { return }
        let attemptToken = UUID()
        connectionAttemptToken = attemptToken
        isConnecting = true
        ownsConnectionAttempt = true
        inlineError = nil
        isErrorFocused = false
        connectionTask = Task {
            let result = await connectionResult()
            let wasCancelled = Task.isCancelled
            guard connectionAttemptToken == attemptToken else { return }
            isConnecting = false
            ownsConnectionAttempt = false
            connectionTask = nil
            connectionAttemptToken = nil
            guard !wasCancelled else { return }
            switch result {
            case .success:
                UIAccessibility.post(
                    notification: .announcement,
                    argument: connectionSuccessAnnouncement
                )
                dismiss()
            case .failure(let error):
                guard !(error is CancellationError) else { return }
                inlineError = actionableMessage(error)
                isErrorFocused = true
                UIAccessibility.post(
                    notification: .announcement,
                    argument: "Connection failed. \(inlineError ?? "Please try again.")"
                )
            }
        }
    }

    private func cancelConnectionAttempt() {
        model.cancelConnectionAttempt()
        connectionTask?.cancel()
        connectionTask = nil
        connectionAttemptToken = nil
        ownsConnectionAttempt = false
        isConnecting = false
        UIAccessibility.post(notification: .announcement, argument: "Connection stopped.")
    }

    private var isConnectionInProgress: Bool {
        if isConnecting {
            return true
        }
        if case .connecting = model.connectionState {
            return true
        }
        return false
    }

    private var canDismiss: Bool {
        mode != .onboarding || model.settings.hasCompletedOnboarding
    }

    private var canSubmit: Bool {
        guard !isConnectionInProgress,
              !serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return false
        }
        let typedToken = !bearerToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        // Every gateway route is token-gated, so there is no anonymous connection to attempt.
        if kind == .gateway {
            return typedToken || (hasSavedCredential && !endpointChanged)
        }
        return !requiresCredentialReentry || typedToken
    }

    /// True when the saved credential no longer belongs to what is being connected to.
    ///
    /// A backend switch counts: the same address serving Ollama and serving a gateway are different
    /// servers wanting different secrets, and the model rotates the profile identity for exactly
    /// this reason.
    private var endpointChanged: Bool {
        guard let originalEndpoint else { return false }
        if let originalKind, originalKind != kind {
            return true
        }
        let trimmed = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        switch kind {
        case .ollama:
            guard let original = try? OllamaEndpoint(originalEndpoint),
                  let candidate = try? OllamaEndpoint(serverURL)
            else {
                return trimmed != originalEndpoint
            }
            return original.displayAddress != candidate.displayAddress
        case .gateway:
            guard let original = try? GatewayEndpoint(originalEndpoint),
                  let candidate = try? GatewayEndpoint(serverURL)
            else {
                return trimmed != originalEndpoint
            }
            return original.displayAddress != candidate.displayAddress
        }
    }

    private var requiresCredentialReentry: Bool {
        hasSavedCredential
            && endpointChanged
            && !connectsWithoutBearerToken
            && bearerToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Whether the token typed here would travel unencrypted.
    private var cleartextTokenWarningApplies: Bool {
        guard kind == .gateway,
              let endpoint = try? GatewayEndpoint(serverURL)
        else {
            return false
        }
        return endpoint.sendsTokenInClear
    }

    private var kindHelp: String {
        switch kind {
        case .ollama:
            "Talks straight to Ollama on the computer. Chats stay on this device, and the model answers with text only."
        case .gateway:
            "Talks to Cagentic running on the computer, so replies can read files, run commands, and search — asking your approval first. Those chats live on the computer."
        }
    }

    private var addressPlaceholder: String {
        switch kind {
        case .ollama: "192.168.1.42:11434"
        case .gateway: "192.168.1.42:8700"
        }
    }

    private var addressHelp: String {
        switch kind {
        case .ollama:
            "Use the computer's LAN IP or .local hostname—not localhost or 0.0.0.0."
        case .gateway:
            "Use the computer's LAN IP or .local hostname and the gateway's port. Port 8700 is assumed if you leave it out."
        }
    }

    private var credentialSectionTitle: String {
        switch kind {
        case .ollama: "Authenticated reverse proxy"
        case .gateway: "Access token"
        }
    }

    private var credentialFieldPrompt: String {
        switch kind {
        case .ollama: "Optional bearer token"
        case .gateway: "gateway.token value"
        }
    }

    private var securityMessage: String {
        switch kind {
        case .ollama:
            "A default Ollama server has no authentication. Use trusted private networks only; use an authenticated HTTPS proxy or VPN for remote access."
        case .gateway:
            "The gateway token grants full control of the computer: anything Cagentic can run, read, or change. Treat it like a password, use it only on networks you trust, and never expose the gateway to the internet without an authenticated HTTPS proxy."
        }
    }

    private var navigationTitle: String {
        switch mode {
        case .onboarding: kind == .gateway ? "Connect to Cagentic" : "Connect to Ollama"
        case .add: "Add server"
        case .edit: "Edit server"
        }
    }

    private var actionTitle: String {
        switch mode {
        case .onboarding: "Connect"
        case .add: "Add"
        case .edit: "Save"
        }
    }

    private var progressTitle: String {
        switch mode {
        case .onboarding, .add: "Connecting…"
        case .edit: "Saving…"
        }
    }

    private var connectionSuccessAnnouncement: String {
        let name = serverName.trimmingCharacters(in: .whitespacesAndNewlines)
        let server = name.isEmpty ? kind.displayName : name
        switch mode {
        case .onboarding:
            return "Connected to \(server)."
        case .add:
            return "\(server) was added and connected."
        case .edit:
            return "\(server) was saved and connected."
        }
    }

    private var introductionTitle: String {
        switch mode {
        case .onboarding: "Your models, on your machine"
        case .add: kind == .gateway ? "Add a Cagentic gateway" : "Add another Ollama machine"
        case .edit: "Keep this connection current"
        }
    }

    private var introductionMessage: String {
        switch mode {
        case .onboarding:
            kind == .gateway
                ? "Cagentic on your computer does the work and this device drives it. Its chats and its tools stay on that machine."
                : "Cagentic talks directly to Ollama over your local network. Your conversations remain on this device and the computer running the model."
        case .add:
            "Cagentic verifies the address before saving it, then makes the new server active."
        case .edit:
            "Cagentic verifies these changes before saving them and making this server active."
        }
    }

    private var authenticationHelp: String {
        if connectsWithoutBearerToken {
            return "Cagentic will verify this address without authentication and remove the saved token only after the change is saved."
        }
        if requiresCredentialReentry {
            return "The address changed. Re-enter the token for the new address, or explicitly choose to connect without one."
        }
        if hasSavedCredential,
           bearerToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        {
            return "Leave this blank to keep the saved token. Enter a value only to replace it; credentials remain in Keychain."
        }
        if kind == .gateway {
            return "Required. Copy the gateway.token value from ~/.config/cagentic/config.json on the computer. It is stored in Keychain on this device."
        }
        return "Direct local Ollama does not need a token. If you use an HTTPS proxy, the token is stored in Keychain."
    }

    private var credentialUpdate: ServerCredentialUpdate {
        let cleanedToken = bearerToken.trimmingCharacters(in: .whitespacesAndNewlines)
        if connectsWithoutBearerToken {
            return .remove
        }
        if !cleanedToken.isEmpty {
            return .replaceBearerToken(cleanedToken)
        }
        return hasSavedCredential ? .preserveExisting : .remove
    }

    private func connectionResult() async -> Result<Void, Error> {
        switch mode {
        case .add:
            return await model.addServerConnection(
                serverURL: serverURL,
                serverName: serverName,
                kind: kind,
                bearerToken: bearerToken
            )
        case .edit:
            guard let profileID else {
                return await model.configureConnection(
                    serverURL: serverURL,
                    serverName: serverName,
                    kind: kind,
                    bearerToken: bearerToken
                )
            }
            return await model.updateConnection(
                profileID: profileID,
                serverURL: serverURL,
                serverName: serverName,
                kind: kind,
                credentialUpdate: credentialUpdate
            )
        case .onboarding:
            if let profileID {
                return await model.updateConnection(
                    profileID: profileID,
                    serverURL: serverURL,
                    serverName: serverName,
                    kind: kind,
                    credentialUpdate: credentialUpdate
                )
            }
            return await model.configureConnection(
                serverURL: serverURL,
                serverName: serverName,
                kind: kind,
                bearerToken: bearerToken
            )
        }
    }

    private func actionableMessage(_ error: Error) -> String {
        if let clientError = error as? OllamaClientError {
            return joined(clientError.errorDescription, clientError.recoverySuggestion)
        }
        if let gatewayError = error as? GatewayClientError {
            return joined(gatewayError.errorDescription, gatewayError.recoverySuggestion)
        }
        return error.localizedDescription
    }

    private func joined(_ parts: String?...) -> String {
        parts.compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
    }
}

private struct SetupStep: View {
    let number: Int
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: CagenticTheme.Spacing.sm) {
            Text(number, format: .number)
                .font(CagenticTheme.FontStyle.captionBold)
                .foregroundStyle(CagenticTheme.onAccent)
                .frame(width: 24, height: 24)
                .background(CagenticTheme.accent, in: Circle())
                .accessibilityHidden(true)
            Text(text)
                .font(CagenticTheme.FontStyle.body)
                .foregroundStyle(CagenticTheme.textPrimary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Step \(number): \(text)")
    }
}

#Preview("Connection setup") {
    ConnectionSetupView(model: .preview(disconnected: true))
}
