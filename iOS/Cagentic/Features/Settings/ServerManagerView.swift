import SwiftUI
import UIKit

struct ServerManagerView: View {
    @Bindable var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dismiss) private var dismiss

    @State private var presentedEditor: ServerEditor?
    @State private var deleteCandidate: ServerProfile?
    @State private var activatingProfileID: ServerProfileID?
    @State private var testingProfileID: ServerProfileID?
    @State private var deletingProfileID: ServerProfileID?
    @State private var operationError: String?
    @State private var testSuccess: ServerTestFeedback?
    @State private var operationTask: Task<Void, Never>?
    @State private var operationToken: UUID?
    @State private var operationKind: ServerOperationKind?
    @State private var isStoppingOperation = false
    @AccessibilityFocusState private var isErrorFocused: Bool
    @AccessibilityFocusState private var isSuccessFocused: Bool

    var body: some View {
        NavigationStack {
            List {
                if model.serverProfiles.isEmpty {
                    emptyState
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                } else {
                    overviewSection
                    profilesSection
                    privacySection
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .background(CagenticTheme.background)
            .navigationTitle("Servers")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if canStopOperation {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Stop", role: .cancel) {
                            stopCurrentOperation()
                        }
                    }
                } else {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") {
                            dismiss()
                        }
                        .disabled(deletingProfileID != nil)
                    }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button("Add server", systemImage: "plus") {
                        presentEditor(.add)
                    }
                    .disabled(isOperationLocked || model.isGenerating)
                }
            }
            .animation(
                reduceMotion ? nil : .snappy(duration: 0.26),
                value: model.serverProfiles.map(\.id)
            )
            .animation(
                reduceMotion ? nil : .snappy(duration: 0.22),
                value: model.activeServerProfile?.id
            )
            .interactiveDismissDisabled(deletingProfileID != nil)
            .sheet(item: $presentedEditor) { editor in
                switch editor {
                case .add:
                    ConnectionSetupView(model: model, mode: .add)
                        .presentationDetents([.large])
                        .presentationDragIndicator(.visible)
                case .edit(let profileID):
                    ConnectionSetupView(model: model, profileID: profileID, mode: .edit)
                        .presentationDetents([.large])
                        .presentationDragIndicator(.visible)
                }
            }
            .confirmationDialog(
                deleteTitle,
                isPresented: isDeleteConfirmationPresented,
                titleVisibility: .visible
            ) {
                if let deleteCandidate {
                    Button("Remove server", role: .destructive) {
                        deleteServer(deleteCandidate)
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(deleteMessage)
            }
            .onDisappear {
                switch operationKind {
                case .activate:
                    model.cancelConnectionAttempt()
                    operationTask?.cancel()
                case .test:
                    operationTask?.cancel()
                case .delete, .none:
                    break
                }
            }
        }
    }

    private var overviewSection: some View {
        Section {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                Text("Move between your computers")
                    .font(CagenticTheme.FontStyle.heading)
                    .foregroundStyle(CagenticTheme.textPrimary)
                Text("Tap a server to verify it and make it active. Each connection keeps its own model choice and Keychain credential.")
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.textSecondary)
            }
            .padding(.vertical, CagenticTheme.Spacing.xxs)
            .accessibilityElement(children: .combine)
        }
        .listRowBackground(CagenticTheme.surface)
    }

    private var profilesSection: some View {
        Section {
            ForEach(model.serverProfiles) { profile in
                VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
                    ServerProfileRow(
                        profile: profile,
                        isActive: profile.id == model.activeServerProfile?.id,
                        isActivating: profile.id == activatingProfileID,
                        isTesting: profile.id == testingProfileID,
                        isDeleting: profile.id == deletingProfileID,
                        isInteractionDisabled: isOperationLocked || model.isGenerating,
                        reduceMotion: reduceMotion,
                        activate: { activate(profile) },
                        test: { testConnection(profile) },
                        edit: { presentEditor(.edit(profile.id)) },
                        requestDelete: { requestDelete(profile) }
                    )

                    if let testSuccess, testSuccess.profileID == profile.id {
                        Label(testSuccess.message, systemImage: "checkmark.circle.fill")
                            .font(CagenticTheme.FontStyle.caption)
                            .foregroundStyle(CagenticTheme.success)
                            .padding(.leading, 44)
                            .padding(.bottom, CagenticTheme.Spacing.xxs)
                            .accessibilityLabel(
                                "Connection test succeeded for \(profile.displayName). "
                                    + testSuccess.message
                            )
                            .accessibilityFocused($isSuccessFocused)
                    }
                }
            }

            if let operationError {
                Label(operationError, systemImage: "exclamationmark.triangle.fill")
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.error)
                    .accessibilityLabel("Server error: \(operationError)")
                    .accessibilityFocused($isErrorFocused)
            }
        } header: {
            Text("Saved servers").textCase(nil)
        } footer: {
            if model.isGenerating {
                Text("Finish or stop the current response before switching servers.")
            }
        }
        .listRowBackground(CagenticTheme.surface)
    }

    private var privacySection: some View {
        Section {
            Label {
                Text("Bearer tokens are stored separately in Keychain. Removing a server also removes its saved credential.")
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.textSecondary)
            } icon: {
                Image(systemName: "key.fill")
                    .foregroundStyle(CagenticTheme.accent)
            }
            .accessibilityElement(children: .combine)
        }
        .listRowBackground(CagenticTheme.surface)
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("No saved servers", systemImage: "desktopcomputer")
        } description: {
            Text("Add an Ollama server or a Cagentic gateway to start a conversation.")
        } actions: {
            Button("Add server") {
                presentEditor(.add)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .frame(maxWidth: .infinity, minHeight: 360)
    }

    private var isOperationLocked: Bool {
        operationKind != nil
    }

    private var canStopOperation: Bool {
        guard !isStoppingOperation else { return false }
        return operationKind == .activate || operationKind == .test
    }

    private var isDeleteConfirmationPresented: Binding<Bool> {
        Binding(
            get: { deleteCandidate != nil },
            set: { isPresented in
                if !isPresented {
                    deleteCandidate = nil
                }
            }
        )
    }

    private var deleteTitle: String {
        guard let deleteCandidate else { return "Remove server?" }
        return "Remove \(deleteCandidate.displayName)?"
    }

    private var deleteMessage: String {
        guard let deleteCandidate else { return "This removes the saved connection." }
        if model.serverProfiles.count == 1 {
            return "This is your only saved server. Cagentic will return to connection setup. Your conversations stay on this device."
        }
        if deleteCandidate.id == model.activeServerProfile?.id {
            return "Cagentic will remove its Keychain credential and connect to another saved server. Your conversations stay on this device."
        }
        return "Cagentic will remove this connection and its Keychain credential. Your conversations stay on this device."
    }

    private func activate(_ profile: ServerProfile) {
        guard profile.id != model.activeServerProfile?.id,
              !isOperationLocked,
              !model.isGenerating
        else {
            return
        }
        clearFeedback()
        activatingProfileID = profile.id
        let token = beginOperation(.activate)
        operationTask = Task {
            let result = await model.activateServer(profile.id)
            guard operationToken == token else { return }
            activatingProfileID = nil
            let wasCancelled = Task.isCancelled
            finishOperation(token)
            guard !wasCancelled else { return }
            switch result {
            case .success:
                UIAccessibility.post(
                    notification: .announcement,
                    argument: "Connected to \(profile.displayName)."
                )
            case .failure(let error):
                guard !(error is CancellationError) else { return }
                presentOperationError(error)
            }
        }
    }

    private func testConnection(_ profile: ServerProfile) {
        guard !isOperationLocked, !model.isGenerating else { return }
        clearFeedback()
        testingProfileID = profile.id
        let token = beginOperation(.test)
        operationTask = Task {
            let result = await model.testServerConnection(profile.id)
            guard operationToken == token else { return }
            testingProfileID = nil
            let wasCancelled = Task.isCancelled
            finishOperation(token)
            guard !wasCancelled else { return }
            switch result {
            case .success(let test):
                let modelLabel = test.modelCount == 1 ? "1 model" : "\(test.modelCount) models"
                testSuccess = ServerTestFeedback(
                    profileID: profile.id,
                    message: "Connected · Ollama \(test.serverVersion) · \(modelLabel)"
                )
                isSuccessFocused = true
            case .failure(let error):
                guard !(error is CancellationError) else { return }
                presentOperationError(error)
            }
        }
    }

    private func deleteServer(_ profile: ServerProfile) {
        guard !isOperationLocked, !model.isGenerating else { return }
        let wasLastServer = model.serverProfiles.count == 1
        clearFeedback()
        deletingProfileID = profile.id
        deleteCandidate = nil
        let token = beginOperation(.delete)
        operationTask = Task {
            let result = await model.deleteServer(profile.id)
            guard operationToken == token else { return }
            deletingProfileID = nil
            let wasCancelled = Task.isCancelled
            finishOperation(token)
            guard !wasCancelled else { return }
            switch result {
            case .success:
                if wasLastServer {
                    dismiss()
                }
            case .failure(let error):
                guard !(error is CancellationError) else { return }
                presentOperationError(error)
            }
        }
    }

    private func presentEditor(_ editor: ServerEditor) {
        guard !isOperationLocked, !model.isGenerating else { return }
        clearFeedback()
        presentedEditor = editor
    }

    private func requestDelete(_ profile: ServerProfile) {
        guard !isOperationLocked, !model.isGenerating else { return }
        clearFeedback()
        deleteCandidate = profile
    }

    private func clearFeedback() {
        operationError = nil
        testSuccess = nil
        isErrorFocused = false
        isSuccessFocused = false
    }

    private func beginOperation(_ kind: ServerOperationKind) -> UUID {
        let token = UUID()
        operationToken = token
        operationKind = kind
        isStoppingOperation = false
        return token
    }

    private func finishOperation(_ token: UUID) {
        guard operationToken == token else { return }
        operationTask = nil
        operationToken = nil
        operationKind = nil
        isStoppingOperation = false
    }

    private func stopCurrentOperation() {
        guard canStopOperation else { return }
        let stoppedKind = operationKind
        isStoppingOperation = true
        operationTask?.cancel()
        if stoppedKind == .activate {
            model.cancelConnectionAttempt()
        }
        activatingProfileID = nil
        testingProfileID = nil
        clearFeedback()
        UIAccessibility.post(
            notification: .announcement,
            argument: stoppedKind == .test ? "Connection test stopped." : "Connection stopped."
        )
    }

    private func presentOperationError(_ error: Error) {
        operationError = actionableMessage(error)
        isErrorFocused = true
    }

    private func actionableMessage(_ error: Error) -> String {
        guard let clientError = error as? OllamaClientError else {
            return error.localizedDescription
        }
        return [clientError.errorDescription, clientError.recoverySuggestion]
            .compactMap { $0 }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }
}

