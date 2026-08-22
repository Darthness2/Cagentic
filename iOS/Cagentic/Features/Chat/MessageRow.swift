import SwiftUI
import UIKit
import ImageIO

struct MessageRow: View {
    let message: ChatMessage
    let modelName: String
    let onRegenerate: (() -> Void)?
    let onEdit: (() -> Void)?
    let attachmentPayload: (@MainActor @Sendable (AttachmentMetadata) async throws -> Data)?

    init(
        message: ChatMessage,
        modelName: String = "Assistant",
        onRegenerate: (() -> Void)? = nil,
        onEdit: (() -> Void)? = nil,
        attachmentPayload: (@MainActor @Sendable (AttachmentMetadata) async throws -> Data)? = nil
    ) {
        self.message = message
        self.modelName = modelName
        self.onRegenerate = onRegenerate
        self.onEdit = onEdit
        self.attachmentPayload = attachmentPayload
    }

    @ViewBuilder
    var body: some View {
        if message.role == .system {
            messageContent
        } else {
            accessibleMessageContent
        }
    }

    @ViewBuilder
    private var accessibleMessageContent: some View {
        let content = messageContent
            .contextMenu {
                messageActions
            }

        if message.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            secondaryAccessibilityAction(for: content)
        } else {
            secondaryAccessibilityAction(
                for: content.accessibilityAction(named: Text("Copy")) {
                    copyMessage()
                }
            )
        }
    }

    @ViewBuilder
    private func secondaryAccessibilityAction<Content: View>(for content: Content) -> some View {
        switch message.role {
        case .assistant:
            if let onRegenerate {
                content.accessibilityAction(named: Text("Regenerate"), onRegenerate)
            } else {
                content
            }
        case .user:
            if let onEdit {
                content.accessibilityAction(named: Text("Edit"), onEdit)
            } else {
                content
            }
        case .system:
            content
        }
    }

    @ViewBuilder
    private var messageContent: some View {
        switch message.role {
        case .assistant:
            AssistantMessageRow(
                message: message,
                modelName: modelName
            )
        case .user:
            UserMessageRow(message: message, attachmentPayload: attachmentPayload)
        case .system:
            SystemMessageRow(message: message)
        }
    }

    @ViewBuilder
    private var messageActions: some View {
        Button("Copy", systemImage: "doc.on.doc") {
            copyMessage()
        }
        .disabled(message.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

        switch message.role {
        case .assistant:
            Button("Regenerate", systemImage: "arrow.clockwise") {
                onRegenerate?()
            }
            .disabled(onRegenerate == nil)
        case .user:
            Button("Edit", systemImage: "pencil") {
                onEdit?()
            }
            .disabled(onEdit == nil)
        case .system:
            EmptyView()
        }
    }

    private func copyMessage() {
        UIPasteboard.general.string = message.content
        UIAccessibility.post(
            notification: .announcement,
            argument: message.role == .assistant ? "Response copied" : "Message copied"
        )
    }
}

private struct AssistantMessageRow: View {
    let message: ChatMessage
    let modelName: String

