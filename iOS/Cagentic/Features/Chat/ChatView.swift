import SwiftUI
import UniformTypeIdentifiers

struct ChatView: View {
    @Bindable var model: AppModel
    let conversationID: UUID
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isNearBottom = true
    /// Whether the transcript should keep following a streaming reply.
    ///
    /// Distinct from `isNearBottom`, which is pure geometry: appending a large block of text moves
    /// the bottom out of reach on its own, and keying "keep following" off that would strand the
    /// reader mid-answer with a Latest button they never asked for. Only a real drag stops the
    /// follow, and returning to the bottom resumes it.
    @State private var followsStream = true
    @State private var isUserScrolling = false
    @State private var pendingDeletion: ChatDeletion?
    @State private var pendingRegenerationMessageID: UUID?
    @State private var messageEditor: MessageEditor?
    @State private var composerHeight: CGFloat = 0
    @State private var isExportingConversation = false
    @State private var exportDocument = MarkdownExportDocument(text: "")

    private let bottomID = "conversation-bottom"

    var body: some View {
        Group {
            if let conversation = model.conversation(id: conversationID) {
                conversationView(conversation)
            } else {
                ContentUnavailableView("Chat unavailable", systemImage: "bubble.left.and.exclamationmark.bubble.right")
            }
        }
        .background(CagenticTheme.stage)
        .navigationTitle(model.conversation(id: conversationID)?.title ?? "Chat")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Menu {
                    modelMenuItems
                } label: {
                    ModelToolbarLabel(
                        modelName: model.activeModelName,
                        isRefreshing: isRefreshingModels
                    )
                }
                .disabled(model.settings.serverURL.isEmpty || model.isGenerating)
                .accessibilityLabel(
                    isRefreshingModels
                        ? "Refreshing models"
                        : "Model, \(model.activeModelName.isEmpty ? "Choose model" : model.activeModelName)"
                )
                .accessibilityHint(
                    model.isUsingGateway
                        ? "Shows the model the gateway is using"
                        : "Choose or refresh Ollama models"
                )
            }

