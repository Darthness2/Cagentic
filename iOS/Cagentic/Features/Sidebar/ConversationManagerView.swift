import SwiftUI

struct ConversationManagerView: View {
    @Bindable var model: AppModel

    @Environment(\.dismiss) private var dismiss
    @State private var scope: ConversationScope = .active
    @State private var selection: Set<UUID> = []
    @State private var isDeleteConfirmationPresented = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Conversation status", selection: $scope) {
                    ForEach(ConversationScope.allCases) { scope in
                        Text(scope.title).tag(scope)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, CagenticTheme.Spacing.md)
                .padding(.vertical, CagenticTheme.Spacing.sm)

                if scopedConversations.isEmpty {
                    ContentUnavailableView(
                        scope.emptyTitle,
                        systemImage: scope == .active ? "bubble.left.and.bubble.right" : "archivebox",
                        description: Text(scope.emptyDescription)
                    )
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    List(scopedConversations) { conversation in
                        Button {
                            toggleSelection(conversation.id)
                        } label: {
                            HStack(spacing: CagenticTheme.Spacing.sm) {
                                Image(
                                    systemName: selection.contains(conversation.id)
                                        ? "checkmark.circle.fill"
                                        : "circle"
                                )
                                .font(.title3)
                                .foregroundStyle(
                                    selection.contains(conversation.id)
                                        ? CagenticTheme.accent
                                        : CagenticTheme.textTertiary
                                )
                                .accessibilityHidden(true)

                                VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
                                    HStack(spacing: CagenticTheme.Spacing.xxs) {
                                        if conversation.isPinned {
                                            Image(systemName: "pin.fill")
                                                .font(.caption2)
                                                .foregroundStyle(CagenticTheme.accent)
                                                .accessibilityHidden(true)
                                        }
                                        Text(conversation.title)
                                            .font(CagenticTheme.FontStyle.bodyMedium)
                                            .foregroundStyle(CagenticTheme.textPrimary)
                                            .lineLimit(1)
                                    }
                                    Text(conversation.preview)
                                        .font(CagenticTheme.FontStyle.caption)
                                        .foregroundStyle(CagenticTheme.textSecondary)
                                        .lineLimit(1)
                                }

                                Spacer(minLength: CagenticTheme.Spacing.xs)
                            }
                            .frame(minHeight: 52)
                            .contentShape(.rect)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(conversation.title)
                        .accessibilityValue(
                            selection.contains(conversation.id) ? "Selected" : "Not selected"
                        )
                        .contextMenu {
                            if scope == .active {
                                Button(
                                    conversation.isPinned ? "Unpin" : "Pin",
                                    systemImage: "pin"
                                ) {
                                    model.togglePinned(conversation.id)
                                }
                            }
                            if let transcript = model.conversationExportText(id: conversation.id) {
                                ShareLink(item: transcript) {
                                    Label("Share", systemImage: "square.and.arrow.up")
                                }
                            }
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                }
            }
            .background(CagenticTheme.background)
            .navigationTitle("Manage chats")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") {
                        dismiss()
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button(selection.count == scopedConversations.count ? "Clear" : "Select all") {
                        toggleAll()
                    }
                    .disabled(scopedConversations.isEmpty)
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if !selection.isEmpty {
                    bulkActionBar
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.easeOut(duration: 0.18), value: selection.isEmpty)
            .onChange(of: scope) {
                selection.removeAll()
            }
            .confirmationDialog(
                "Delete selected chats?",
                isPresented: $isDeleteConfirmationPresented,
                titleVisibility: .visible
            ) {
                Button("Delete \(selection.count) chats", role: .destructive) {
                    model.deleteConversations(selection)
                    selection.removeAll()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("This permanently removes the selected conversations and their attachments from this device.")
            }
        }
    }

    private var scopedConversations: [ConversationSummary] {
        model.visibleConversationSummaries.filter { $0.isArchived == (scope == .archived) }
    }

    private var bulkActionBar: some View {
        HStack(spacing: CagenticTheme.Spacing.sm) {
            Button(scope == .active ? "Archive" : "Restore", systemImage: scope.actionIcon) {
                if scope == .active {
                    model.archiveConversations(selection)
                } else {
                    model.restoreConversations(selection)
                }
                selection.removeAll()
            }
            .buttonStyle(.borderedProminent)
            .frame(maxWidth: .infinity)

            Button("Delete", systemImage: "trash", role: .destructive) {
                isDeleteConfirmationPresented = true
            }
            .buttonStyle(.bordered)
            .tint(CagenticTheme.error)
        }
        .controlSize(.large)
        .padding(CagenticTheme.Spacing.md)
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) {
            Divider().overlay(CagenticTheme.border)
        }
    }

    private func toggleSelection(_ id: UUID) {
        if selection.contains(id) {
            selection.remove(id)
        } else {
            selection.insert(id)
        }
    }

    private func toggleAll() {
        let allIDs = Set(scopedConversations.map(\.id))
        selection = selection == allIDs ? [] : allIDs
    }
}

private enum ConversationScope: String, CaseIterable, Identifiable {
    case active
    case archived

    var id: Self { self }
    var title: String { self == .active ? "Active" : "Archived" }
    var actionIcon: String { self == .active ? "archivebox" : "arrow.uturn.backward" }
    var emptyTitle: String { self == .active ? "No active chats" : "No archived chats" }
    var emptyDescription: String {
        self == .active
            ? "Create a new chat to begin."
            : "Chats you archive will stay available here."
    }
}

#Preview("Manage conversations") {
    ConversationManagerView(model: .preview())
}