    var body: some View {
        HStack(alignment: .top, spacing: CagenticTheme.Spacing.sm) {
            AssistantMark(isStreaming: message.state == .streaming)

            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
                if !message.thinking.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    ThinkingDisclosure(
                        thinking: message.thinking,
                        isStreaming: message.state == .streaming
                    )
                }

                if !message.activity.isEmpty {
                    AssistantActivityTimeline(
                        activity: message.activity,
                        isStreaming: message.state == .streaming
                    )
                }

                if !hasContent {
                    if message.state == .streaming, message.activity.isEmpty {
                        StreamingPlaceholder()
                    }
                } else {
                    MarkdownText(message.content)
                }

                if message.state != .streaming || hasContent {
                    MessageStateView(
                        role: .assistant,
                        state: message.state,
                        errorDescription: message.errorDescription
                    )
                }

                if message.state == .complete, let metrics = message.metrics {
                    GenerationMetricsLabel(metrics: metrics)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(modelName) response")
        .onChange(of: message.state) { previousState, newState in
            guard previousState == .streaming, newState == .failed else { return }
            let announcement = ["Response failed", message.errorDescription]
                .compactMap { $0 }
                .joined(separator: ". ")
            UIAccessibility.post(notification: .announcement, argument: announcement)
        }
    }

    private var hasContent: Bool {
        !message.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

private struct UserMessageRow: View {
    let message: ChatMessage
    let attachmentPayload: (@MainActor @Sendable (AttachmentMetadata) async throws -> Data)?

    var body: some View {
        VStack(alignment: .trailing, spacing: CagenticTheme.Spacing.xxs) {
            if hasContent || !message.attachments.isEmpty {
                HStack(alignment: .top) {
                    Spacer(minLength: CagenticTheme.Spacing.xxl)

                    VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
                        if !message.attachments.isEmpty {
                            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
                                ForEach(message.attachments) { attachment in
                                    AttachmentChip(
                                        attachment: attachment,
                                        loadPayload: payloadLoader(for: attachment)
                                    )
                                }
                            }
                        }

                        if hasContent {
                            MarkdownText(message.content)
                        }
                    }
                    .padding(.horizontal, CagenticTheme.Spacing.md)
                    .padding(.vertical, CagenticTheme.Spacing.sm)
                    .background(CagenticTheme.accentSoft)
                    .compositingGroup()
                    .clipShape(.rect(cornerRadius: CagenticTheme.Radius.sheet))
                }
            }

            MessageStateView(
                role: .user,
                state: message.state,
                errorDescription: message.errorDescription
            )
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Your message")
    }

    private var hasContent: Bool {
        !message.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func payloadLoader(
        for attachment: AttachmentMetadata
    ) -> (@MainActor @Sendable () async throws -> Data)? {
        guard let attachmentPayload else { return nil }
        return { try await attachmentPayload(attachment) }
    }
}

struct AttachmentChip: View {
    let attachment: AttachmentMetadata
    var onRemove: (() -> Void)?
    var loadPayload: (@MainActor @Sendable () async throws -> Data)?

    @State private var thumbnail: UIImage?
    @State private var isPreviewPresented = false

    init(
        attachment: AttachmentMetadata,
        onRemove: (() -> Void)? = nil,
        loadPayload: (@MainActor @Sendable () async throws -> Data)? = nil
    ) {
        self.attachment = attachment
        self.onRemove = onRemove
        self.loadPayload = loadPayload
    }

    var body: some View {
        HStack(spacing: CagenticTheme.Spacing.xs) {
            if loadPayload != nil {
                Button {
                    isPreviewPresented = true
                } label: {
                    attachmentSummary
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Preview \(attachment.displayName)")
                .accessibilityHint("Opens the attachment preview")
            } else {
                attachmentSummary
            }

            if let onRemove {
                Button(action: onRemove) {
                    Image(systemName: "xmark")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(CagenticTheme.textSecondary)
                        .frame(width: 44, height: 44)
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Remove \(attachment.displayName)")
                .accessibilityHint("Removes this attachment from the message")
            }
        }
        .padding(.leading, CagenticTheme.Spacing.xs)
        .padding(.trailing, onRemove == nil ? CagenticTheme.Spacing.sm : 0)
        .frame(minHeight: 44)
        .background(CagenticTheme.accentSoft.opacity(0.44), in: .rect(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .stroke(CagenticTheme.border.opacity(0.65), lineWidth: 0.5)
        }
        .accessibilityElement(
            children: onRemove == nil && loadPayload == nil ? .combine : .contain
        )
        .accessibilityLabel(attachmentAccessibilityLabel)
        .task(id: attachment.id) {
            await loadThumbnailIfNeeded()
        }
        .sheet(isPresented: $isPreviewPresented) {
            AttachmentPreviewView(
                attachment: attachment,
                thumbnail: thumbnail,
                loadPayload: loadPayload
            )
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
    }

    private var attachmentSummary: some View {
        HStack(spacing: CagenticTheme.Spacing.xs) {
            Group {
                if let thumbnail, attachment.kind == .photo {
                    Image(uiImage: thumbnail)
                        .resizable()
                        .scaledToFill()
                } else {
                    Image(systemName: iconName)
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(CagenticTheme.accent)
                }
            }
            .frame(width: 38, height: 38)
            .background(CagenticTheme.accentSoft.opacity(0.82))
            .clipShape(.rect(cornerRadius: 9))
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 1) {
                Text(attachment.displayName)
                    .font(CagenticTheme.FontStyle.captionMedium)
                    .foregroundStyle(CagenticTheme.textPrimary)
                    .lineLimit(1)

                Text("\(typeName) · \(formattedSize)")
                    .font(CagenticTheme.FontStyle.metadata)
                    .foregroundStyle(CagenticTheme.textTertiary)
                    .lineLimit(1)
            }
            .frame(maxWidth: 190, alignment: .leading)
        }
    }

    private func loadThumbnailIfNeeded() async {
        guard attachment.kind == .photo,
              thumbnail == nil,
              let loadPayload,
              let data = try? await loadPayload()
        else {
            return
        }
        thumbnail = await AttachmentImageDecoder.shared.image(
            from: data,
            maximumPixelSize: 160
        )
    }

    private var iconName: String {
        switch attachment.kind {
        case .photo: "photo"
        case .pdf: "doc.richtext"
        case .textFile: "doc.text"
        }
    }

    private var typeName: String {
        switch attachment.kind {
        case .photo:
            "Photo"
        case .pdf:
            "PDF"
        case .textFile:
            inferredTextType
        }
    }

    private var inferredTextType: String {
        let pathExtension = (attachment.displayName as NSString).pathExtension
        guard !pathExtension.isEmpty, pathExtension.count <= 8 else { return "Text" }
        return pathExtension.uppercased()
    }

    private var formattedSize: String {
        ByteCountFormatter.string(fromByteCount: attachment.byteCount, countStyle: .file)
    }

    private var attachmentAccessibilityLabel: String {
        "\(typeName) attachment, \(attachment.displayName), \(formattedSize)"
    }
}

private struct AttachmentPreviewView: View {
    let attachment: AttachmentMetadata
    let thumbnail: UIImage?
    let loadPayload: (@MainActor @Sendable () async throws -> Data)?

    @Environment(\.dismiss) private var dismiss
    @State private var image: UIImage?
    @State private var textPreview: String?
    @State private var errorMessage: String?
    @State private var isLoading = true

    var body: some View {
        NavigationStack {
            ScrollView {
                Group {
                    if isLoading {
                        ProgressView("Opening preview…")
                            .frame(maxWidth: .infinity, minHeight: 240)
                    } else if let image {
                        Image(uiImage: image)
                            .resizable()
                            .scaledToFit()
                            .frame(maxWidth: .infinity)
                            .accessibilityLabel(attachment.displayName)
                    } else if let textPreview {
                        Text(textPreview)
                            .font(
                                attachment.kind == .textFile
                                    ? .system(.callout, design: .monospaced)
                                    : CagenticTheme.FontStyle.body
                            )
                            .foregroundStyle(CagenticTheme.textPrimary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        ContentUnavailableView(
                            "Preview unavailable",
                            systemImage: "doc.questionmark",
                            description: Text(errorMessage ?? "This attachment could not be opened.")
                        )
                    }
                }
                .padding(CagenticTheme.Spacing.md)
            }
            .background(CagenticTheme.background)
            .navigationTitle(attachment.displayName)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
            .safeAreaInset(edge: .bottom) {
                HStack {
                    Label(typeDescription, systemImage: previewSymbol)
                    Spacer()
                    Text(ByteCountFormatter.string(
                        fromByteCount: attachment.byteCount,
                        countStyle: .file
                    ))
                }
                .font(CagenticTheme.FontStyle.caption)
                .foregroundStyle(CagenticTheme.textSecondary)
                .padding(.horizontal, CagenticTheme.Spacing.md)
                .frame(minHeight: 44)
                .background(.ultraThinMaterial)
            }
            .task {
                await loadPreview()
            }
        }
    }

    private func loadPreview() async {
        defer { isLoading = false }
        guard let loadPayload else {
            errorMessage = "The attachment payload is unavailable."
            return
        }
        do {
            let data = try await loadPayload()
            try Task.checkCancellation()
            if attachment.kind == .photo {
                image = await AttachmentImageDecoder.shared.image(
                    from: data,
                    maximumPixelSize: 1_600
                ) ?? thumbnail
            } else if let text = String(data: data, encoding: .utf8) {
                textPreview = String(text.prefix(40_000))
            } else {
                errorMessage = "The extracted document text is not valid UTF-8."
            }
        } catch is CancellationError {
            return
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private var previewSymbol: String {
        switch attachment.kind {
        case .photo: "photo"
        case .pdf: "doc.richtext"
        case .textFile: "doc.text"
        }
    }

    private var typeDescription: String {
        switch attachment.kind {
        case .photo:
            if let width = attachment.pixelWidth, let height = attachment.pixelHeight {
                return "Photo · \(width)×\(height)"
            }
            return "Photo"
        case .pdf:
            return "PDF text preview"
        case .textFile:
            return "Text preview"
        }
    }
}

private actor AttachmentImageDecoder {
    static let shared = AttachmentImageDecoder()

    func image(from data: Data, maximumPixelSize: Int) -> UIImage? {
        let sourceOptions: [CFString: Any] = [kCGImageSourceShouldCache: false]
        guard let source = CGImageSourceCreateWithData(
            data as CFData,
            sourceOptions as CFDictionary
        ) else {
            return nil
        }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maximumPixelSize,
            kCGImageSourceShouldCacheImmediately: true,
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(
            source,
            0,
            options as CFDictionary
        ) else {
            return nil
        }
        return UIImage(cgImage: image)
    }
}

private struct SystemMessageRow: View {
    let message: ChatMessage

    var body: some View {
        Text(message.content)
            .font(CagenticTheme.FontStyle.footnote)
            .foregroundStyle(CagenticTheme.textSecondary)
            .multilineTextAlignment(.center)
            .padding(.horizontal, CagenticTheme.Spacing.md)
            .padding(.vertical, CagenticTheme.Spacing.xs)
            .frame(minHeight: 44)
            .background(CagenticTheme.surfaceRaised, in: .capsule)
            .frame(maxWidth: .infinity)
            .accessibilityLabel("System message")
            .accessibilityValue(message.content)
    }
}

private struct AssistantMark: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .body) private var markSize = 24.0
    @State private var isBreathing = false

    let isStreaming: Bool

    var body: some View {
        BrandMark(size: min(markSize, 34))
            .scaleEffect(isBreathing ? 0.9 : 1)
            .opacity(isBreathing ? 0.58 : 1)
            .animation(
                isStreaming && !reduceMotion
                    ? .easeInOut(duration: 1.05).repeatForever(autoreverses: true)
                    : nil,
                value: isBreathing
            )
            .onChange(of: isStreaming, initial: true) {
                updateAnimationState()
            }
            .onChange(of: reduceMotion) {
                updateAnimationState()
            }
    }

    private func updateAnimationState() {
        isBreathing = isStreaming && !reduceMotion
    }
}

private struct ThinkingDisclosure: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isExpanded = false

    let thinking: String
    let isStreaming: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
            Button {
                withAnimation(reduceMotion ? nil : .smooth(duration: 0.26)) {
                    isExpanded.toggle()
                }
            } label: {
                HStack(spacing: CagenticTheme.Spacing.xxs) {
                    if isStreaming {
                        ProgressView()
                            .controlSize(.mini)
                            .accessibilityHidden(true)
                    } else {
                        Image(systemName: "sparkles")
                            .font(.caption2)
                            .accessibilityHidden(true)
                    }

                    Text(isStreaming ? "Thinking" : "Thoughts")
                        .font(CagenticTheme.FontStyle.captionMedium)

                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                        .accessibilityHidden(true)
                }
                .foregroundStyle(
                    isExpanded ? CagenticTheme.textSecondary : CagenticTheme.textTertiary
                )
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(isStreaming ? "Thinking" : "Thoughts")
            .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
            .accessibilityHint("Shows or hides the model's thinking text")

            if isExpanded {
                HStack(alignment: .top, spacing: CagenticTheme.Spacing.sm) {
                    Capsule()
                        .fill(CagenticTheme.accent.opacity(isStreaming ? 0.72 : 0.52))
                        .frame(width: 2)
                        .frame(maxHeight: .infinity)
                        .accessibilityHidden(true)

                    MarkdownText(thinking)
                        .font(CagenticTheme.FontStyle.callout)
                        .foregroundStyle(CagenticTheme.textSecondary)
                        .opacity(0.82)
                }
                .padding(.leading, CagenticTheme.Spacing.xxs)
                .padding(.bottom, CagenticTheme.Spacing.xxs)
                .transition(.opacity)
            }
        }
        .transaction { transaction in
            if reduceMotion {
                transaction.animation = nil
            }
        }
    }
}


/// The steps an agentic backend took on the way to its answer.
///
/// Rendered in the order they happened, because that order is the explanation: the model says what
/// it is about to do, does it, and reacts to the result. A flat list of tool names would lose that.
private struct AssistantActivityTimeline: View {
    let activity: [AssistantActivity]
    let isStreaming: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
            ForEach(activity) { item in
                switch item.kind {
                case .narration:
                    MarkdownText(item.text)
                        .font(CagenticTheme.FontStyle.callout)
                        .foregroundStyle(CagenticTheme.textSecondary)
                case .plan:
                    PlanBlock(steps: item.steps)
                case .tool:
                    ToolActivityRow(activity: item)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(isStreaming ? "Work in progress" : "Work performed")
    }
}

private struct PlanBlock: View {
    let steps: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
            Text("Plan")
                .font(CagenticTheme.FontStyle.captionSemibold)
                .foregroundStyle(CagenticTheme.textTertiary)
                .textCase(.uppercase)

            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .firstTextBaseline, spacing: CagenticTheme.Spacing.xs) {
                    Text("\(index + 1).")
                        .font(CagenticTheme.FontStyle.caption)
                        .foregroundStyle(CagenticTheme.textTertiary)
                        .monospacedDigit()
                    Text(step)
                        .font(CagenticTheme.FontStyle.callout)
                        .foregroundStyle(CagenticTheme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(CagenticTheme.Spacing.sm)
        .background(CagenticTheme.surfaceRaised)
        .clipShape(.rect(cornerRadius: CagenticTheme.Radius.card))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Plan with \(steps.count) steps")
    }
}

/// One tool call: what ran, and one line of what came back.
///
/// Collapsed by default and expandable, on the same chassis as the thinking disclosure — a turn can
/// contain a dozen of these and they must not crowd out the answer.
private struct ToolActivityRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isExpanded = false

    let activity: AssistantActivity

    var body: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
            Button {
                guard canExpand else { return }
                withAnimation(reduceMotion ? nil : .smooth(duration: 0.26)) {
                    isExpanded.toggle()
                }
            } label: {
                HStack(spacing: CagenticTheme.Spacing.xs) {
                    stateMark

                    Text(activity.toolName)
                        .font(CagenticTheme.FontStyle.captionSemibold)
                        .foregroundStyle(CagenticTheme.textSecondary)

                    if !activity.text.isEmpty {
                        Text(activity.text)
                            .font(CagenticTheme.FontStyle.caption)
                            .foregroundStyle(CagenticTheme.textTertiary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }

                    Spacer(minLength: 0)

                    if canExpand {
                        Image(systemName: "chevron.right")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(CagenticTheme.textTertiary)
                            .rotationEffect(.degrees(isExpanded ? 90 : 0))
                            .accessibilityHidden(true)
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .disabled(!canExpand)
            .accessibilityLabel(accessibilityLabel)
            .accessibilityValue(canExpand ? (isExpanded ? "Expanded" : "Collapsed") : "")

            if isExpanded, canExpand {
                HStack(alignment: .top, spacing: CagenticTheme.Spacing.sm) {
                    Capsule()
                        .fill(railColor)
                        .frame(width: 2)
                        .frame(maxHeight: .infinity)
                        .accessibilityHidden(true)

                    Text(activity.resultLine)
                        .font(CagenticTheme.FontStyle.caption)
                        .foregroundStyle(CagenticTheme.textSecondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.leading, CagenticTheme.Spacing.xxs)
                .padding(.bottom, CagenticTheme.Spacing.xxs)
                .transition(.opacity)
            }
        }
        .transaction { transaction in
            if reduceMotion {
                transaction.animation = nil
            }
        }
    }

    @ViewBuilder
    private var stateMark: some View {
        switch activity.toolState {
        case .running:
            ProgressView()
                .controlSize(.mini)
                .accessibilityHidden(true)
        case .succeeded:
            Image(systemName: "checkmark")
                .font(.caption2.weight(.bold))
                .foregroundStyle(CagenticTheme.success)
                .accessibilityHidden(true)
        case .failed:
            Image(systemName: "xmark")
                .font(.caption2.weight(.bold))
                .foregroundStyle(CagenticTheme.error)
                .accessibilityHidden(true)
        }
    }

    private var railColor: Color {
        activity.toolState == .failed ? CagenticTheme.error : CagenticTheme.border
    }

    private var canExpand: Bool {
        !activity.resultLine.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var accessibilityLabel: String {
        let outcome = switch activity.toolState {
        case .running: "running"
        case .succeeded: "finished"
        case .failed: "failed"
        }
        return [activity.toolName, activity.text, outcome]
            .filter { !$0.isEmpty }
            .joined(separator: ", ")
    }
}

/// What the assistant shows before the first token lands.
///
/// The verb and the three bobbing dots are the same ones the web UI uses, so the wait reads the
/// same wherever Cagentic is being driven from. The verb is chosen once per appearance rather than
/// per redraw — it would otherwise reshuffle on every layout pass.
private struct StreamingPlaceholder: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var verb = StreamingPlaceholder.verbs.randomElement() ?? "Thinking"

    fileprivate static let verbs = ["Thinking", "Reading context", "Working", "Reviewing"]

    var body: some View {
        HStack(spacing: CagenticTheme.Spacing.xs) {
            Text(verb)
                .font(CagenticTheme.FontStyle.body)
                .foregroundStyle(CagenticTheme.textSecondary)

            BobbingDots()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .frame(minHeight: 44)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(verb)…")
    }
}

/// Three dots that rise in sequence — the native counterpart of the web UI's `bob` keyframes.
private struct BobbingDots: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isAnimating = false

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0 ..< 3, id: \.self) { index in
                Circle()
                    .fill(CagenticTheme.accent)
                    .frame(width: 6, height: 6)
                    .opacity(isAnimating ? 1 : 0.15)
                    .offset(y: isAnimating ? -4 : 0)
                    .animation(
                        reduceMotion
                            ? nil
                            : .easeInOut(duration: 0.5)
                                .repeatForever(autoreverses: true)
                                .delay(Double(index) * 0.18),
                        value: isAnimating
                    )
            }
        }
        .accessibilityHidden(true)
        .onAppear {
            guard !reduceMotion else { return }
            isAnimating = true
        }
    }
}

private struct MessageStateView: View {
    let role: ChatRole
    let state: MessageState
    let errorDescription: String?

    var body: some View {
        Group {
            switch state {
            case .complete:
                EmptyView()

            case .streaming:
                // Nothing. Text arriving on screen already says the model is working, and the mark
                // beside it keeps breathing until the turn ends — a second spinner captioned
                // "Generating" underneath was chrome restating what the reader can see.
                EmptyView()

            case .cancelled:
                VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
                    Label(cancelledTitle, systemImage: "stop.circle")
                        .font(CagenticTheme.FontStyle.caption)
                        .foregroundStyle(CagenticTheme.warning)

                    if let cancellationDetail {
                        Text(cancellationDetail)
                            .font(CagenticTheme.FontStyle.caption)
                            .foregroundStyle(CagenticTheme.textSecondary)
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)

            case .failed:
                HStack(alignment: .top, spacing: CagenticTheme.Spacing.sm) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .foregroundStyle(CagenticTheme.error)
                        .padding(.top, 2)
                        .accessibilityHidden(true)

                    VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xxs) {
                        Text(role == .user ? "Message failed" : "Response failed")
                            .font(CagenticTheme.FontStyle.subheadlineSemibold)
                            .foregroundStyle(CagenticTheme.error)

                        if let errorDescription, !errorDescription.isEmpty {
                            Text(errorDescription)
                                .font(CagenticTheme.FontStyle.caption)
                                .foregroundStyle(CagenticTheme.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }

                }
                .padding(.leading, CagenticTheme.Spacing.sm)
                .padding(.trailing, CagenticTheme.Spacing.xs)
                .padding(.vertical, CagenticTheme.Spacing.xxs)
                .background(CagenticTheme.error.opacity(0.08))
                .compositingGroup()
                .clipShape(.rect(cornerRadius: CagenticTheme.Radius.control))
                .accessibilityElement(children: .contain)
            }
        }
    }

    private var cancelledTitle: String {
        cancellationDetail == nil ? "Generation stopped" : "Response interrupted"
    }

    private var cancellationDetail: String? {
        guard let errorDescription = errorDescription?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !errorDescription.isEmpty,
            errorDescription.caseInsensitiveCompare("Stopped") != .orderedSame
        else {
            return nil
        }
        return errorDescription
    }
}

private struct GenerationMetricsLabel: View {
    let metrics: GenerationMetrics

    var body: some View {
        if let rate = metrics.tokensPerSecond {
            Text(metadataText(rate: rate))
                .font(CagenticTheme.FontStyle.metadata)
                .foregroundStyle(CagenticTheme.textTertiary)
                .accessibilityLabel(metadataAccessibilityLabel(rate: rate))
        }
    }

    private func metadataText(rate: Double) -> String {
        let rateText = rate.formatted(.number.precision(.fractionLength(1)))
        if let tokenCount = metrics.responseTokenCount {
            return "\(tokenCount) tokens · \(rateText) tok/s"
        }
        return "\(rateText) tok/s"
    }

    private func metadataAccessibilityLabel(rate: Double) -> String {
        let rateText = rate.formatted(.number.precision(.fractionLength(1)))
        if let tokenCount = metrics.responseTokenCount {
            return "\(tokenCount) response tokens, \(rateText) tokens per second"
        }
        return "\(rateText) tokens per second"
    }
}

private enum MessageRowPreviewData {
    static let date = Date(timeIntervalSince1970: 1_725_000_000)

    static let user = ChatMessage(
        id: UUID(uuidString: "10000000-0000-0000-0000-000000000001")!,
        role: .user,
        content: "Compare a direct Ollama connection with a secure reverse proxy.",
        createdAt: date
    )

    static let assistant = ChatMessage(
        id: UUID(uuidString: "20000000-0000-0000-0000-000000000002")!,
        role: .assistant,
        content: """
        ## Start on your trusted network

        A direct connection is the simplest path. Use a proxy when you need
        **authentication** or remote access.

        > Never expose an unauthenticated Ollama server to the public internet.

        1. Bind Ollama to the PC's LAN interface.
        2. Allow port `11434` through the private firewall.

        ```swift
        let endpoint = URL(string: "http://192.168.1.42:11434/api/chat")!
        ```
        """,
        createdAt: date,
        metrics: GenerationMetrics(
            promptTokenCount: 87,
            responseTokenCount: 142,
            totalDurationNanoseconds: 7_200_000_000,
            evaluationDurationNanoseconds: 5_800_000_000
        )
    )

    static let streaming = ChatMessage(
        id: UUID(uuidString: "30000000-0000-0000-0000-000000000003")!,
        role: .assistant,
        content: "Start by confirming that the phone and PC are on the same network.",
        thinking: "I am checking the likely connection failures before recommending a setup.",
        createdAt: date,
        state: .streaming
    )

    static let failed = ChatMessage(
        id: UUID(uuidString: "40000000-0000-0000-0000-000000000004")!,
        role: .assistant,
        content: "The request reached the PC, but generation stopped before completion.",
        createdAt: date,
        state: .failed,
        errorDescription: "The model ran out of memory. Try a smaller model or context window."
    )
}

private struct MessageRowPreviewCanvas: View {
    let message: ChatMessage
    var modelName = "qwen3:8b"
    var showsActions = false

    var body: some View {
        ScrollView {
            MessageRow(
                message: message,
                modelName: modelName,
                onRegenerate: showsActions ? {} : nil,
                onEdit: showsActions ? {} : nil
            )
            .padding(CagenticTheme.Spacing.md)
        }
        .background(CagenticTheme.stage)
    }
}

#Preview("User") {
    MessageRowPreviewCanvas(message: MessageRowPreviewData.user)
}

#Preview("Assistant") {
    MessageRowPreviewCanvas(message: MessageRowPreviewData.assistant)
}

#Preview("Streaming") {
    MessageRowPreviewCanvas(message: MessageRowPreviewData.streaming, showsActions: true)
        .transaction { transaction in
            transaction.animation = nil
            transaction.disablesAnimations = true
        }
}

#Preview("Error") {
    MessageRowPreviewCanvas(message: MessageRowPreviewData.failed, showsActions: true)
}

#Preview("Dark") {
    MessageRowPreviewCanvas(message: MessageRowPreviewData.assistant)
        .preferredColorScheme(.dark)
}
