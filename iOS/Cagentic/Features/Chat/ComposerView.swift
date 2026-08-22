import CoreTransferable
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

struct ComposerView: View {
    @Bindable var model: AppModel
    var showsConnectionRecovery = true
    @FocusState private var isComposerFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var selectedPhotoItems: [PhotosPickerItem] = []
    @State private var attachmentPresentation: AttachmentPresentation?
    @State private var isPreparingAttachmentSelection = false
    @State private var attachmentImportTask: Task<Void, Never>?

    var body: some View {
        VStack(spacing: CagenticTheme.Spacing.xs) {
            VStack(spacing: CagenticTheme.Spacing.xs) {
                if showsConnectionRecovery, !model.connectionState.isConnected {
                    connectionRecoveryRow
                }

                if !model.pendingAttachments.isEmpty || isAttachmentImportInProgress {
                    pendingAttachmentTray
                        .transition(
                            reduceMotion
                                ? .opacity
                                : .move(edge: .bottom).combined(with: .opacity)
                        )
                }

                HStack(alignment: .bottom, spacing: CagenticTheme.Spacing.xxs) {
                    // A gateway turn is a plain message string — there is nowhere to put a file —
                    // so the affordance is absent rather than present and failing.
                    if model.canUseFileAttachments {
                        attachmentButton
                    }

                    TextField("Ask Cagentic…", text: $model.draft, axis: .vertical)
                        .font(CagenticTheme.FontStyle.body)
                        .lineLimit(1 ... 8)
                        .focused($isComposerFocused)
                        .submitLabel(.send)
                        .onSubmit(submitDraft)
                        .disabled(model.isGenerating)
                        .accessibilityLabel("Message")
                        .padding(.vertical, 11)
                        .layoutPriority(1)

                    primaryButton
                }

                if model.isGenerating, !model.isGeneratingSelectedConversation {
                    otherChatGenerationRow
                }
            }
            .padding(.leading, CagenticTheme.Spacing.xs)
            .padding(.trailing, CagenticTheme.Spacing.xs)
            .padding(.top, CagenticTheme.Spacing.xs)
            .padding(.bottom, CagenticTheme.Spacing.xs)
            .background {
                ZStack {
                    Rectangle().fill(.ultraThinMaterial)
                    CagenticTheme.surface.opacity(0.42)
                }
            }
            .overlay {
                RoundedRectangle(cornerRadius: CagenticTheme.Radius.composer)
                    .stroke(
                        isComposerFocused
                            ? CagenticTheme.accent.opacity(0.72)
                            : CagenticTheme.border,
                        lineWidth: isComposerFocused ? 1 : 0.75
                    )
            }
            .compositingGroup()
            .clipShape(.rect(cornerRadius: CagenticTheme.Radius.composer))
            .shadow(color: .black.opacity(0.08), radius: 16, y: 6)

        }
        .frame(maxWidth: 768)
        .padding(.horizontal, CagenticTheme.Spacing.md)
        .padding(.top, CagenticTheme.Spacing.xs)
        .padding(.bottom, CagenticTheme.Spacing.xxs)
        .frame(maxWidth: .infinity)
        .sensoryFeedback(.impact(weight: .light), trigger: model.hapticTrigger) { _, _ in
            model.settings.hapticsEnabled
        }
        .fileImporter(
            isPresented: presentationBinding(for: .files),
            allowedContentTypes: Self.supportedDocumentTypes,
            allowsMultipleSelection: true,
            onCompletion: handleFilePickerResult
        )
        .photosPicker(
            isPresented: presentationBinding(for: .photos),
            selection: $selectedPhotoItems,
            maxSelectionCount: remainingAttachmentCapacity,
            matching: .images
        )
        .confirmationDialog(
            "Add attachment",
            isPresented: presentationBinding(for: .options),
            titleVisibility: .visible
        ) {
            attachmentOptions
        }
        .onChange(of: selectedPhotoItems) { _, newItems in
            guard !newItems.isEmpty else { return }
            beginPhotoImport(newItems)
        }
        .onDisappear {
            attachmentPresentation = nil
            attachmentImportTask?.cancel()
        }
    }

