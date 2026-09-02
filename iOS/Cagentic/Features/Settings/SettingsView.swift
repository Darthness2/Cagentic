import SwiftUI

struct SettingsView: View {
    @Bindable var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var draft: AppSettings

    init(model: AppModel) {
        self.model = model
        _draft = State(initialValue: model.settings)
    }

    var body: some View {
        NavigationStack {
            Form {
                connectionSection
                modelSection
                customizeSection
                appearanceSection
                chatsSection
                privacySection
            }
            .scrollContentBackground(.hidden)
            .background(CagenticTheme.background)
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        dismiss()
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        model.updateSettings(draft)
                        dismiss()
                    }
                    .fontWeight(.semibold)
                }
            }
        }
    }

    private var connectionSection: some View {
        Section {
            Button {
                model.queueSheetAfterDismiss(.serverManager)
            } label: {
                LabeledContent {
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(model.connectionState.title)
                            .foregroundStyle(model.connectionState.isConnected ? CagenticTheme.success : CagenticTheme.warning)
                        Text(model.settings.serverURL)
                            .font(CagenticTheme.FontStyle.metadata)
                            .foregroundStyle(CagenticTheme.textSecondary)
                            .lineLimit(1)
                    }
                } label: {
                    Label(
                        model.activeServerKind == .gateway ? "Cagentic gateway" : "Ollama server",
                        systemImage: model.activeServerKind == .gateway
                            ? "sparkles.rectangle.stack"
                            : "desktopcomputer"
                    )
                        .foregroundStyle(CagenticTheme.textPrimary)
                }
            }
            .buttonStyle(.plain)
        } header: {
            Text("Connection").textCase(nil)
        }
    }

    private var modelSection: some View {
        Section {
            Picker("Default model", selection: $draft.selectedModel) {
                if !draft.selectedModel.isEmpty,
                   !model.availableModels.contains(where: { $0.name == draft.selectedModel })
                {
                    Text("\(savedModelName) (saved)").tag(draft.selectedModel)
                } else if model.availableModels.isEmpty {
                    Text("No models available").tag("")
                }

                ForEach(model.availableModels) { option in
                    Text(option.shortName).tag(option.name)
                }
            }
            .disabled(model.availableModels.isEmpty)
        } header: {
            Text("Model").textCase(nil)
        } footer: {
            if model.availableModels.isEmpty {
                Text("Reconnect to the Ollama server to change the default model.")
            }
        }
    }

    private var savedModelName: String {
        draft.selectedModel.replacingOccurrences(of: ":latest", with: "")
    }

    private var customizeSection: some View {
        Section {
            NavigationLink {
                CustomizeView(model: model, draft: $draft)
            } label: {
                Label("Customize", systemImage: "slider.horizontal.3")
            }
        } header: {
            Text("Model").textCase(nil)
        } footer: {
            // These used to be loose controls here, which read as global settings. They are per
            // profile now, because that is what makes more than one of them possible — and on a
            // gateway connection several of them never applied at all.
            Text("Instructions, temperature, and the rest, kept as profiles you can switch between.")
        }
    }

    private var appearanceSection: some View {
        Section {
            Picker("Appearance", selection: $draft.appearance) {
                ForEach(AppearancePreference.allCases) { preference in
                    Text(preference.title).tag(preference)
                }
            }
            Toggle("Haptic feedback", isOn: $draft.hapticsEnabled)
        } header: {
            Text("Experience").textCase(nil)
        }
    }

    private var chatsSection: some View {
        Section {
            NavigationLink {
                ConversationManagerView(model: model)
            } label: {
                Label("Manage chats", systemImage: "checklist")
            }
        } header: {
            Text("Chats").textCase(nil)
        } footer: {
            // The only way back to an archived chat, so it says so rather than leaving someone to
            // wonder where a chat they swiped away went.
            Text("Archive, restore, or delete chats in bulk. Archived chats live here.")
        }
    }

    private var privacySection: some View {
        Section {
            Label("Chats are stored locally on this device.", systemImage: "iphone.gen3")
            Label("The optional proxy token is stored in Keychain.", systemImage: "key")
            Label("Direct Ollama traffic is not sent through Cagentic cloud services.", systemImage: "network.slash")
        } header: {
            Text("Privacy").textCase(nil)
        }
    }
}

struct RenameConversationView: View {
    @Bindable var model: AppModel
    let conversationID: UUID
    @Environment(\.dismiss) private var dismiss
    @State private var title: String
    @FocusState private var isTitleFocused: Bool

    init(model: AppModel, conversationID: UUID) {
        self.model = model
        self.conversationID = conversationID
        _title = State(initialValue: model.conversation(id: conversationID)?.title ?? "")
    }

    var body: some View {
        NavigationStack {
            Form {
                TextField("Chat title", text: $title)
                    .focused($isTitleFocused)
                    .submitLabel(.done)
                    .onSubmit(save)
                    .accessibilityLabel("Chat title")
            }
            .navigationTitle("Rename chat")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save", action: save)
                        .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .defaultFocus($isTitleFocused, true)
        }
        .presentationDetents([.medium])
        .presentationDragIndicator(.visible)
    }

    private func save() {
        let cleanedTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanedTitle.isEmpty else { return }
        model.renameConversation(id: conversationID, title: cleanedTitle)
        dismiss()
    }
}

#Preview("Settings") {
    SettingsView(model: .preview())
}
