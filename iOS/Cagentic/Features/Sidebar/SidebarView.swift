import SwiftUI

struct SidebarView: View {
    @Bindable var model: AppModel
    @Binding var isPresented: Bool
    @State private var pendingDeletion: UUID?

    var body: some View {
        List {
            chatsSection
        }
        // Plain, not sidebar: the drawer supplies its own selection treatment, and the sidebar
        // style's inset cards fought it for the same visual weight.
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(CagenticTheme.background)
        .navigationTitle("Chats")
        .safeAreaInset(edge: .bottom, spacing: 0) {
            SidebarFooter(model: model, onNewChat: createConversation)
        }
        .onChange(of: model.presentedSheet) { _, destination in
            if destination != nil {
                isPresented = false
            }
        }
        .confirmationDialog(
            "Delete this chat?",
            isPresented: isDeleteConfirmationPresented,
            titleVisibility: .visible
        ) {
            Button("Delete chat", role: .destructive) {
                guard let pendingDeletion else { return }
                model.deleteConversation(id: pendingDeletion)
                self.pendingDeletion = nil
            }
            Button("Cancel", role: .cancel) {
                pendingDeletion = nil
            }
        } message: {
            Text("This permanently removes the conversation from this device.")
        }
    }

    private var isDeleteConfirmationPresented: Binding<Bool> {
        Binding(
            get: { pendingDeletion != nil },
            set: { if !$0 { pendingDeletion = nil } }
        )
    }

    @ViewBuilder
    private var chatsSection: some View {
        let visibleConversations = model.visibleConversationSummaries.filter { !$0.isArchived }
        let pinned = visibleConversations.filter(\.isPinned)
        let recent = visibleConversations.filter { !$0.isPinned }

        if visibleConversations.isEmpty {
            Section {
                Text(model.visibleConversationSummaries.isEmpty
                    ? "Your conversations will appear here."
                    : "Your active chats are clear. Open Manage Chats to restore an archive.")
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .listRowBackground(Color.clear)
                    .padding(.vertical, CagenticTheme.Spacing.xs)
            }
        } else {
            if !pinned.isEmpty {
                conversationSection("Pinned", conversations: pinned)
            }
            conversationSection("Recent", conversations: recent)
        }
    }

    private func conversationSection(
        _ title: String,
        conversations: [ConversationSummary]
    ) -> some View {
        Section {
            ForEach(conversations) { conversation in
                conversationButton(conversation)
            }
        } header: {
            Text(title)
                .textCase(nil)
                .font(CagenticTheme.FontStyle.captionSemibold)
                .foregroundStyle(CagenticTheme.textTertiary)
        }
    }

    private func conversationButton(_ conversation: ConversationSummary) -> some View {
        Button {
            model.selectConversation(conversation.id)
            isPresented = false
        } label: {
            ConversationRow(
                conversation: conversation,
                hasDraft: model.hasDraft(for: conversation.id),
                isSelected: model.selectedConversationID == conversation.id
            )
            .padding(.horizontal, CagenticTheme.Spacing.sm)
            .background(
                model.selectedConversationID == conversation.id
                    ? CagenticTheme.accentSoft
                    : .clear,
                in: .rect(cornerRadius: CagenticTheme.Radius.control)
            )
        }
        .buttonStyle(.plain)
        .listRowInsets(
            EdgeInsets(
                top: 1,
                leading: CagenticTheme.Spacing.xs,
                bottom: 1,
                trailing: CagenticTheme.Spacing.xs
            )
        )
        .listRowBackground(Color.clear)
        .listRowSeparator(.hidden)
        .accessibilityAddTraits(
            model.selectedConversationID == conversation.id ? .isSelected : []
        )
        .contextMenu {
            Button(conversation.isPinned ? "Unpin" : "Pin", systemImage: "pin") {
                model.togglePinned(conversation.id)
            }
            Button("Rename", systemImage: "pencil") {
                model.presentedSheet = .renameConversation(conversation.id)
            }
            if let transcript = model.conversationExportText(id: conversation.id) {
                ShareLink(item: transcript) {
                    Label("Share", systemImage: "square.and.arrow.up")
                }
            }
            Button("Archive", systemImage: "archivebox") {
                model.setArchived(true, conversationID: conversation.id)
            }
            Divider()
            Button("Delete", systemImage: "trash", role: .destructive) {
                pendingDeletion = conversation.id
            }
        }
        .swipeActions(edge: .leading, allowsFullSwipe: true) {
            Button("Archive", systemImage: "archivebox") {
                model.setArchived(true, conversationID: conversation.id)
            }
            .tint(CagenticTheme.accent)
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button("Delete", systemImage: "trash", role: .destructive) {
                pendingDeletion = conversation.id
            }
        }
    }

    private func createConversation() {
        model.createConversation()
        isPresented = false
    }
}