            ToolbarItem(placement: .topBarTrailing) {
                Menu("Chat actions", systemImage: "ellipsis") {
                    Button("New chat", systemImage: "square.and.pencil") {
                        model.createConversation()
                    }
                    .disabled(model.isGenerating)
                    Button("Rename", systemImage: "pencil") {
                        model.presentedSheet = .renameConversation(conversationID)
                    }
                    Button(
                        conversationIsPinned ? "Unpin" : "Pin",
                        systemImage: "pin"
                    ) {
                        model.togglePinned(conversationID)
                    }
                    if let transcript = model.conversationExportText(id: conversationID) {
                        ShareLink(item: transcript) {
                            Label("Share", systemImage: "square.and.arrow.up")
                        }
                    }
                    Button("Export Markdown", systemImage: "doc.badge.arrow.up") {
                        prepareExport()
                    }
                    Button("Archive", systemImage: "archivebox") {
                        model.setArchived(true, conversationID: conversationID)
                    }
                    Divider()
                    Button("Manage chats", systemImage: "checklist") {
                        model.presentedSheet = .conversationManager
                    }
                    Button("Settings", systemImage: "gearshape") {
                        model.presentedSheet = .settings
                    }
                    Button("Delete chat", systemImage: "trash", role: .destructive) {
                        pendingDeletion = .chat
                    }
                }
            }
        }
        .confirmationDialog(
            pendingDeletion?.title ?? "Delete?",
            isPresented: isDeleteConfirmationPresented,
            titleVisibility: .visible
        ) {
            if let pendingDeletion {
                Button(pendingDeletion.buttonTitle, role: .destructive) {
                    performDeletion(pendingDeletion)
                }
            }
            Button("Cancel", role: .cancel) {
                pendingDeletion = nil
            }
        } message: {
            Text(pendingDeletion?.message ?? "")
        }
        .confirmationDialog(
            "Regenerate from here?",
            isPresented: isRegenerationConfirmationPresented,
            titleVisibility: .visible
        ) {
            if let pendingRegenerationMessageID {
                Button("Branch and regenerate") {
                    model.branchAndRetryResponse(
                        messageID: pendingRegenerationMessageID,
                        in: conversationID
                    )
                    self.pendingRegenerationMessageID = nil
                }
                Button("Replace in this chat", role: .destructive) {
                    model.retryResponse(
                        messageID: pendingRegenerationMessageID,
                        in: conversationID
                    )
                    self.pendingRegenerationMessageID = nil
                }
            }
            Button("Cancel", role: .cancel) {
                pendingRegenerationMessageID = nil
            }
        } message: {
            Text("Create a branch to keep this chat unchanged, or replace the later turns here. Replacements can be undone for a short time.")
        }
        .sheet(item: $messageEditor) { editor in
            EditMessageView(
                model: model,
                conversationID: conversationID,
                message: editor.message,
                replacesLaterTurns: editor.replacesLaterTurns
            )
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .fileExporter(
            isPresented: $isExportingConversation,
            document: exportDocument,
            contentType: .cagenticMarkdown,
            defaultFilename: exportFilename
        ) { result in
            if case .failure(let error) = result {
                model.notice = AppNotice(
                    title: "Couldn’t export chat",
                    message: error.localizedDescription
                )
            }
        }
    }

    private var conversationIsPinned: Bool {
        model.conversation(id: conversationID)?.isPinned == true
    }

    @ViewBuilder
    private var modelMenuItems: some View {
        ForEach(model.availableModels) { option in
            Button {
                model.selectModel(option.name)
            } label: {
                Label(
                    option.shortName,
                    systemImage: option.name == model.activeModelIdentifier
                        ? "checkmark"
                        : option.menuSymbol
                )
            }
            .disabled(!model.connectionState.isConnected || isRefreshingModels)
        }

        if !model.availableModels.isEmpty {
            Divider()
        }

        Button {
            Task {
                await model.refreshConnection()
            }
        } label: {
            Label(
                isRefreshingModels ? "Refreshing models…" : "Refresh models",
                systemImage: "arrow.clockwise"
            )
        }
        .disabled(isRefreshingModels)

        // The model library installs models with Ollama's pull API, which the gateway does not
        // expose — its models are configured on the computer.
        if !model.isUsingGateway {
            Button("Manage models…", systemImage: "square.stack.3d.up") {
                model.presentedSheet = .modelLibrary
            }
            .disabled(model.isGenerating)
        }
    }

    private var isRefreshingModels: Bool {
        if case .connecting = model.connectionState {
            return true
        }
        return false
    }

    private func conversationView(_ conversation: Conversation) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                if conversation.messages.isEmpty {
                    ChatEmptyState(model: model)
                        .containerRelativeFrame(.vertical, alignment: .center) { height, _ in
                            max(360, height * 0.82)
                        }
                } else {
                    LazyVStack(alignment: .leading, spacing: CagenticTheme.Spacing.xl) {
                        ForEach(conversation.messages) { message in
                            MessageRow(
                                message: message,
                                modelName: message.modelName ?? conversation.modelName,
                                onRegenerate: canRegenerate(message)
                                    ? { requestRegeneration(of: message, in: conversation) }
                                    : nil,
                                onEdit: canEdit(message)
                                    ? {
                                        messageEditor = MessageEditor(
                                            message: message,
                                            replacesLaterTurns: hasLaterUserTurn(
                                                after: message.id,
                                                in: conversation
                                            )
                                        )
                                    }
                                    : nil,
                                attachmentPayload: { attachment in
                                    try await model.attachmentPayload(for: attachment)
                                }
                            )
                            .transition(messageTransition)
                            .id(message.id)
                        }

                        Color.clear
                            .frame(height: composerClearance)
                            .id(bottomID)
                    }
                    .frame(maxWidth: 768)
                    .padding(.horizontal, CagenticTheme.Spacing.md)
                    .padding(.top, CagenticTheme.Spacing.lg)
                    .frame(maxWidth: .infinity)
                    .animation(
                        reduceMotion || model.isGenerating
                            ? nil
                            : .snappy(duration: 0.24, extraBounce: 0),
                        value: conversation.messages.count
                    )
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .cagenticHidesTopScrollEdge()
            .overlay(alignment: .top) {
                // The transcript passes behind the floating controls, so it has to fade out before
                // it reaches them — the same treatment the composer gets at the other end.
                LinearGradient(
                    stops: [
                        // Solid across the status bar, still heavy behind the controls, and fully
                        // clear a little below them — a fade, not a slab with a divider.
                        .init(color: CagenticTheme.stage, location: 0),
                        .init(color: CagenticTheme.stage, location: 0.42),
                        .init(color: CagenticTheme.stage.opacity(0.82), location: 0.68),
                        .init(color: CagenticTheme.stage.opacity(0), location: 1),
                    ],
                    startPoint: .top,
                    endPoint: .bottom
                )
                .frame(height: 152)
                .ignoresSafeArea(edges: .top)
                .allowsHitTesting(false)
            }
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.contentOffset.y + geometry.containerSize.height
                    >= geometry.contentSize.height - 120
            } action: { _, newValue in
                isNearBottom = newValue
                if newValue {
                    followsStream = true
                } else if isUserScrolling {
                    followsStream = false
                }
            }
            .onScrollPhaseChange { _, phase in
                isUserScrolling = phase == .tracking
                    || phase == .interacting
                    || phase == .decelerating
            }
            .onAppear {
                proxy.scrollTo(bottomID, anchor: .bottom)
            }
            .onChange(of: conversation.messages.count) { previousCount, newCount in
                guard newCount > previousCount else { return }
                // Sending starts a turn, and an animated scroll here would still be running when
                // the first deltas arrive and scroll again.
                followsStream = true
                scrollToBottom(using: proxy, animated: !model.isGenerating)
            }
            .onChange(of: model.streamRevision) {
                guard model.lastStreamConversationID == conversationID, followsStream else {
                    return
                }
                scrollToBottom(using: proxy, animated: false)
            }
            // Deliberately NOT part of the measured overlay below. Its height would feed
            // `composerClearance`, which sizes a spacer inside the scroll content — so showing it
            // would grow the content, move the scroll position, flip `isNearBottom`, and hide it
            // again. That loop is what made the transcript jump while a reply streamed.
            .overlay(alignment: .bottom) {
                if !isNearBottom, !conversation.messages.isEmpty {
                    Button {
                        followsStream = true
                        scrollToBottom(using: proxy, animated: true)
                    } label: {
                        Label("Latest", systemImage: "arrow.down")
                            .font(CagenticTheme.FontStyle.captionMedium)
                            .foregroundStyle(CagenticTheme.textPrimary)
                            .padding(.horizontal, CagenticTheme.Spacing.sm)
                            .frame(minHeight: 44)
                            .background(.ultraThinMaterial, in: .capsule)
                            .overlay {
                                Capsule().stroke(CagenticTheme.border, lineWidth: 0.75)
                            }
                            .shadow(color: .black.opacity(0.08), radius: 8, y: 3)
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Scrolls to the newest message")
                    .padding(.bottom, composerHeight + CagenticTheme.Spacing.xs)
                    .transition(.opacity)
                    .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: isNearBottom)
                }
            }
            .overlay(alignment: .bottom) {
                VStack(spacing: CagenticTheme.Spacing.xs) {
                    if let undo = model.lastConversationUndo,
                       undo.conversation.id == conversationID
                    {
                        ConversationUndoBanner(
                            title: undo.title,
                            onUndo: model.undoLastConversationRewrite,
                            onDismiss: model.clearConversationUndo
                        )
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                    }

                    if let permission = model.pendingPermission {
                        PermissionRequestCard(
                            request: permission,
                            onAnswer: model.answerPendingPermission
                        )
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                    }

                    ComposerView(
                        model: model,
                        showsConnectionRecovery: !conversation.messages.isEmpty
                    )
                }
                    .background {
                        // Content now scrolls all the way to the device edge, so it needs to fade
                        // out beneath the composer rather than collide with it.
                        LinearGradient(
                            colors: [
                                CagenticTheme.stage.opacity(0),
                                CagenticTheme.stage.opacity(0.92),
                                CagenticTheme.stage,
                            ],
                            startPoint: .top,
                            endPoint: .bottom
                        )
                        .ignoresSafeArea(edges: .bottom)
                        .allowsHitTesting(false)
                    }
                    .background {
                        GeometryReader { geometry in
                            Color.clear.preference(
                                key: ComposerHeightPreferenceKey.self,
                                value: geometry.size.height
                            )
                        }
                    }
                    .animation(
                        reduceMotion ? nil : .easeOut(duration: 0.2),
                        value: model.pendingPermission
                    )
            }
            .onPreferenceChange(ComposerHeightPreferenceKey.self) { height in
                guard abs(composerHeight - height) > 0.5 else { return }
                composerHeight = height
            }
            .onChange(of: composerHeight) { previousHeight, newHeight in
                guard newHeight > previousHeight, isNearBottom else { return }
                Task { @MainActor in
                    scrollToBottom(using: proxy, animated: false)
                }
            }
        }
    }

    private var composerClearance: CGFloat {
        max(composerHeight + CagenticTheme.Spacing.xs, 1)
    }

    private var isDeleteConfirmationPresented: Binding<Bool> {
        Binding(
            get: { pendingDeletion != nil },
            set: { if !$0 { pendingDeletion = nil } }
        )
    }

    private var isRegenerationConfirmationPresented: Binding<Bool> {
        Binding(
            get: { pendingRegenerationMessageID != nil },
            set: { if !$0 { pendingRegenerationMessageID = nil } }
        )
    }

    private func requestRegeneration(of message: ChatMessage, in conversation: Conversation) {
        if hasLaterUserTurn(after: message.id, in: conversation) {
            pendingRegenerationMessageID = message.id
        } else {
            model.retryResponse(messageID: message.id, in: conversationID)
        }
    }

    private func hasLaterUserTurn(after messageID: UUID, in conversation: Conversation) -> Bool {
        guard let index = conversation.messages.firstIndex(where: { $0.id == messageID }) else {
            return false
        }
        return conversation.messages.dropFirst(index + 1).contains { $0.role == .user }
    }

    private func scrollToBottom(using proxy: ScrollViewProxy, animated: Bool) {
        if reduceMotion || !animated {
            proxy.scrollTo(bottomID, anchor: .bottom)
        } else {
            withAnimation(.easeOut(duration: 0.24)) {
                proxy.scrollTo(bottomID, anchor: .bottom)
            }
        }
    }

    private func prepareExport() {
        guard let transcript = model.conversationExportText(id: conversationID) else { return }
        exportDocument = MarkdownExportDocument(text: transcript)
        isExportingConversation = true
    }

    private var exportFilename: String {
        let title = model.conversation(id: conversationID)?.title ?? "Cagentic Chat"
        let cleaned = title
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty }
            .joined(separator: "-")
        return cleaned.isEmpty ? "Cagentic-Chat" : cleaned
    }

    private func performDeletion(_ deletion: ChatDeletion) {
        switch deletion {
        case .chat:
            model.deleteConversation(id: conversationID)
        }
        pendingDeletion = nil
    }

    private func canRegenerate(_ message: ChatMessage) -> Bool {
        message.role == .assistant
            && model.conversationsAreLocallyOwned
            && model.connectionState.isConnected
            && !model.isGenerating
            && !model.activeModelName.isEmpty
    }

    private func canEdit(_ message: ChatMessage) -> Bool {
        message.role == .user
            && model.conversationsAreLocallyOwned
            && model.connectionState.isConnected
            && !model.isGenerating
            && !model.activeModelName.isEmpty
    }

    private var messageTransition: AnyTransition {
        // While a reply is streaming the pair of messages is appended immediately and then grows
        // continuously; animating their insertion fights the scroll that follows the growth.
        guard !reduceMotion, !model.isGenerating else { return .identity }
        return .asymmetric(
            insertion: .move(edge: .bottom).combined(with: .opacity),
            removal: .opacity
        )
    }
}

