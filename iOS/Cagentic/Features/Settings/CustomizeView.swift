import SwiftUI

/// Profiles: named sets of instructions and dials to talk to a model with.
///
/// Independent of both the server and the model — any profile works with any model, and one may
/// name a preferred model that gets selected along with it.
///
/// Each control says whether the connected backend can honour it. That is not decoration: on a
/// gateway the computer owns the loop and only the system prompt, temperature and reasoning
/// effort have anywhere to land, so top-P, context and keep-alive genuinely do nothing there.
/// They were shown unqualified before, which made the whole section look like it worked.
struct CustomizeView: View {
    @Bindable var model: AppModel
    @Binding var draft: AppSettings

    var body: some View {
        List {
            Section {
                ForEach(draft.chatProfiles.profiles) { profile in
                    NavigationLink {
                        ChatProfileEditor(
                            profile: binding(for: profile.id),
                            isUsingGateway: model.isUsingGateway,
                            availableModels: model.availableModels.map(\.name)
                        )
                    } label: {
                        row(for: profile)
                    }
                }
                .onDelete(perform: deleteProfiles)

                Button("Add profile", systemImage: "plus") {
                    addProfile()
                }
            } header: {
                Text("Profiles").textCase(nil)
            } footer: {
                Text(backendNote)
            }
        }
        .scrollContentBackground(.hidden)
        .background(CagenticTheme.background)
        .navigationTitle("Customize")
        .navigationBarTitleDisplayMode(.inline)
    }

    private var backendNote: String {
        model.isUsingGateway
            ? "Switching profile applies it to the chat you are in. The computer owns the rest of "
                + "its setup, so top-P, context window and keep-alive are Ollama only."
            : "Switching profile applies its instructions and dials to new turns."
    }

    @ViewBuilder
    private func row(for profile: ChatProfile) -> some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
            Text(profile.displayName)
                .font(CagenticTheme.FontStyle.body)
                .foregroundStyle(CagenticTheme.textPrimary)
            if let detail = subtitle(for: profile) {
                Text(detail)
                    .font(CagenticTheme.FontStyle.caption)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .lineLimit(1)
            }
        }
    }

    private func subtitle(for profile: ChatProfile) -> String? {
        let prompt = profile.systemPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        if !prompt.isEmpty { return prompt }
        if let preferred = profile.preferredModel, !preferred.isEmpty { return preferred }
        return nil
    }

    /// Edits write straight back into the catalog so the sheet's single Cancel/Done still governs
    /// the whole change — nothing here commits on its own.
    private func binding(for id: ChatProfileID) -> Binding<ChatProfile> {
        Binding(
            get: { draft.chatProfiles.profile(id: id) ?? ChatProfile(name: "") },
            set: { updated in
                var edited = updated
                edited.updatedAt = .now
                draft.chatProfiles.upsert(edited)
            }
        )
    }

    private func addProfile() {
        // Seeded from what is in effect now, which is a more useful starting point than an empty
        // form: most profiles are a small edit away from the one already being used.
        draft.chatProfiles.upsert(
            ChatProfile(
                name: "New profile",
                systemPrompt: draft.systemPrompt,
                generation: draft.generation
            )
        )
    }

    private func deleteProfiles(at offsets: IndexSet) {
        for index in offsets {
            guard let profile = draft.chatProfiles.profiles[safe: index] else { continue }
            draft.chatProfiles.remove(id: profile.id)
        }
    }
}

private struct ChatProfileEditor: View {
    @Binding var profile: ChatProfile
    let isUsingGateway: Bool
    let availableModels: [String]

    var body: some View {
        Form {
            Section {
                TextField("Name", text: $profile.name)
            }

            Section {
                TextEditor(text: $profile.systemPrompt)
                    .frame(minHeight: 120)
                    .accessibilityLabel("System prompt")
            } header: {
                Text("Instructions").textCase(nil)
            } footer: {
                Text("Sent with each turn in this profile.")
            }

            Section {
                LabeledContent("Temperature", value: profile.generation.temperature, format: .number.precision(.fractionLength(1)))
                Slider(value: $profile.generation.temperature, in: 0...2, step: 0.1)
                    .accessibilityLabel("Temperature")

                Picker("Reasoning effort", selection: effortSelection) {
                    Text("Model default").tag("")
                    ForEach(ChatEffort.allCases) { effort in
                        Text(effort.title).tag(effort.rawValue)
                    }
                }
            } header: {
                Text("Sampling").textCase(nil)
            } footer: {
                Text(note(for: .temperature))
            }

            Section {
                LabeledContent("Top P", value: profile.generation.topP, format: .number.precision(.fractionLength(2)))
                Slider(value: $profile.generation.topP, in: 0.05...1, step: 0.05)
                    .accessibilityLabel("Top P")
                Stepper(
                    "Context window: \(profile.generation.contextLength)",
                    value: $profile.generation.contextLength,
                    in: 1_024...131_072,
                    step: 1_024
                )
                TextField("Keep alive", text: $profile.generation.keepAlive)
                Toggle("Show model thinking", isOn: $profile.generation.enableThinking)
            } header: {
                Text("Ollama").textCase(nil)
            } footer: {
                Text(note(for: .topP))
            }
            .disabled(isUsingGateway)

            Section {
                Picker("Preferred model", selection: preferredModelSelection) {
                    Text("Whatever is selected").tag("")
                    ForEach(availableModels, id: \.self) { name in
                        Text(name).tag(name)
                    }
                    // A profile can name a model that is not installed here; showing it keeps the
                    // picker from silently rewriting the choice.
                    if let preferred = profile.preferredModel,
                       !preferred.isEmpty,
                       !availableModels.contains(preferred)
                    {
                        Text("\(preferred) (not installed)").tag(preferred)
                    }
                }
            } footer: {
                Text("Selected along with the profile, when it is installed.")
            }
        }
        .scrollContentBackground(.hidden)
        .background(CagenticTheme.background)
        .navigationTitle(profile.displayName)
        .navigationBarTitleDisplayMode(.inline)
    }

    private func note(for field: ChatProfileField) -> String {
        guard isUsingGateway, !field.appliesOnGateway else { return "" }
        return ChatProfileField.ollamaOnlyNote
    }

    private var effortSelection: Binding<String> {
        Binding(
            get: { profile.effort?.rawValue ?? "" },
            set: { profile.effort = ChatEffort(rawValue: $0) }
        )
    }

    private var preferredModelSelection: Binding<String> {
        Binding(
            get: { profile.preferredModel ?? "" },
            set: { profile.preferredModel = $0.isEmpty ? nil : $0 }
        )
    }
}

extension Array {
    fileprivate subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
