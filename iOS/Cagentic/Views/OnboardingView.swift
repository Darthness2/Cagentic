import SwiftUI

struct OnboardingView: View {
    @Bindable var model: AppModel

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    @State private var step = OnboardingStep.welcome
    @State private var direction = NavigationDirection.forward
    @State private var serverName: String
    @State private var serverAddress: String
    @State private var kind = ServerKind.ollama
    @State private var bearerToken: String
    @State private var isConnecting = false
    @State private var isSetupHelpExpanded = false
    @State private var isManualEntryExpanded = false
    @State private var inlineError: String?
    @State private var connectionTask: Task<Void, Never>?
    @State private var ownsConnectionAttempt = false

    @FocusState private var focusedField: ConnectionField?
    @AccessibilityFocusState private var isErrorFocused: Bool
    @AccessibilityFocusState private var focusedStep: OnboardingStep?

    private let scrollTopID = "onboarding-top"

    init(model: AppModel) {
        self.model = model

        let savedName = model.settings.serverName.trimmingCharacters(in: .whitespacesAndNewlines)
        _serverName = State(initialValue: savedName.isEmpty ? "My Ollama" : savedName)
        _serverAddress = State(initialValue: model.settings.serverURL)
        _bearerToken = State(initialValue: model.bearerToken)
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                header
                    .frame(maxWidth: 620, alignment: .leading)
                    .padding(.horizontal, horizontalPadding)
                    .padding(.top, CagenticTheme.Spacing.lg)
                    .padding(.bottom, CagenticTheme.Spacing.md)
                    .frame(maxWidth: .infinity)
                    .background(CagenticTheme.background)
                    .overlay(alignment: .bottom) {
                        Divider()
                    }
                    .zIndex(1)

                ScrollViewReader { proxy in
                    ScrollView {
                        Color.clear
                            .frame(height: 0)
                            .id(scrollTopID)

                        ZStack(alignment: .topLeading) {
                            content(for: step)
                                .id(step)
                                .transition(stepTransition)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .frame(maxWidth: 620, alignment: .leading)
                        .padding(.horizontal, horizontalPadding)
                        .padding(.top, CagenticTheme.Spacing.xl)
                        .padding(.bottom, CagenticTheme.Spacing.xl)
                        .frame(maxWidth: .infinity)
                    }
                    .scrollDismissesKeyboard(.interactively)
                    .onChange(of: step) { _, newStep in
                        focusedField = nil
                        if reduceMotion {
                            proxy.scrollTo(scrollTopID, anchor: .top)
                        } else {
                            withAnimation(.easeOut(duration: 0.2)) {
                                proxy.scrollTo(scrollTopID, anchor: .top)
                            }
                        }
                        Task { @MainActor in
                            await Task.yield()
                            focusedStep = newStep
                        }
                    }
                }
            }
            .background(CagenticTheme.background)
            .safeAreaInset(edge: .bottom, spacing: 0) {
                actionBar
            }
            .toolbar(.hidden, for: .navigationBar)
            .interactiveDismissDisabled(true)
            .task {
                focusedStep = step
            }
            .onDisappear {
                // A successful connection flips the onboarding flag as its final model
                // mutation. Do not cancel that completed operation while SwiftUI removes
                // this required full-screen cover.
                if ownsConnectionAttempt, !model.settings.hasCompletedOnboarding {
                    model.cancelConnectionAttempt()
                    connectionTask?.cancel()
                }
                connectionTask = nil
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
            HStack {
                BrandLockup(compact: true)
                Spacer(minLength: CagenticTheme.Spacing.md)
                // The progress bars below already say where you are, so this slot is worth more
                // as a way out: somebody who opened the app before setting up their computer
                // should not be trapped in a form they cannot complete.
                Button("Set up later") {
                    model.skipOnboarding()
                }
                .font(CagenticTheme.FontStyle.subheadlineMedium)
                .foregroundStyle(CagenticTheme.textSecondary)
                .frame(minHeight: 44)
                .disabled(isConnectionInProgress)
                .accessibilityHint("Finishes setup without connecting. You can add a computer any time.")
            }

            HStack(spacing: CagenticTheme.Spacing.xs) {
                ForEach(OnboardingStep.allCases) { item in
                    Capsule()
                        .fill(item.rawValue <= step.rawValue
                            ? CagenticTheme.accent
                            : CagenticTheme.border)
                        .frame(height: 4)
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Onboarding progress")
            .accessibilityValue("Step \(step.position) of \(OnboardingStep.allCases.count), \(step.accessibilityTitle)")
        }
    }

    @ViewBuilder
    private func content(for step: OnboardingStep) -> some View {
        switch step {
        case .welcome:
            welcomeStep
        case .connection:
            connectionStep
        }
    }

    private var welcomeStep: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xl) {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.md) {
                BrandMark(size: 58)

                Text("Your models. Your space.")
                    .font(CagenticTheme.FontStyle.display)
                    .foregroundStyle(CagenticTheme.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityFocused($focusedStep, equals: .welcome)

                // One sentence. The backend choice moved to the screen where it is acted on, so
                // this page has nothing to decide and nothing to configure — it says what the app
                // is and gets out of the way.
                Text("Cagentic talks to a computer you control. No account, no cloud in between.")
                    .font(CagenticTheme.FontStyle.title3)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
                OnboardingValue(
                    systemImage: "lock.shield",
                    title: "Private by default",
                    detail: "Your chats stay between this device and your computer."
                )
                OnboardingValue(
                    systemImage: "bolt.horizontal.circle",
                    title: "Answers as they are written",
                    detail: "Replies stream over your own network."
                )
            }
        }
    }

