import SwiftUI

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
    /// Whether the Latest button is showing. Deliberately *not* `!isNearBottom`: that flips 120pt
    /// off the bottom, which is a nudge rather than a decision to go read something, and a button
    /// appearing every time the transcript settles a hair short of the end is noise.
    @State private var showsJumpButton = false
    /// A finger is on the transcript right now. Distinct from `isUserScrolling`, which stays true
    /// through momentum — the difference decides whether a programmatic scroll is helping or
    /// fighting the reader.
    @State private var isTouchingScroll = false
    @State private var jumpTask: Task<Void, Never>?
    @State private var isUserScrolling = false
    @State private var pendingDeletion: ChatDeletion?
    @State private var pendingRegenerationMessageID: UUID?
    @State private var messageEditor: MessageEditor?
    @State private var composerHeight: CGFloat = 0

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
                // Only what acts on *this* chat. New chat, settings and the rest live in the
                // drawer, and a menu that repeats them just makes the three that matter harder
                // to find.
                Menu("Chat actions", systemImage: "ellipsis") {
                    Button("Rename", systemImage: "pencil") {
                        model.presentedSheet = .renameConversation(conversationID)
                    }
                    Button(
                        conversationIsPinned ? "Unpin" : "Pin",
                        systemImage: "pin"
                    ) {
                        model.togglePinned(conversationID)
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
                if canBranch() {
                    Button("Branch and regenerate") {
                        model.branchAndRetryResponse(
                            messageID: pendingRegenerationMessageID,
                            in: conversationID
                        )
                        self.pendingRegenerationMessageID = nil
                    }
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
            Text(
                canBranch()
                    ? "Create a branch to keep this chat unchanged, or replace the later turns here. Replacements can be undone for a short time."
                    : "This removes the later turns in this chat, on the computer as well. It can be undone for a short time."
            )
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

        // Modes decide what a turn may do without asking, and only the gateway runs tools — on a
        // direct Ollama connection there is nothing to approve, so offering the choice there
        // would be a control that does nothing.
        if model.isUsingGateway {
            Menu {
                modeMenuItems
            } label: {
                Label(
                    "Mode: \(model.activeChatMode.title)",
                    systemImage: model.activeChatMode.symbol
                )
            }
            .disabled(model.isGenerating)
        }

        if !model.chatProfiles.isEmpty {
            Menu {
                profileMenuItems
            } label: {
                Label("Profile: \(profileMenuTitle)", systemImage: "person.crop.rectangle")
            }
            .disabled(model.isGenerating)
        }

        Divider()

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

    private var profileMenuTitle: String {
        model.activeChatProfile?.displayName ?? "None"
    }

    @ViewBuilder
    private var modeMenuItems: some View {
        ForEach(GatewayChatMode.allCases) { mode in
            Button {
                model.setChatMode(mode)
            } label: {
                // The same faked checkmark the model rows use, so the two lists read alike.
                Label(
                    mode.title,
                    systemImage: mode == model.activeChatMode ? "checkmark" : mode.symbol
                )
            }
        }
    }

    @ViewBuilder
    private var profileMenuItems: some View {
        ForEach(model.chatProfiles) { profile in
            Button {
                model.applyChatProfile(profile)
            } label: {
                Label(
                    profile.displayName,
                    systemImage: profile.id == model.activeChatProfile?.id
                        ? "checkmark"
                        : "person.crop.rectangle"
                )
            }
        }
        Divider()
        Button("Edit profiles…", systemImage: "slider.horizontal.3") {
            model.presentedSheet = .settings
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
            .onScrollGeometryChange(for: ScrollBottomDistance.self) { geometry in
                ScrollBottomDistance(
                    fromBottom: geometry.contentSize.height
                        - (geometry.contentOffset.y + geometry.containerSize.height),
                    viewport: geometry.containerSize.height
                )
            } action: { _, distance in
                showsJumpButton = distance.showsJumpButton(whenAlreadyShowing: showsJumpButton)
            }
            .onScrollPhaseChange { _, phase in
                isUserScrolling = phase == .tracking
                    || phase == .interacting
                    || phase == .decelerating
                isTouchingScroll = phase == .tracking || phase == .interacting
                if isTouchingScroll {
                    // The reader has taken the wheel back; stop chasing the bottom.
                    jumpTask?.cancel()
                    jumpTask = nil
                }
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
                // The animation has to live on a container that outlives the button; attached
                // inside the `if` it belonged to a view that did not exist yet, so the transition
                // never ran and the button simply blinked into place.
                ZStack {
                    if showsJumpButton, !conversation.messages.isEmpty {
                    Button {
                        jumpToLatest(using: proxy)
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
                    .transition(
                        .asymmetric(
                            // Rises and settles from where the composer is, then leaves quickly
                            // downward — it is going back to the thing it points at.
                            insertion: .scale(scale: 0.82, anchor: .bottom)
                                .combined(with: .opacity)
                                .combined(with: .offset(y: 10)),
                            removal: .scale(scale: 0.92, anchor: .bottom)
                                .combined(with: .opacity)
                        )
                    )
                    }
                }
                .animation(
                    reduceMotion ? nil : .spring(response: 0.34, dampingFraction: 0.74),
                    value: showsJumpButton
                )
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

                    if let question = model.pendingQuestion {
                        QuestionCard(
                            question: question,
                            onAnswer: model.answerPendingQuestion
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

    /// Sends the transcript to the newest message, even from a flick that is still coasting.
    ///
    /// A single `scrollTo` is not enough: tapping the button mid-scroll leaves the scroll view with
    /// momentum, and that momentum overrides the programmatic offset a frame later — so the button
    /// looked dead exactly when it was most useful. Re-issuing the scroll until the viewport
    /// actually arrives is what makes the tap stick.
    private func jumpToLatest(using proxy: ScrollViewProxy) {
        followsStream = true
        scrollToBottom(using: proxy, animated: true)
        jumpTask?.cancel()
        jumpTask = Task { @MainActor in
            // Bounded: eight tries over about half a second, and it stops the moment it lands or
            // the reader touches the transcript again.
            for _ in 0..<8 {
                try? await Task.sleep(for: .milliseconds(60))
                if Task.isCancelled || isTouchingScroll || isNearBottom { return }
                scrollToBottom(using: proxy, animated: false)
            }
        }
    }

    private func performDeletion(_ deletion: ChatDeletion) {
        switch deletion {
        case .chat:
            model.deleteConversation(id: conversationID)
        }
        pendingDeletion = nil
    }

    /// Both of these work on either backend. They used to require locally-owned chats, which
    /// greyed them out for every gateway conversation — but the gateway has always been able to
    /// rewrite a turn and re-run it, which is what its own web page has used all along.
    private func canRegenerate(_ message: ChatMessage) -> Bool {
        message.role == .assistant
            && model.connectionState.isConnected
            && !model.isGenerating
            && !model.activeModelName.isEmpty
    }

    private func canEdit(_ message: ChatMessage) -> Bool {
        message.role == .user
            && model.connectionState.isConnected
            && !model.isGenerating
            && !model.activeModelName.isEmpty
    }

    /// Branching forks a chat into a second one. The gateway owns its history and has no notion
    /// of a fork, so this stays local — offering it there would promise something no route can do.
    private func canBranch() -> Bool {
        model.conversationsAreLocallyOwned
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
                if model.conversationsAreLocallyOwned {
                    Button("Branch and send") {
                        submitEdit(asBranch: true)
                    }
                }
                Button("Replace in this chat", role: .destructive) {
                    submitEdit(asBranch: false)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                // Branching forks the chat, which only a locally-owned one can do — the gateway
                // owns its history and has no fork. Saying so beats offering a button that
                // cannot work.
                Text(
                    model.conversationsAreLocallyOwned
                        ? "A branch preserves the original chat. Replacing removes the later turns here, with Undo available briefly afterward."
                        : "This removes the later turns in this chat, on the computer as well. Undo is available briefly afterward."
                )
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
/// The model asking something, with the turn parked on the answer.
///
/// Options are shortcuts, never the whole answer — free text is always offered alongside them,
/// because a list the model wrote may not contain the real answer and being unable to say so
/// would leave the turn parked until it times out.
private struct QuestionCard: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var typed = ""
    @FocusState private var isFieldFocused: Bool

    let question: GatewayQuestion
    let onAnswer: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
            Label("Question", systemImage: "questionmark.bubble")
                .font(CagenticTheme.FontStyle.captionSemibold)
                .foregroundStyle(CagenticTheme.accent)

            Text(question.question)
                .font(CagenticTheme.FontStyle.Message.body)
                .foregroundStyle(CagenticTheme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(question.options, id: \.self) { option in
                Button {
                    onAnswer(option)
                } label: {
                    Text(option)
                        .font(CagenticTheme.FontStyle.calloutMedium)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .frame(minHeight: 44)
                        .padding(.horizontal, CagenticTheme.Spacing.sm)
                        .background(CagenticTheme.surfaceRaised, in: .rect(cornerRadius: CagenticTheme.Radius.control))
                        .overlay {
                            RoundedRectangle(cornerRadius: CagenticTheme.Radius.control)
                                .stroke(CagenticTheme.border, lineWidth: 0.75)
                        }
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
            }

            HStack(spacing: CagenticTheme.Spacing.xs) {
                TextField(
                    question.options.isEmpty ? "Your answer" : "Or answer in your own words",
                    text: $typed,
                    axis: .vertical
                )
                .lineLimit(1...4)
                .textFieldStyle(.plain)
                .focused($isFieldFocused)
                .submitLabel(.send)
                .onSubmit(submit)
                .padding(.horizontal, CagenticTheme.Spacing.sm)
                .frame(minHeight: 44)
                .background(CagenticTheme.surface, in: .rect(cornerRadius: CagenticTheme.Radius.control))
                .overlay {
                    RoundedRectangle(cornerRadius: CagenticTheme.Radius.control)
                        .stroke(CagenticTheme.border, lineWidth: 0.75)
                }

                Button("Answer", action: submit)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(CagenticTheme.Spacing.sm)
        .background(CagenticTheme.surface, in: .rect(cornerRadius: CagenticTheme.Radius.card))
        .overlay {
            RoundedRectangle(cornerRadius: CagenticTheme.Radius.card)
                .stroke(CagenticTheme.accent.opacity(0.5), lineWidth: 0.75)
        }
        .padding(.horizontal, CagenticTheme.Spacing.sm)
        .onAppear { isFieldFocused = question.options.isEmpty }
    }

    private func submit() {
        let trimmed = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        typed = ""
        onAnswer(trimmed)
    }
}

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
                        .font(CagenticTheme.FontStyle.Code.detail)
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

/// How far the transcript sits from its own end, with the viewport it is measured against.
///
/// Carrying both means the Latest button's threshold can scale with the device instead of being a
/// magic number tuned on one phone, and the hysteresis can be applied where the previous state is
/// known rather than inside the geometry closure.
struct ScrollBottomDistance: Equatable {
    let fromBottom: CGFloat
    let viewport: CGFloat

    /// Whether the Latest button belongs on screen, given whether it is already there.
    ///
    /// Two thresholds rather than one. It takes half a screen of deliberate scrolling to summon
    /// the button — drifting 120pt off the bottom is a nudge, not a decision to go read something
    /// — and it survives until the transcript is most of the way back, so a finger resting near
    /// the boundary cannot flicker it on and off.
    func showsJumpButton(whenAlreadyShowing showing: Bool) -> Bool {
        let appearsAt = max(320, viewport * 0.5)
        if showing { return fromBottom >= appearsAt * 0.4 }
        return fromBottom > appearsAt
    }
}