private struct ComposerHeightPreferenceKey: PreferenceKey {
    static var defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

private enum ChatDeletion {
    case chat

    var title: String {
        "Delete this chat?"
    }

    var buttonTitle: String {
        "Delete chat"
    }

    var message: String {
        "This permanently removes the conversation from this device."
    }
}

private struct MessageEditor: Identifiable {
    let message: ChatMessage
    let replacesLaterTurns: Bool

    var id: UUID { message.id }
}

private struct EditMessageView: View {
    @Bindable var model: AppModel
    let conversationID: UUID
    let message: ChatMessage
    let replacesLaterTurns: Bool

    @Environment(\.dismiss) private var dismiss
    @FocusState private var isEditorFocused: Bool
    @State private var text: String
    @State private var attachments: [AttachmentMetadata]
    @State private var isRewriteConfirmationPresented = false

    init(
        model: AppModel,
        conversationID: UUID,
        message: ChatMessage,
        replacesLaterTurns: Bool
    ) {
        self.model = model
        self.conversationID = conversationID
        self.message = message
        self.replacesLaterTurns = replacesLaterTurns
        _text = State(initialValue: message.content)
        _attachments = State(initialValue: message.attachments)
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.md) {
                TextEditor(text: $text)
                    .font(CagenticTheme.FontStyle.body)
                    .foregroundStyle(CagenticTheme.textPrimary)
                    .scrollContentBackground(.hidden)
                    .padding(CagenticTheme.Spacing.xs)
                    .focused($isEditorFocused)
                    .background(CagenticTheme.surface)
                    .overlay {
                        RoundedRectangle(cornerRadius: CagenticTheme.Radius.card)
                            .stroke(CagenticTheme.border, lineWidth: 0.75)
                    }
                    .clipShape(.rect(cornerRadius: CagenticTheme.Radius.card))
                    .accessibilityLabel("Message text")

                if !attachments.isEmpty {
                    VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                        Text("Attachments")
                            .font(CagenticTheme.FontStyle.captionMedium)
                            .foregroundStyle(CagenticTheme.textSecondary)

                        ScrollView(.horizontal) {
                            HStack(spacing: CagenticTheme.Spacing.xs) {
                                ForEach(attachments) { attachment in
                                    AttachmentChip(
                                        attachment: attachment,
                                        onRemove: {
                                            attachments.removeAll { $0.id == attachment.id }
                                        },
                                        loadPayload: {
                                            try await model.attachmentPayload(for: attachment)
                                        }
                                    )
                                }
                            }
                        }
                        .scrollIndicators(.hidden)
                    }
                }