    /// The computer-side setup, available on demand rather than as a step everybody must read.
    ///
    /// It used to be the middle of three screens: a wall of environment variables and config keys
    /// standing between a new person and the one thing they came to do. Most people never need it —
    /// `/token` does the work now — so it waits behind a disclosure for the ones who do.
    private var setupHelp: some View {
        DisclosureGroup(isExpanded: $isSetupHelpExpanded) {
            switch kind {
            case .ollama: ollamaSetupInstructions
            case .gateway: gatewaySetupInstructions
            }
        } label: {
            Text(kind == .gateway
                ? "How do I set this up on the computer?"
                : "How do I make Ollama reachable?")
                .font(CagenticTheme.FontStyle.calloutMedium)
                .foregroundStyle(CagenticTheme.accent)
                .frame(minHeight: 44)
        }
        .tint(CagenticTheme.accent)
    }

    private var gatewaySetupInstructions: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
                OnboardingInstruction(number: 1, title: "Ask Cagentic for a code") {
                    VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
                        Text(
                            "In Cagentic on the computer — the terminal or the web page — type:"
                        )
                            .foregroundStyle(CagenticTheme.textSecondary)

                        Text("/token")
                            .font(.body.monospaced())
                            .foregroundStyle(CagenticTheme.textPrimary)
                            .textSelection(.enabled)
                            .padding(CagenticTheme.Spacing.sm)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(
                                CagenticTheme.surfaceRaised,
                                in: .rect(cornerRadius: CagenticTheme.Radius.control)
                            )
                    }
                }

                Divider()

                OnboardingInstruction(number: 2, title: "Let it open the door") {
                    Text(
                        "It asks once whether devices on your Wi-Fi may connect, sets everything "
                            + "up for you, and shows a six-digit code."
                    )
                        .foregroundStyle(CagenticTheme.textSecondary)
                }

                Divider()

                OnboardingInstruction(number: 3, title: "Pick the computer on the next step") {
                    Text(
                        "It appears by name once it is running — no address to find, and nothing "
                            + "to copy across but the six digits."
                    )
                        .foregroundStyle(CagenticTheme.textSecondary)
                }
            }

            Label {
                Text(
                    "That token can run commands and read files on the computer. Keep it on "
                        + "networks you trust, and never expose the gateway to the internet without "
                        + "an authenticated HTTPS proxy in front of it."
                )
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "exclamationmark.shield")
                    .foregroundStyle(CagenticTheme.warning)
            }
            .font(CagenticTheme.FontStyle.callout)
            .foregroundStyle(CagenticTheme.textSecondary)
            .accessibilityElement(children: .combine)
        }
    }

    private var ollamaSetupInstructions: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
                OnboardingInstruction(number: 1, title: "Set the Ollama host") {
                    VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
                        Text("Set this environment variable for the Ollama app or service, then fully restart Ollama.")
                            .foregroundStyle(CagenticTheme.textSecondary)

                        Text("OLLAMA_HOST=0.0.0.0:11434")
                            .font(.body.monospaced())
                            .foregroundStyle(CagenticTheme.textPrimary)
                            .textSelection(.enabled)
                            .padding(.horizontal, CagenticTheme.Spacing.sm)
                            .padding(.vertical, CagenticTheme.Spacing.sm)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(
                                CagenticTheme.surfaceRaised,
                                in: .rect(cornerRadius: CagenticTheme.Radius.control)
                            )
                            .accessibilityLabel(
                                "Ollama host environment variable: O L L A M A underscore "
                                    + "H O S T equals 0 point 0 point 0 point 0 colon 1 1 4 3 4"
                            )
                    }
                }

                Divider()

                OnboardingInstruction(number: 2, title: "Allow private-network access") {
                    Text(
                        "If your computer asks, allow Ollama through its firewall on private "
                            + "networks. Keep both devices on the same trusted Wi-Fi or LAN."
                    )
                        .foregroundStyle(CagenticTheme.textSecondary)
                }

                Divider()

                OnboardingInstruction(number: 3, title: "Find the computer’s LAN address") {
                    VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                        Text(
                            "Use an address such as the one below on the next step. "
                                + "Do not use localhost or 0.0.0.0 from this device."
                        )
                            .foregroundStyle(CagenticTheme.textSecondary)
                        Text("http://192.168.1.42:11434")
                            .font(.body.monospaced())
                            .foregroundStyle(CagenticTheme.accent)
                            .textSelection(.enabled)
                    }
                }
            }

            Label {
                Text(
                    "A default Ollama server has no authentication. Never expose it directly "
                        + "to the public internet; use a trusted LAN, VPN, or authenticated HTTPS proxy."
                )
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "exclamationmark.shield")
                    .foregroundStyle(CagenticTheme.warning)
            }
            .font(CagenticTheme.FontStyle.callout)
            .foregroundStyle(CagenticTheme.textSecondary)
            .accessibilityElement(children: .combine)
        }
    }

    private var connectionStep: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xl) {
            stepHeading(
                step: .connection,
                eyebrow: "On this device",
                title: "Connect a computer",
                detail: "Cagentic checks it before saving anything."
            )

            // The one decision worth making, asked where it is acted on rather than a screen
            // earlier, so the answer and its consequences are visible together.
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                Picker("What are you connecting to?", selection: $kind) {
                    ForEach(ServerKind.allCases) { option in
                        Text(option.displayName).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .accessibilityLabel("What are you connecting to?")

                Text(kindDetail)
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .onChange(of: kind) { _, newKind in
                let currentDefault = newKind == .gateway
                    ? ServerKind.ollama.defaultServerName
                    : ServerKind.gateway.defaultServerName
                if serverName.trimmingCharacters(in: .whitespacesAndNewlines) == currentDefault {
                    serverName = newKind.defaultServerName
                }
            }

            if kind == .gateway {
                Label {
                    Text("Type /token on the computer. It shows six digits — that is all you need.")
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "wifi")
                        .foregroundStyle(CagenticTheme.accent)
                }
                .font(CagenticTheme.FontStyle.callout)
                .foregroundStyle(CagenticTheme.textSecondary)
                .accessibilityElement(children: .combine)
            }

            setupHelp

            manualFields
        }
    }

    /// Typing an address by hand: the only route for Ollama, and the fallback for a gateway on a
    /// network where discovery is blocked.
    @ViewBuilder
    private var manualFields: some View {
        if kind == .gateway {
            DisclosureGroup(isExpanded: $isManualEntryExpanded) {
                connectionFields
                    .padding(.top, CagenticTheme.Spacing.sm)
            } label: {
                Text("Enter an address instead")
                    .font(CagenticTheme.FontStyle.calloutMedium)
                    .foregroundStyle(CagenticTheme.accent)
                    .frame(minHeight: 44)
            }
            .tint(CagenticTheme.accent)
        } else {
            connectionFields
        }
    }

    private var connectionFields: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xl) {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
                OnboardingTextFieldLabel(
                    title: "Connection name",
                    detail: "A familiar name shown in Cagentic"
                ) {
                    TextField("Studio PC", text: $serverName)
                        .textInputAutocapitalization(.words)
                        .textContentType(.name)
                        .submitLabel(.next)
                        .focused($focusedField, equals: .name)
                        .onSubmit { focusedField = .address }
                }

                OnboardingTextFieldLabel(
                    title: "Server address",
                    detail: kind == .gateway
                        ? "LAN IP, .local hostname, or Tailscale name — plain, without https://"
                        : "The computer’s LAN IP, .local hostname, or Tailscale name"
                ) {
                    TextField(
                        kind == .gateway
                            ? "http://192.168.1.42:8700"
                            : "http://192.168.1.42:11434",
                        text: $serverAddress
                    )
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textContentType(.URL)
                        .submitLabel(.next)
                        .focused($focusedField, equals: .address)
                        .onSubmit { focusedField = .token }
                }

                OnboardingTextFieldLabel(
                    title: kind == .gateway ? "Access token" : "Bearer token",
                    detail: kind == .gateway
                        ? "Required; the six digits from /token, or a gateway.token value"
                        : "Optional; only for an authenticated reverse proxy"
                ) {
                    SecureField(
                        kind == .gateway ? "Six digits, or a token" : "Optional token",
                        text: $bearerToken
                    )
                        .textContentType(.password)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.go)
                        .focused($focusedField, equals: .token)
                        .onSubmit {
                            if canConnect {
                                connect()
                            }
                        }
                }
            }
            .disabled(isConnectionInProgress)

            if let inlineError {
                Label {
                    Text(inlineError)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill")
                }
                .font(CagenticTheme.FontStyle.callout)
                .foregroundStyle(CagenticTheme.error)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Connection error: \(inlineError)")
                .accessibilityFocused($isErrorFocused)
            }

            if cleartextTokenWarningApplies {
                Label {
                    Text(
                        "This address is not encrypted, so the token crosses your local network "
                            + "in the clear. Use it only on a network you trust."
                    )
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "lock.open")
                        .foregroundStyle(CagenticTheme.warning)
                }
                .font(CagenticTheme.FontStyle.callout)
                .foregroundStyle(CagenticTheme.textSecondary)
                .accessibilityElement(children: .combine)
            }

            Label("Tokens are stored securely in Keychain.", systemImage: "key")
                .font(CagenticTheme.FontStyle.callout)
                .foregroundStyle(CagenticTheme.textSecondary)
                .accessibilityElement(children: .combine)
        }
    }

    private func stepHeading(
        step: OnboardingStep,
        eyebrow: String,
        title: String,
        detail: String
    ) -> some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
            Text(eyebrow)
                .font(CagenticTheme.FontStyle.subheadlineSemibold)
                .foregroundStyle(CagenticTheme.accent)
            Text(title)
                .font(CagenticTheme.FontStyle.display)
                .foregroundStyle(CagenticTheme.textPrimary)
                .accessibilityAddTraits(.isHeader)
                .accessibilityFocused($focusedStep, equals: step)
            Text(detail)
                .font(CagenticTheme.FontStyle.title3)
                .foregroundStyle(CagenticTheme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var actionBar: some View {
        VStack(spacing: 0) {
            Divider()

            Group {
                if dynamicTypeSize.isAccessibilitySize {
                    VStack(spacing: CagenticTheme.Spacing.sm) {
                        primaryButton
                        backButton
                    }
                } else {
                    HStack(spacing: CagenticTheme.Spacing.md) {
                        backButton
                        Spacer(minLength: CagenticTheme.Spacing.md)
                        primaryButton
                    }
                }
            }
            .frame(maxWidth: 620)
            .padding(.horizontal, horizontalPadding)
            .padding(.vertical, CagenticTheme.Spacing.sm)
            .frame(maxWidth: .infinity)
        }
        .background(CagenticTheme.background)
    }

    @ViewBuilder
    private var backButton: some View {
        if isConnectionInProgress {
            Button("Stop", systemImage: "xmark") {
                stopConnectionAttempt()
            }
            .buttonStyle(.bordered)
            .controlSize(.large)
            .accessibilityHint("Cancels this connection attempt and keeps the form open.")
        } else if step != .welcome {
            Button("Back", systemImage: "chevron.left") {
                moveBackward()
            }
            .buttonStyle(.bordered)
            .controlSize(.large)
        }
    }

    private var primaryButton: some View {
        Button(action: primaryAction) {
            HStack(spacing: CagenticTheme.Spacing.xs) {
                if isConnectionInProgress {
                    ProgressView()
                        .controlSize(.small)
                        .tint(CagenticTheme.onAccent)
                        .accessibilityHidden(true)
                }
                Text(primaryButtonTitle)
                    .lineLimit(1)
                if step != .connection, !isConnectionInProgress {
                    Image(systemName: "arrow.right")
                        .accessibilityHidden(true)
                }
            }
            .padding(.horizontal, CagenticTheme.Spacing.xs)
        }
        .buttonStyle(.borderedProminent)
        .tint(CagenticTheme.accent)
        .controlSize(.large)
        .disabled(step == .connection && !gatewayUsesDiscovery && !canConnect)
        .accessibilityHint(primaryButtonHint)
    }

    /// True when the gateway path is still offering discovery rather than a typed address.
    private var gatewayUsesDiscovery: Bool {
        step == .connection && kind == .gateway && !isManualEntryExpanded
    }

    private var primaryButtonTitle: String {
        if isConnectionInProgress {
            return "Connecting…"
        }
        if step == .welcome {
            return "Continue"
        }
        return gatewayUsesDiscovery ? "Find on Wi-Fi" : "Connect"
    }

    private var primaryButtonHint: String {
        switch step {
        case .welcome:
            "Shows the connection screen."
        case .connection:
            gatewayUsesDiscovery
                ? "Looks for computers running Cagentic on your Wi-Fi."
                : "Verifies the computer and loads what it offers."
        }
    }

    private var canConnect: Bool {
        guard !isConnectionInProgress,
              !serverAddress.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return false
        }
        // Every gateway route is token-gated; there is no anonymous connection to try.
        guard kind == .gateway else { return true }
        return !bearerToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var kindDetail: String {
        switch kind {
        case .ollama:
            "Ollama on your computer answers with text. Chats are kept on this device."
        case .gateway:
            "Cagentic on your computer can read files, run commands, and search — asking your approval each time. Those chats live on that computer."
        }
    }

    private var cleartextTokenWarningApplies: Bool {
        guard kind == .gateway,
              !bearerToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let endpoint = try? GatewayEndpoint(serverAddress)
        else {
            return false
        }
        return endpoint.sendsTokenInClear
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

    private var horizontalPadding: CGFloat {
        dynamicTypeSize.isAccessibilitySize
            ? CagenticTheme.Spacing.md
            : CagenticTheme.Spacing.lg
    }

    private var stepTransition: AnyTransition {
        guard !reduceMotion else { return .identity }

        let insertionEdge: Edge = direction == .forward ? .trailing : .leading
        let removalEdge: Edge = direction == .forward ? .leading : .trailing
        return .asymmetric(
            insertion: .move(edge: insertionEdge).combined(with: .opacity),
            removal: .move(edge: removalEdge).combined(with: .opacity)
        )
    }

    private func primaryAction() {
        focusedField = nil
        switch step {
        case .welcome:
            moveForward()
        case .connection:
            if gatewayUsesDiscovery {
                model.presentedSheet = .pairGateway
            } else {
                connect()
            }
        }
    }

    private func moveForward() {
        guard let next = OnboardingStep(rawValue: step.rawValue + 1) else { return }
        direction = .forward
        withAnimation(reduceMotion ? nil : .snappy(duration: 0.34, extraBounce: 0)) {
            step = next
        }
    }

    private func moveBackward() {
        guard let previous = OnboardingStep(rawValue: step.rawValue - 1) else { return }
        focusedField = nil
        inlineError = nil
        direction = .backward
        withAnimation(reduceMotion ? nil : .snappy(duration: 0.34, extraBounce: 0)) {
            step = previous
        }
    }

    private func connect() {
        guard canConnect else { return }

        focusedField = nil
        isConnecting = true
        ownsConnectionAttempt = true
        inlineError = nil
        isErrorFocused = false

        connectionTask = Task { @MainActor in
            let result = await model.configureConnection(
                serverURL: serverAddress,
                serverName: serverName,
                kind: kind,
                bearerToken: bearerToken
            )
            let wasCancelled = Task.isCancelled

            isConnecting = false
            ownsConnectionAttempt = false
            connectionTask = nil
            guard !wasCancelled else { return }

            switch result {
            case .success:
                // AppModel owns the onboarding flag and persistence. The presenting root view
                // dismisses this required full-screen flow when that state becomes true.
                break
            case .failure(let error):
                guard !(error is CancellationError) else { return }
                inlineError = actionableMessage(error)
                isErrorFocused = true
            }
        }
    }

    private func stopConnectionAttempt() {
        guard isConnectionInProgress else { return }
        model.cancelConnectionAttempt()
        connectionTask?.cancel()
        connectionTask = nil
        ownsConnectionAttempt = false
        isConnecting = false
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

private enum OnboardingStep: Int, CaseIterable, Hashable, Identifiable {
    case welcome
    case connection

    var id: Self { self }
    var position: Int { rawValue + 1 }

    var accessibilityTitle: String {
        switch self {
        case .welcome: "Welcome"
        case .connection: "Connect a computer"
        }
    }
}

private enum NavigationDirection: Equatable {
    case forward
    case backward
}

private enum ConnectionField: Hashable {
    case name
    case address
    case token
}

private struct OnboardingValue: View {
    let systemImage: String
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: CagenticTheme.Spacing.md) {
            Image(systemName: systemImage)
                .font(.title3)
                .foregroundStyle(CagenticTheme.accent)
                .frame(width: 30, height: 30)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
                Text(title)
                    .font(CagenticTheme.FontStyle.headline)
                    .foregroundStyle(CagenticTheme.textPrimary)
                Text(detail)
                    .font(CagenticTheme.FontStyle.body)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct OnboardingInstruction<Content: View>: View {
    let number: Int
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        HStack(alignment: .top, spacing: CagenticTheme.Spacing.md) {
            Text(number, format: .number)
                .font(CagenticTheme.FontStyle.captionBold)
                .foregroundStyle(CagenticTheme.onAccent)
                .frame(width: 28, height: 28)
                .background(CagenticTheme.accent, in: Circle())
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                Text(title)
                    .font(CagenticTheme.FontStyle.headline)
                    .foregroundStyle(CagenticTheme.textPrimary)
                content
                    .font(CagenticTheme.FontStyle.body)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Step \(number): \(title)")
    }
}

private struct OnboardingTextFieldLabel<Field: View>: View {
    let title: String
    let detail: String
    @ViewBuilder let field: Field

    var body: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
            Text(title)
                .font(CagenticTheme.FontStyle.subheadlineSemibold)
                .foregroundStyle(CagenticTheme.textPrimary)

            field
                .font(CagenticTheme.FontStyle.body)
                .accessibilityLabel(title)
                .accessibilityHint(detail)
                .padding(.horizontal, CagenticTheme.Spacing.sm)
                .frame(minHeight: 50)
                .background(
                    CagenticTheme.surface,
                    in: .rect(cornerRadius: CagenticTheme.Radius.control)
                )
                .overlay {
                    RoundedRectangle(cornerRadius: CagenticTheme.Radius.control)
                        .stroke(CagenticTheme.border, lineWidth: 0.75)
                }

            Text(detail)
                .font(CagenticTheme.FontStyle.caption)
                .foregroundStyle(CagenticTheme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

#Preview("Onboarding") {
    OnboardingView(model: .preview(disconnected: true))
}

#Preview("Onboarding · dark") {
    OnboardingView(model: .preview(disconnected: true, appearance: .dark))
        .preferredColorScheme(.dark)
}
