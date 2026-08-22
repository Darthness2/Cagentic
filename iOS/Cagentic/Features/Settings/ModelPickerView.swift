import SwiftUI
import UIKit

/// Model selection remains in the chat toolbar's compact menu. This sheet is the deeper,
/// intentionally secondary surface for metadata and installing a model on the active server.
struct ModelPickerView: View {
    @Bindable var model: AppModel

    @Environment(\.dismiss) private var dismiss
    @FocusState private var isModelNameFocused: Bool
    @State private var modelName = ""
    @State private var pullTask: Task<Void, Never>?
    @State private var successMessage: String?

    var body: some View {
        NavigationStack {
            List {
                installSection

                Section("Installed") {
                    if model.availableModels.isEmpty {
                        emptyState
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    } else {
                        ForEach(model.availableModels) { option in
                            Button {
                                model.selectModel(option.name)
                                dismiss()
                            } label: {
                                ModelOptionRow(
                                    option: option,
                                    isSelected: option.name == model.activeModelIdentifier
                                )
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .background(CagenticTheme.background)
            .navigationTitle("Models")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable {
                await model.refreshConnection()
                await model.refreshModelMetadata()
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") {
                        dismiss()
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Refresh", systemImage: "arrow.clockwise") {
                        Task {
                            await model.refreshConnection()
                            await model.refreshModelMetadata()
                        }
                    }
                    .disabled(isBusy)
                }
            }
            .task {
                await model.refreshModelMetadata()
            }
            .onDisappear {
                pullTask?.cancel()
            }
        }
    }

    private var installSection: some View {
        Section {
            HStack(spacing: CagenticTheme.Spacing.xs) {
                TextField("Model name, for example gemma3:4b", text: $modelName)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($isModelNameFocused)
                    .submitLabel(.go)
                    .onSubmit(startPull)
                    .disabled(isBusy || !model.connectionState.isConnected)
                    .accessibilityLabel("Model to install")

                if model.pullingModelName != nil {
                    Button("Cancel", systemImage: "xmark") {
                        pullTask?.cancel()
                    }
                    .labelStyle(.iconOnly)
                    .frame(width: 44, height: 44)
                } else {
                    Button("Install", systemImage: "arrow.down.circle.fill", action: startPull)
                        .labelStyle(.iconOnly)
                        .font(.title3)
                        .foregroundStyle(canPull ? CagenticTheme.accent : CagenticTheme.textTertiary)
                        .frame(width: 44, height: 44)
                        .disabled(!canPull)
                }
            }

            if let status = model.modelPullStatus {
                HStack(spacing: CagenticTheme.Spacing.xs) {
                    ProgressView()
                        .controlSize(.small)
                    Text(status)
                        .font(CagenticTheme.FontStyle.caption)
                        .foregroundStyle(CagenticTheme.textSecondary)
                }
                .accessibilityElement(children: .combine)
            } else if let successMessage {
                Label(successMessage, systemImage: "checkmark.circle.fill")
                    .font(CagenticTheme.FontStyle.caption)
                    .foregroundStyle(CagenticTheme.success)
            }
        } header: {
            Text("Install from Ollama")
        } footer: {
            Text("The download runs on \(model.settings.serverName), not on this iPhone. Large models may take several minutes and require enough storage on that computer.")
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("No installed models", systemImage: "cpu")
        } description: {
            Text(
                model.connectionState.isConnected
                    ? "Enter a model name above to install one on the active server."
                    : "Connect to Ollama first."
            )
        } actions: {
            if !model.connectionState.isConnected {
                Button("Manage servers") {
                    model.queueSheetAfterDismiss(.serverManager)
                }
                .buttonStyle(.borderedProminent)
            }
        }
    }

    private var canPull: Bool {
        !modelName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && model.connectionState.isConnected
            && !isBusy
    }

    private var isBusy: Bool {
        model.pullingModelName != nil || model.isLoadingModelMetadata || isRefreshing
    }

    private var isRefreshing: Bool {
        if case .connecting = model.connectionState {
            return true
        }
        return false
    }

    private func startPull() {
        guard canPull else { return }
        let requestedName = modelName.trimmingCharacters(in: .whitespacesAndNewlines)
        successMessage = nil
        isModelNameFocused = false
        pullTask?.cancel()
        pullTask = Task {
            let result = await model.pullModel(named: requestedName)
            guard !Task.isCancelled else { return }
            switch result {
            case .success:
                modelName = ""
                successMessage = "\(requestedName) is ready"
                UIAccessibility.post(notification: .announcement, argument: "Model installed")
                await model.refreshModelMetadata()
            case .failure(let error):
                guard !(error is CancellationError) else { return }
                model.notice = AppNotice(
                    title: "Couldn’t install model",
                    message: error.localizedDescription
                )
            }
        }
    }
}

private struct ModelOptionRow: View {
    let option: AvailableModel
    let isSelected: Bool

    var body: some View {
        HStack(alignment: .top, spacing: CagenticTheme.Spacing.sm) {
            Image(systemName: modelSymbol)
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(isSelected ? CagenticTheme.accent : CagenticTheme.textSecondary)
                .frame(width: 28, height: 28)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                Text(option.shortName)
                    .font(CagenticTheme.FontStyle.bodySemibold)
                    .foregroundStyle(CagenticTheme.textPrimary)

                if !option.metadataDescription.isEmpty {
                    Text(option.metadataDescription)
                        .font(CagenticTheme.FontStyle.metadata)
                        .foregroundStyle(CagenticTheme.textSecondary)
                        .lineLimit(2)
                }

                if option.metadataLoaded {
                    ScrollView(.horizontal) {
                        HStack(spacing: CagenticTheme.Spacing.xxs) {
                            ForEach(option.sortedCapabilities, id: \.self) { capability in
                                ModelCapabilityBadge(capability: capability)
                            }
                            if let contextLength = option.contextLength {
                                ModelCapabilityBadge(capability: "\(contextLength.formatted()) ctx")
                            }
                        }
                    }
                    .scrollIndicators(.hidden)
                } else {
                    Text("Checking capabilities…")
                        .font(CagenticTheme.FontStyle.metadata)
                        .foregroundStyle(CagenticTheme.textTertiary)
                }
            }

            Spacer(minLength: CagenticTheme.Spacing.sm)

            if isSelected {
                Image(systemName: "checkmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(CagenticTheme.accent)
                    .accessibilityHidden(true)
            }
        }
        .frame(minHeight: 64)
        .contentShape(.rect)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }

    private var modelSymbol: String {
        option.capabilities.contains("vision") ? "eye" : "cpu"
    }
}

private struct ModelCapabilityBadge: View {
    let capability: String

    var body: some View {
        Text(capability.replacingOccurrences(of: "completion", with: "chat").capitalized)
            .font(CagenticTheme.FontStyle.metadata)
            .foregroundStyle(CagenticTheme.textSecondary)
            .padding(.horizontal, CagenticTheme.Spacing.xs)
            .padding(.vertical, 3)
            .background(CagenticTheme.surfaceRaised, in: .capsule)
            .overlay {
                Capsule().stroke(CagenticTheme.border, lineWidth: 0.5)
            }
    }
}

#Preview("Model library") {
    ModelPickerView(model: .preview())
}
