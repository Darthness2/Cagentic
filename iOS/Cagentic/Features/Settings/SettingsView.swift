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
                generationSection
                personalizationSection
                appearanceSection
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

    private var generationSection: some View {
        Section {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                LabeledContent("Temperature") {
                    Text(draft.generation.temperature, format: .number.precision(.fractionLength(1)))
                        .monospacedDigit()
                }
                Slider(value: $draft.generation.temperature, in: 0 ... 2, step: 0.1)
                    .accessibilityLabel("Temperature")
                    .accessibilityValue(draft.generation.temperature.formatted(.number.precision(.fractionLength(1))))
            }

            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                LabeledContent("Top P") {
                    Text(draft.generation.topP, format: .number.precision(.fractionLength(2)))
                        .monospacedDigit()
                }
                Slider(value: $draft.generation.topP, in: 0.05 ... 1, step: 0.05)
                    .accessibilityLabel("Top P")
                    .accessibilityValue(draft.generation.topP.formatted(.number.precision(.fractionLength(2))))
            }

            Stepper(value: $draft.generation.contextLength, in: 1_024 ... 131_072, step: 1_024) {
                LabeledContent("Context window") {
                    Text(draft.generation.contextLength, format: .number)
                        .monospacedDigit()
                }
            }

            Toggle("Show model thinking", isOn: $draft.generation.enableThinking)

            LabeledContent("Keep alive") {
                TextField("Server default", text: $draft.generation.keepAlive)
                    .multilineTextAlignment(.trailing)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityLabel("Keep alive duration")
                    .accessibilityHint(
                        "Enter a duration such as 5m or 1h, or leave blank to use the server default."
                    )
            }
        } header: {
            Text("Generation").textCase(nil)
        } footer: {
            Text(
                "Larger context windows use more memory. Thinking is shown only when the "
                    + "selected model provides it. Use a keep-alive duration such as 5m or 1h."
            )
        }
    }

    private var personalizationSection: some View {
        Section {
            TextEditor(text: $draft.systemPrompt)
                .frame(minHeight: 112)
                .accessibilityLabel("System prompt")
        } header: {
            Text("System prompt").textCase(nil)
        } footer: {
            Text("This instruction is sent with each new turn and stays on your devices.")
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