                if replacesLaterTurns {
                    Label(
                        "This edit will remove the later messages in this chat.",
                        systemImage: "exclamationmark.triangle"
                    )
                    .font(CagenticTheme.FontStyle.footnote)
                    .foregroundStyle(CagenticTheme.warning)
                    .accessibilityElement(children: .combine)
                } else {
                    Text("Sending replaces this turn and regenerates the response after it.")
                        .font(CagenticTheme.FontStyle.footnote)
                        .foregroundStyle(CagenticTheme.textSecondary)
                }
            }
            .padding(CagenticTheme.Spacing.md)
            .background(CagenticTheme.background)
            .navigationTitle("Edit message")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        dismiss()
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send", systemImage: "arrow.up") {
                        if replacesLaterTurns {
                            isRewriteConfirmationPresented = true
                        } else {
                            submitEdit()
                        }
                    }
                    .disabled(!canSubmit)
                }
            }
            .task {
                isEditorFocused = true
            }
            .confirmationDialog(
                "Edit from here?",
                isPresented: $isRewriteConfirmationPresented,
                titleVisibility: .visible
            ) {
                Button("Branch and send") {
                    submitEdit(asBranch: true)
                }
                Button("Replace in this chat", role: .destructive) {
                    submitEdit(asBranch: false)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("A branch preserves the original chat. Replacing removes the later turns here, with Undo available briefly afterward.")
            }
        }
    }

    private func submitEdit(asBranch: Bool = false) {
        if asBranch {
            model.branchFromUserMessage(
                messageID: message.id,
                in: conversationID,
                content: text,
                attachments: attachments
            )
        } else {
            model.editUserMessage(
                messageID: message.id,
                in: conversationID,
                content: text,
                attachments: attachments
            )
        }
        dismiss()
    }

    private var canSubmit: Bool {
        (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !attachments.isEmpty)
            && model.connectionState.isConnected
            && !model.isGenerating
            && !model.activeModelName.isEmpty
    }
}