/// One conversation in the drawer.
///
/// Deliberately a single line. The row used to carry a truncated echo of the last message under the
/// title, which is the densest, least useful thing a chat list can show: every row looks the same
/// after a few turns, and the title — the one thing that identifies the chat — competes with it.
/// Draft and branch state are the only extras that change what tapping the row will do, so they are
/// the only extras kept, and both are marks rather than words.
private struct ConversationRow: View {
    let conversation: ConversationSummary
    let hasDraft: Bool
    let isSelected: Bool

    var body: some View {
        HStack(spacing: CagenticTheme.Spacing.xs) {
            if conversation.isBranch {
                Image(systemName: "arrow.triangle.branch")
                    .font(.caption)
                    .foregroundStyle(CagenticTheme.textTertiary)
                    .accessibilityHidden(true)
            }

            Text(conversation.title)
                .font(
                    isSelected
                        ? CagenticTheme.FontStyle.bodySemibold
                        : CagenticTheme.FontStyle.body
                )
                .foregroundStyle(
                    isSelected ? CagenticTheme.accent : CagenticTheme.textPrimary
                )
                .lineLimit(1)
                .truncationMode(.tail)

            Spacer(minLength: CagenticTheme.Spacing.xxs)

            if hasDraft {
                Circle()
                    .fill(CagenticTheme.accent)
                    .frame(width: 6, height: 6)
                    .accessibilityHidden(true)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
        .contentShape(.rect)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityHint("Opens this conversation")
    }

    private var accessibilityLabel: String {
        // The glyph and the dot are decorative, so their meaning is spoken here instead.
        [
            conversation.title,
            conversation.isBranch ? "Branch" : nil,
            hasDraft ? "Unsent draft" : nil,
        ]
        .compactMap { $0 }
        .joined(separator: ", ")
    }
}

private struct SidebarFooter: View {
    @Bindable var model: AppModel
    let onNewChat: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Divider()
                .overlay(CagenticTheme.border)

            Button {
                model.presentedSheet = .serverManager
            } label: {
                HStack(spacing: CagenticTheme.Spacing.sm) {
                    Image(systemName: statusSymbol)
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(statusColor)
                        .frame(width: 24)

                    VStack(alignment: .leading, spacing: 2) {
                        Text(model.connectionState.title)
                            .font(CagenticTheme.FontStyle.calloutMedium)
                            .foregroundStyle(CagenticTheme.textPrimary)
                        Text(model.serverSubtitle)
                            .font(CagenticTheme.FontStyle.caption)
                            .foregroundStyle(CagenticTheme.textSecondary)
                            .lineLimit(1)
                    }

                    Spacer(minLength: CagenticTheme.Spacing.xs)

                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(CagenticTheme.textTertiary)
                }
                .frame(minHeight: 44)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)

            HStack(spacing: CagenticTheme.Spacing.sm) {
                Button(action: onNewChat) {
                    HStack(spacing: CagenticTheme.Spacing.xs) {
                        Image(systemName: "square.and.pencil")
                            .foregroundStyle(CagenticTheme.accent)
                        Text("New chat")
                            .foregroundStyle(CagenticTheme.textPrimary)
                    }
                    .font(CagenticTheme.FontStyle.calloutMedium)
                    .frame(maxWidth: .infinity, minHeight: 48)
                    .background(CagenticTheme.surfaceRaised, in: .capsule)
                    .overlay {
                        Capsule()
                            .stroke(CagenticTheme.border, lineWidth: 0.75)
                    }
                    .contentShape(.capsule)
                }
                .buttonStyle(.plain)
                .disabled(!canCreateChat)
                .opacity(canCreateChat ? 1 : 0.45)

                Button {
                    model.presentedSheet = .settings
                } label: {
                    Image(systemName: "gearshape")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(CagenticTheme.textPrimary)
                        .frame(width: 48, height: 48)
                        .background(CagenticTheme.surfaceRaised, in: .circle)
                        .overlay {
                            Circle()
                                .stroke(CagenticTheme.border, lineWidth: 0.75)
                        }
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Settings")
            }
            .padding(.top, CagenticTheme.Spacing.xs)
        }
        .padding(.horizontal, CagenticTheme.Spacing.md)
        .padding(.vertical, CagenticTheme.Spacing.xs)
        .background(CagenticTheme.background)
    }

    private var canCreateChat: Bool {
        model.connectionState.isConnected
            && !model.availableModels.isEmpty
            && !model.isGenerating
    }

    private var statusSymbol: String {
        switch model.connectionState {
        case .notConfigured: "link.badge.plus"
        case .connecting: "arrow.trianglehead.2.clockwise.rotate.90"
        case .connected: "checkmark.circle.fill"
        case .failed: "exclamationmark.triangle.fill"
        }
    }

    private var statusColor: Color {
        switch model.connectionState {
        case .notConfigured: CagenticTheme.textSecondary
        case .connecting: CagenticTheme.warning
        case .connected: CagenticTheme.success
        case .failed: CagenticTheme.error
        }
    }
}

#Preview("Sidebar") {
    NavigationStack {
        SidebarView(model: .preview(), isPresented: .constant(true))
    }
}