    private var canActivate: Bool {
        model.isGeneratingSelectedConversation
            || (!model.isGenerating && model.canSendDraft && !isPreparingAttachmentSelection)
    }

    private var isAttachmentImportInProgress: Bool {
        isPreparingAttachmentSelection || model.isImportingAttachments
    }

    private var attachmentButton: some View {
        Button {
            attachmentPresentation = .options
        } label: {
            Image(systemName: "plus")
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(CagenticTheme.textSecondary)
                .frame(width: 44, height: 44)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .disabled(!canBeginAttachmentImport)
        .accessibilityLabel("Add attachment")
        .accessibilityHint("Shows photo and file attachment options")
    }

    @ViewBuilder
    private var attachmentOptions: some View {
        if model.canUsePhotoAttachments {
            Button("Photos", systemImage: "photo.on.rectangle.angled") {
                selectedPhotoItems = []
                attachmentPresentation = .photos
            }
            .disabled(!canBeginAttachmentImport)
        } else {
            Button("Photos", systemImage: "photo.on.rectangle.angled") {
                model.notice = AppNotice(
                    title: "Choose a vision model",
                    message: "The selected model does not accept images. Switch to a model with the vision capability, then attach a photo."
                )
            }
            .disabled(model.isGenerating || isAttachmentImportInProgress)
        }

        Button("Files", systemImage: "doc.badge.plus") {
            attachmentPresentation = .files
        }
        .disabled(!canBeginAttachmentImport)

        Button("Cancel", role: .cancel) {}
    }

    private var pendingAttachmentTray: some View {
        ScrollView(.horizontal) {
            HStack(spacing: CagenticTheme.Spacing.xs) {
                ForEach(model.pendingAttachments) { attachment in
                    AttachmentChip(
                        attachment: attachment,
                        onRemove: {
                            model.removePendingAttachment(attachment.id)
                        },
                        loadPayload: {
                            try await model.attachmentPayload(for: attachment)
                        }
                    )
                }

                if isAttachmentImportInProgress {
                    HStack(spacing: CagenticTheme.Spacing.xs) {
                        ProgressView()
                            .controlSize(.small)
                            .accessibilityHidden(true)
                        Text("Preparing")
                            .font(CagenticTheme.FontStyle.captionMedium)
                            .foregroundStyle(CagenticTheme.textSecondary)
                    }
                    .padding(.horizontal, CagenticTheme.Spacing.sm)
                    .frame(minHeight: 44)
                    .background(CagenticTheme.accentSoft.opacity(0.58), in: .capsule)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Preparing attachments")
                }
            }
        }
        .scrollIndicators(.hidden)
        .contentMargins(.horizontal, 0, for: .scrollContent)
        .animation(
            reduceMotion ? nil : .easeOut(duration: 0.18),
            value: model.pendingAttachments.map(\.id)
        )
    }

    private var primaryButton: some View {
        Button(action: primaryAction) {
            Image(systemName: model.isGeneratingSelectedConversation ? "stop.fill" : "arrow.up")
                .font(.system(size: 16, weight: .bold))
                .foregroundStyle(CagenticTheme.onAccent)
                .frame(width: 40, height: 40)
                .background(
                    canActivate ? CagenticTheme.accent : CagenticTheme.textTertiary.opacity(0.45),
                    in: Circle()
                )
        }
        .frame(width: 44, height: 44)
        .disabled(!canActivate)
        .accessibilityLabel(
            model.isGeneratingSelectedConversation ? "Stop response" : "Send message"
        )
    }

    private func primaryAction() {
        if model.isGeneratingSelectedConversation {
            model.stopGenerating()
        } else {
            submitDraft()
        }
    }

    private var connectionRecoveryRow: some View {
        HStack(spacing: CagenticTheme.Spacing.xs) {
            Label(model.connectionState.title, systemImage: connectionStatusIcon)
                .font(CagenticTheme.FontStyle.captionMedium)
                .foregroundStyle(CagenticTheme.textSecondary)
                .lineLimit(1)

            Spacer(minLength: CagenticTheme.Spacing.xxs)

            if !model.settings.serverURL.isEmpty {
                Button("Retry") {
                    Task {
                        await model.refreshConnection()
                    }
                }
                .font(CagenticTheme.FontStyle.captionMedium)
                .disabled(isConnecting)
                .accessibilityHint("Attempts to reconnect to the selected server")
            }

            Button {
                model.presentedSheet = .serverManager
            } label: {
                Image(systemName: "server.rack")
                    .frame(width: 44, height: 44)
                    .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Manage servers")
            .accessibilityHint("Opens server settings")
        }
        .frame(minHeight: 44)
        .padding(.leading, CagenticTheme.Spacing.xs)
    }

    private var otherChatGenerationRow: some View {
        HStack(spacing: CagenticTheme.Spacing.xs) {
            Label("Another chat is responding", systemImage: "waveform")
                .font(CagenticTheme.FontStyle.caption)
                .foregroundStyle(CagenticTheme.textSecondary)
                .lineLimit(1)

            Spacer(minLength: CagenticTheme.Spacing.xxs)

            if let conversationID = activeGenerationConversationID {
                Button("View") {
                    model.selectConversation(conversationID)
                }
                .font(CagenticTheme.FontStyle.captionMedium)
                .frame(minHeight: 44)
                .accessibilityHint("Opens the chat that is generating a response")
            }
        }
        .padding(.leading, CagenticTheme.Spacing.xs)
    }

    private var activeGenerationConversationID: UUID? {
        guard let conversationID = model.lastStreamConversationID,
              conversationID != model.selectedConversationID,
              model.conversation(id: conversationID) != nil
        else {
            return nil
        }
        return conversationID
    }

    private var connectionStatusIcon: String {
        if isConnecting {
            return "arrow.trianglehead.2.clockwise.rotate.90"
        }
        return model.settings.serverURL.isEmpty ? "server.rack" : "wifi.exclamationmark"
    }

    private var isConnecting: Bool {
        if case .connecting = model.connectionState {
            return true
        }
        return false
    }

    private func submitDraft() {
        guard !isPreparingAttachmentSelection else { return }
        model.sendDraft()
    }

    private var canBeginAttachmentImport: Bool {
        !model.isGenerating
            && !isAttachmentImportInProgress
            && model.pendingAttachments.count < AttachmentImportLimits.default.maximumAttachmentCount
    }

    private var remainingAttachmentCapacity: Int {
        max(
            1,
            AttachmentImportLimits.default.maximumAttachmentCount - model.pendingAttachments.count
        )
    }

    private func presentationBinding(
        for presentation: AttachmentPresentation
    ) -> Binding<Bool> {
        Binding(
            get: { attachmentPresentation == presentation },
            set: { isPresented in
                if isPresented {
                    attachmentPresentation = presentation
                } else if attachmentPresentation == presentation {
                    attachmentPresentation = nil
                }
            }
        )
    }

    private func handleFilePickerResult(_ result: Result<[URL], any Error>) {
        switch result {
        case .success(let urls):
            guard !urls.isEmpty else { return }
            beginAttachmentImport {
                await model.importFileAttachments(
                    from: urls.map { AttachmentImportSource(url: $0) }
                )
            }
        case .failure(let error):
            presentAttachmentError(error)
        }
    }

    private func beginPhotoImport(_ items: [PhotosPickerItem]) {
        attachmentImportTask?.cancel()
        attachmentImportTask = Task {
            isPreparingAttachmentSelection = true
            var transfers: [AttachmentPhotoTransfer] = []
            transfers.reserveCapacity(items.count)
            defer {
                removeTemporaryDirectories(for: transfers)
                isPreparingAttachmentSelection = false
                selectedPhotoItems = []
            }

            do {
                for item in items {
                    try Task.checkCancellation()
                    guard let transfer = try await item.loadTransferable(
                        type: AttachmentPhotoTransfer.self
                    ) else {
                        throw ComposerAttachmentError.photoUnavailable
                    }
                    transfers.append(transfer)
                }

                try Task.checkCancellation()
                let sources = zip(items, transfers).map { item, transfer in
                    AttachmentImportSource(
                        url: transfer.fileURL,
                        declaredContentTypeIdentifier: item.supportedContentTypes.first?.identifier
                    )
                }
                let result = await model.importFileAttachments(from: sources)
                try Task.checkCancellation()
                if case .failure(let error) = result {
                    throw error
                }
            } catch is CancellationError {
                return
            } catch {
                presentAttachmentError(error)
            }
        }
    }

    private func removeTemporaryDirectories(for transfers: [AttachmentPhotoTransfer]) {
        for directory in Set(transfers.map(\.temporaryDirectory)) {
            try? FileManager.default.removeItem(at: directory)
        }
    }

    private func beginAttachmentImport(
        operation: @escaping @MainActor () async -> Result<Void, any Error>
    ) {
        attachmentImportTask?.cancel()
        attachmentImportTask = Task {
            isPreparingAttachmentSelection = true
            defer { isPreparingAttachmentSelection = false }

            let result = await operation()
            guard !Task.isCancelled else { return }
            if case .failure(let error) = result {
                presentAttachmentError(error)
            }
        }
    }

    private func presentAttachmentError(_ error: any Error) {
        guard !(error is CancellationError) else { return }

        if error as? AttachmentError == .payloadTypeMismatch
            || error as? AttachmentError == .visionModelRequired
        {
            model.notice = AppNotice(
                title: "Choose a vision model",
                message: "The selected model does not accept images. Switch to a model with the vision capability, then attach the photo again."
            )
        } else {
            model.notice = AppNotice(
                title: "Couldn’t attach that item",
                message: error.localizedDescription
            )
        }
    }

    private static let supportedDocumentTypes: [UTType] = [
        .image,
        .pdf,
        .text,
        .sourceCode,
    ]
}

private enum AttachmentPresentation: Equatable {
    case options
    case photos
    case files
}

private nonisolated struct AttachmentPhotoTransfer: Transferable, Sendable {
    let fileURL: URL
    let temporaryDirectory: URL

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .image) { received in
            let fileManager = FileManager.default
            let sourceName = received.file.lastPathComponent
            let fileName = sourceName.isEmpty ? "Photo" : sourceName
            let maximumBytes = AttachmentImportLimits.default.maximumPhotoInputBytes
            if let fileSize = try received.file.resourceValues(forKeys: [.fileSizeKey]).fileSize,
               fileSize > maximumBytes
            {
                throw AttachmentError.inputTooLarge(
                    fileName: fileName,
                    maximumBytes: maximumBytes
                )
            }
            let directory = fileManager.temporaryDirectory
                .appending(path: "CagenticPhotoImports", directoryHint: .isDirectory)
                .appending(path: UUID().uuidString, directoryHint: .isDirectory)
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            let destination = directory.appending(path: fileName, directoryHint: .notDirectory)
            do {
                try fileManager.copyItem(at: received.file, to: destination)
                return AttachmentPhotoTransfer(
                    fileURL: destination,
                    temporaryDirectory: directory
                )
            } catch {
                try? fileManager.removeItem(at: directory)
                throw error
            }
        }
    }
}

private enum ComposerAttachmentError: LocalizedError {
    case photoUnavailable

    var errorDescription: String? {
        switch self {
        case .photoUnavailable:
            "That photo could not be loaded. Download it from iCloud Photos and try again."
        }
    }
}

#Preview("Composer") {
    ComposerView(model: .preview())
        .background(CagenticTheme.stage)
}