private enum ServerOperationKind {
    case activate
    case test
    case delete
}

private struct ServerTestFeedback: Equatable {
    let profileID: ServerProfileID
    let message: String
}

private enum ServerEditor: Identifiable {
    case add
    case edit(ServerProfileID)

    var id: String {
        switch self {
        case .add:
            "add-server"
        case .edit(let profileID):
            "edit-server-\(profileID.description)"
        }
    }
}

private struct ServerProfileRow: View {
    let profile: ServerProfile
    let isActive: Bool
    let isActivating: Bool
    let isTesting: Bool
    let isDeleting: Bool
    let isInteractionDisabled: Bool
    let reduceMotion: Bool
    let activate: () -> Void
    let test: () -> Void
    let edit: () -> Void
    let requestDelete: () -> Void

    var body: some View {
        HStack(spacing: CagenticTheme.Spacing.xs) {
            Button(action: activate) {
                HStack(spacing: CagenticTheme.Spacing.sm) {
                    serverIcon
                    details
                    Spacer(minLength: CagenticTheme.Spacing.xs)
                    stateIndicator
                }
                .contentShape(.rect)
                .frame(minHeight: 52)
            }
            .buttonStyle(.plain)
            .disabled(isInteractionDisabled)
            .accessibilityLabel(profile.displayName)
            .accessibilityValue(accessibilityValue)
            .accessibilityHint(
                isActive
                    ? "This server is currently active."
                    : "Switches to this server after verifying the connection."
            )

            Menu {
                Button("Test connection", systemImage: "network") {
                    test()
                }
                Button("Edit connection", systemImage: "pencil") {
                    edit()
                }
                Button("Remove server", systemImage: "trash", role: .destructive) {
                    requestDelete()
                }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .frame(width: 44, height: 44)
                    .contentShape(.rect)
            }
            .disabled(isInteractionDisabled)
            .accessibilityLabel("Actions for \(profile.displayName), \(profile.endpoint)")
        }
        .animation(reduceMotion ? nil : .snappy(duration: 0.2), value: isActive)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.16), value: isActivating)
    }

    private var accessibilityValue: String {
        let state = isActive ? "Active server" : "Inactive server"
        // The badge is decorative for VoiceOver, so the kind is spoken here instead.
        let base = "\(state), \(profile.kind.displayName), \(profile.endpoint)"
        if profile.selectedModel.isEmpty {
            return base
        }
        return "\(base), model \(profile.selectedModel)"
    }

    private var serverIcon: some View {
        Image(systemName: iconName)
            .font(.body.weight(.medium))
            .foregroundStyle(isActive ? CagenticTheme.accent : CagenticTheme.textSecondary)
            .frame(width: 36, height: 36)
            .background(
                isActive ? CagenticTheme.accentSoft : CagenticTheme.surfaceRaised,
                in: .rect(cornerRadius: CagenticTheme.Radius.control)
            )
            .accessibilityHidden(true)
    }

    /// The two backends behave differently enough that the list has to say which is which.
    private var iconName: String {
        switch profile.kind {
        case .ollama: isActive ? "desktopcomputer.and.macbook" : "desktopcomputer"
        case .gateway: "sparkles.rectangle.stack"
        }
    }

    private var details: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
            // The name gets the whole line. It is user-chosen and can be long, and a badge sharing
            // the line will always win a width fight it should lose — which is how "My gateway"
            // ended up rendering as "My gat…" beside a two-line badge.
            Text(profile.displayName)
                .font(CagenticTheme.FontStyle.bodySemibold)
                .foregroundStyle(CagenticTheme.textPrimary)
                .lineLimit(1)

            Text(profile.endpoint)
                .font(CagenticTheme.FontStyle.metadata)
                .foregroundStyle(CagenticTheme.textSecondary)
                .lineLimit(1)
                .truncationMode(.middle)

            HStack(spacing: CagenticTheme.Spacing.xxs) {
                Text(profile.kind.displayName)
                    .font(CagenticTheme.FontStyle.caption2Bold)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .lineLimit(1)
                    .fixedSize()
                    .padding(.horizontal, CagenticTheme.Spacing.xxs)
                    .padding(.vertical, 2)
                    .background(CagenticTheme.surfaceRaised, in: .capsule)

                if !profile.selectedModel.isEmpty {
                    Text(profile.selectedModel.replacingOccurrences(of: ":latest", with: ""))
                        .font(CagenticTheme.FontStyle.caption)
                        .foregroundStyle(CagenticTheme.textTertiary)
                        .lineLimit(1)
                }
            }
            .accessibilityHidden(true)
        }
    }

    @ViewBuilder
    private var stateIndicator: some View {
        if isActivating || isTesting || isDeleting {
            ProgressView()
                .controlSize(.small)
                .tint(CagenticTheme.accent)
                .frame(width: 52)
                .frame(minHeight: 44)
                .accessibilityLabel(progressAccessibilityLabel)
        } else if isActive {
            Text("Active")
                .font(CagenticTheme.FontStyle.captionSemibold)
                .foregroundStyle(CagenticTheme.accent)
                .padding(.horizontal, CagenticTheme.Spacing.xs)
                .padding(.vertical, CagenticTheme.Spacing.xxs)
                .background(CagenticTheme.accentSoft, in: .capsule)
                .transition(.opacity)
                .accessibilityHidden(true)
        } else {
            Image(systemName: "arrow.left.arrow.right")
                .font(.caption.weight(.semibold))
                .foregroundStyle(CagenticTheme.textTertiary)
                .frame(width: 44, height: 44)
                .accessibilityHidden(true)
        }
    }

    private var progressAccessibilityLabel: String {
        if isDeleting {
            return "Removing server"
        }
        if isTesting {
            return "Testing server connection"
        }
        return "Connecting to server"
    }
}

#Preview("Server manager") {
    ServerManagerView(model: .preview())
}