private struct ModelToolbarLabel: View {
    let modelName: String
    let isRefreshing: Bool

    var body: some View {
        HStack(spacing: CagenticTheme.Spacing.xxs) {
            if isRefreshing {
                ProgressView()
                    .controlSize(.mini)
                    .accessibilityHidden(true)
                Text("Refreshing…")
                    .font(CagenticTheme.FontStyle.subheadlineSemibold)
                    .lineLimit(1)
            } else {
                Text(modelName.isEmpty ? "Choose model" : modelName.replacingOccurrences(of: ":latest", with: ""))
                    .font(CagenticTheme.FontStyle.subheadlineSemibold)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.bold))
                    .opacity(0.7)
            }
        }
        .foregroundStyle(CagenticTheme.textPrimary)
        .padding(.horizontal, CagenticTheme.Spacing.sm)
        .frame(minHeight: 40)
        // Its own glass capsule, so it reads as a control floating over the transcript rather
        // than a title stranded in a bar.
        .cagenticGlass()
        .frame(maxWidth: 220)
    }
}

private struct ChatEmptyState: View {
    @Bindable var model: AppModel

    var body: some View {
        VStack(spacing: CagenticTheme.Spacing.md) {
            BrandMark(size: 38)
            .accessibilityHidden(true)

            VStack(spacing: CagenticTheme.Spacing.xs) {
                Text("What should we work on?")
                    .font(CagenticTheme.FontStyle.title)
                    .foregroundStyle(CagenticTheme.textPrimary)
                    .multilineTextAlignment(.center)

                Text(statusMessage)
                    .font(CagenticTheme.FontStyle.subheadline)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
            }

            if isConnecting {
                ProgressView()
                    .controlSize(.regular)
                    .tint(CagenticTheme.accent)
                    .frame(minHeight: 44)
                .accessibilityLabel("Refreshing models")
            } else if model.isGenerating {
                Label("Another chat is responding", systemImage: "waveform")
                    .font(CagenticTheme.FontStyle.captionMedium)
                    .foregroundStyle(CagenticTheme.textTertiary)
            } else if model.connectionState.isConnected, model.availableModels.isEmpty {
                Button("Refresh models", systemImage: "arrow.clockwise") {
                    Task {
                        await model.refreshConnection()
                    }
                }
                .buttonStyle(.bordered)
            } else if !model.connectionState.isConnected {
                Button(
                    model.isUsingGateway ? "Connect to Cagentic" : "Connect to Ollama",
                    systemImage: "desktopcomputer"
                ) {
                    model.presentedSheet = .serverManager
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .frame(maxWidth: 420)
        .padding(CagenticTheme.Spacing.lg)
        .frame(maxWidth: .infinity, alignment: .center)
        .accessibilityElement(children: .contain)
    }

    private var statusMessage: String {
        if isConnecting {
            return model.isUsingGateway
                ? "Checking which models the gateway has available."
                : "Checking which models are available from your Ollama server."
        }
        if !model.connectionState.isConnected {
            return model.isUsingGateway
                ? "Connect to the Cagentic gateway on your computer to begin."
                : "Connect to Ollama on your computer to begin."
        }
        if model.availableModels.isEmpty {
            return "Install a model on the computer, then refresh here."
        }
        return "\(model.activeModelName) · \(model.settings.serverName)"
    }

    private var isConnecting: Bool {
        if case .connecting = model.connectionState {
            return true
        }
        return false
    }
}


/// The approval a gateway turn is waiting on.
///
/// Presented inline above the composer rather than as a sheet: the gateway parks its turn thread
/// here for up to five minutes, so the request has to stay visible with the transcript that
/// explains it, and it must not collide with the settings sheets the app can already be showing.
private struct PermissionRequestCard: View {
    let request: GatewayPermissionRequest
    let onAnswer: (GatewayPermissionAnswer) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
                Label("Approval needed", systemImage: "hand.raised")
                    .font(CagenticTheme.FontStyle.captionSemibold)
                    .foregroundStyle(CagenticTheme.warning)

                Text(request.tool)
                    .font(CagenticTheme.FontStyle.bodySemibold)
                    .foregroundStyle(CagenticTheme.textPrimary)

                if !request.summary.isEmpty {
                    Text(request.summary)
                        .font(CagenticTheme.FontStyle.caption)
                        .foregroundStyle(CagenticTheme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }

                // Which confinement a shell command will actually run under changes what approving
                // it means, so it is never hidden behind a disclosure.
                if let sandbox = request.sandbox {
                    Text(sandbox)
                        .font(CagenticTheme.FontStyle.caption2)
                        .foregroundStyle(
                            request.allowsNetwork
                                ? CagenticTheme.warning
                                : CagenticTheme.textTertiary
                        )
                }
            }

            if let diff = request.diff {
                ScrollView(.vertical) {
                    Text(diff)
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(CagenticTheme.textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 132)
                .padding(CagenticTheme.Spacing.xs)
                .background(CagenticTheme.surfaceRaised)
                .clipShape(.rect(cornerRadius: CagenticTheme.Radius.control))
            }

            VStack(spacing: CagenticTheme.Spacing.xs) {
                HStack(spacing: CagenticTheme.Spacing.xs) {
                    Button("Deny") { onAnswer(.denyOnce) }
                        .buttonStyle(.bordered)
                        .frame(maxWidth: .infinity)

                    Button("Allow") { onAnswer(.allowOnce) }
                        .buttonStyle(.borderedProminent)
                        .frame(maxWidth: .infinity)
                }
                .controlSize(.large)

                // The narrow standing approval the gateway offered for this exact call, when it
                // offered one. Preferred over "always allow this tool", which is much broader than
                // it sounds — it applies process-wide, including to the terminal.
                if let rule = request.rule {
                    Button {
                        onAnswer(.allowRule)
                    } label: {
                        Text("Always allow \(rule)")
                            .font(CagenticTheme.FontStyle.caption)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(CagenticTheme.accent)
                }
            }
        }
        .padding(CagenticTheme.Spacing.md)
        .frame(maxWidth: 520)
        .background(CagenticTheme.surface)
        .overlay {
            RoundedRectangle(cornerRadius: CagenticTheme.Radius.card)
                .stroke(CagenticTheme.warning.opacity(0.5), lineWidth: 1)
        }
        .clipShape(.rect(cornerRadius: CagenticTheme.Radius.card))
        .shadow(color: .black.opacity(0.12), radius: 12, y: 4)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Approval needed for \(request.tool)")
    }
}

private struct ConversationUndoBanner: View {
    let title: String
    let onUndo: () -> Void
    let onDismiss: () -> Void

    var body: some View {
        HStack(spacing: CagenticTheme.Spacing.sm) {
            Image(systemName: "arrow.uturn.backward.circle.fill")
                .foregroundStyle(CagenticTheme.accent)
                .accessibilityHidden(true)

            Text(title)
                .font(CagenticTheme.FontStyle.captionMedium)
                .foregroundStyle(CagenticTheme.textPrimary)
                .lineLimit(1)

            Spacer(minLength: 0)

            Button("Undo", action: onUndo)
                .font(CagenticTheme.FontStyle.captionMedium)

            Button("Dismiss", systemImage: "xmark", action: onDismiss)
                .labelStyle(.iconOnly)
                .foregroundStyle(CagenticTheme.textSecondary)
                .frame(width: 44, height: 44)
        }
        .padding(.leading, CagenticTheme.Spacing.sm)
        .padding(.trailing, CagenticTheme.Spacing.xxs)
        .frame(maxWidth: 520, minHeight: 48)
        .background(.ultraThinMaterial, in: .capsule)
        .overlay {
            Capsule().stroke(CagenticTheme.border, lineWidth: 0.75)
        }
        .shadow(color: .black.opacity(0.08), radius: 8, y: 3)
        .padding(.horizontal, CagenticTheme.Spacing.md)
        .accessibilityElement(children: .contain)
    }
}

private struct MarkdownExportDocument: FileDocument {
    static var readableContentTypes: [UTType] { [.cagenticMarkdown] }

    var text: String

    init(text: String) {
        self.text = text
    }

    init(configuration: ReadConfiguration) throws {
        guard let data = configuration.file.regularFileContents,
              let text = String(data: data, encoding: .utf8)
        else {
            throw CocoaError(.fileReadCorruptFile)
        }
        self.text = text
    }

    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: Data(text.utf8))
    }
}

private extension UTType {
    nonisolated static let cagenticMarkdown = UTType(filenameExtension: "md") ?? .plainText
}

private extension AvailableModel {
    var menuSymbol: String {
        if capabilities.contains("vision") {
            return "eye"
        }
        if capabilities.contains("thinking") {
            return "sparkles"
        }
        return "cpu"
    }
}

#Preview("Chat · active") {
    NavigationStack {
        let model = AppModel.preview()
        ChatView(model: model, conversationID: model.selectedConversationID!)
    }
}
