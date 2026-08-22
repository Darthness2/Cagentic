import Foundation

nonisolated enum ChatRole: String, Codable, Hashable, Sendable {
    case system
    case user
    case assistant
}

nonisolated enum MessageState: String, Codable, Hashable, Sendable {
    case complete
    case streaming
    case failed
    case cancelled
}

nonisolated struct GenerationMetrics: Codable, Equatable, Hashable, Sendable {
    var promptTokenCount: Int?
    var responseTokenCount: Int?
    var totalDurationNanoseconds: Int64?
    var evaluationDurationNanoseconds: Int64?

    var tokensPerSecond: Double? {
        guard
            let responseTokenCount,
            let evaluationDurationNanoseconds,
            evaluationDurationNanoseconds > 0
        else {
            return nil
        }

        return Double(responseTokenCount) / (Double(evaluationDurationNanoseconds) / 1_000_000_000)
    }
}


/// One step of an assistant turn that is not the answer itself.
///
/// A gateway turn is a story: the model narrates, runs a tool, narrates again, then answers.
/// Collapsing that into a single text blob loses the part that explains *why* the answer says what
/// it does, so the ordered timeline is kept on the message. Stored flat with a `kind` discriminator
/// because the snapshot decoder has to tolerate items written by a newer build.
nonisolated struct AssistantActivity: Identifiable, Codable, Equatable, Hashable, Sendable {
    nonisolated enum Kind: String, Codable, Equatable, Hashable, Sendable {
        /// Text the model produced before handing off to a tool.
        case narration
        case plan
        case tool

        init(from decoder: any Decoder) throws {
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            self = Kind(rawValue: raw) ?? .narration
        }
    }

    nonisolated enum ToolState: String, Codable, Equatable, Hashable, Sendable {
        case running
        case succeeded
        case failed

        init(from decoder: any Decoder) throws {
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            self = ToolState(rawValue: raw) ?? .running
        }
    }

    let id: UUID
    var kind: Kind
    /// Narration text, or a tool call's one-line argument summary.
    var text: String
    var steps: [String]
    var toolName: String
    /// The gateway's own call identifier, used to match a result to its call.
    var toolCallID: String
    var toolState: ToolState
    /// The first line of the tool's output, which is all the transcript shows.
    var resultLine: String

    init(
        id: UUID = UUID(),
        kind: Kind,
        text: String = "",
        steps: [String] = [],
        toolName: String = "",
        toolCallID: String = "",
        toolState: ToolState = .running,
        resultLine: String = ""
    ) {
        self.id = id
        self.kind = kind
        self.text = text
        self.steps = steps
        self.toolName = toolName
        self.toolCallID = toolCallID
        self.toolState = toolState
        self.resultLine = resultLine
    }

    static func narration(_ text: String) -> AssistantActivity {
        AssistantActivity(kind: .narration, text: text)
    }

    static func plan(_ steps: [String]) -> AssistantActivity {
        AssistantActivity(kind: .plan, steps: steps)
    }

    static func tool(id: String, name: String, summary: String) -> AssistantActivity {
        AssistantActivity(kind: .tool, text: summary, toolName: name, toolCallID: id)
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case kind
        case text
        case steps
        case toolName
        case toolCallID
        case toolState
        case resultLine
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(UUID.self, forKey: .id) ?? UUID()
        kind = try container.decodeIfPresent(Kind.self, forKey: .kind) ?? .narration
        text = try container.decodeIfPresent(String.self, forKey: .text) ?? ""
        steps = try container.decodeIfPresent([String].self, forKey: .steps) ?? []
        toolName = try container.decodeIfPresent(String.self, forKey: .toolName) ?? ""
        toolCallID = try container.decodeIfPresent(String.self, forKey: .toolCallID) ?? ""
        toolState = try container.decodeIfPresent(ToolState.self, forKey: .toolState) ?? .running
        resultLine = try container.decodeIfPresent(String.self, forKey: .resultLine) ?? ""
    }
}

nonisolated struct ChatMessage: Identifiable, Codable, Equatable, Hashable, Sendable {
    let id: UUID
    var role: ChatRole
    var modelName: String?
    var content: String
    var thinking: String
    var createdAt: Date
    var state: MessageState
    var metrics: GenerationMetrics?
    var errorDescription: String?
    var attachments: [AttachmentMetadata]
    /// Tool calls, plans, and intermediate narration produced by an agentic backend.
    var activity: [AssistantActivity]

    init(
        id: UUID = UUID(),
        role: ChatRole,
        modelName: String? = nil,
        content: String,
        thinking: String = "",
        createdAt: Date = .now,
        state: MessageState = .complete,
        metrics: GenerationMetrics? = nil,
        errorDescription: String? = nil,
        attachments: [AttachmentMetadata] = [],
        activity: [AssistantActivity] = []
    ) {
        self.id = id
        self.role = role
        self.modelName = modelName
        self.content = content
        self.thinking = thinking
        self.createdAt = createdAt
        self.state = state
        self.metrics = metrics
        self.errorDescription = errorDescription
        self.attachments = attachments
        self.activity = activity
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case role
        case modelName
        case content
        case thinking
        case createdAt
        case state
        case metrics
        case errorDescription
        case attachments
        case activity
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        role = try container.decode(ChatRole.self, forKey: .role)
        modelName = try container.decodeIfPresent(String.self, forKey: .modelName)
        content = try container.decodeIfPresent(String.self, forKey: .content) ?? ""
        thinking = try container.decodeIfPresent(String.self, forKey: .thinking) ?? ""
        createdAt = try container.decodeIfPresent(Date.self, forKey: .createdAt) ?? .now
        state = try container.decodeIfPresent(MessageState.self, forKey: .state) ?? .complete
        metrics = try container.decodeIfPresent(GenerationMetrics.self, forKey: .metrics)
        errorDescription = try container.decodeIfPresent(String.self, forKey: .errorDescription)
        attachments = try container.decodeIfPresent(
            [AttachmentMetadata].self,
            forKey: .attachments
        ) ?? []
        activity = try container.decodeIfPresent([AssistantActivity].self, forKey: .activity) ?? []
    }
}

nonisolated struct Conversation: Identifiable, Codable, Equatable, Hashable, Sendable {
    let id: UUID
    var title: String
    var modelName: String
    var createdAt: Date
    var updatedAt: Date
    var isPinned: Bool
    var isArchived: Bool
    var branchedFromConversationID: UUID?
    var branchedFromMessageID: UUID?
    /// The server this conversation belongs to. Nil means a local Ollama chat, which is what every
    /// conversation written before multi-backend support was.
    var serverProfileID: ServerProfileID?
    /// The gateway's own chat id when this conversation mirrors one. Nil for locally owned chats.
    var remoteID: String?
    var messages: [ChatMessage]

    init(
        id: UUID = UUID(),
        title: String = "New chat",
        modelName: String = "",
        createdAt: Date = .now,
        updatedAt: Date = .now,
        isPinned: Bool = false,
        isArchived: Bool = false,
        branchedFromConversationID: UUID? = nil,
        branchedFromMessageID: UUID? = nil,
        serverProfileID: ServerProfileID? = nil,
        remoteID: String? = nil,
        messages: [ChatMessage] = []
    ) {
        self.id = id
        self.title = title
        self.modelName = modelName
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.isPinned = isPinned
        self.isArchived = isArchived
        self.branchedFromConversationID = branchedFromConversationID
        self.branchedFromMessageID = branchedFromMessageID
        self.serverProfileID = serverProfileID
        self.remoteID = remoteID
        self.messages = messages
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case title
        case modelName
        case createdAt
        case updatedAt
        case isPinned
        case isArchived
        case branchedFromConversationID
        case branchedFromMessageID
        case serverProfileID
        case remoteID
        case messages
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        title = try container.decodeIfPresent(String.self, forKey: .title) ?? "New chat"
        modelName = try container.decodeIfPresent(String.self, forKey: .modelName) ?? ""
        createdAt = try container.decodeIfPresent(Date.self, forKey: .createdAt) ?? .now
        updatedAt = try container.decodeIfPresent(Date.self, forKey: .updatedAt) ?? createdAt
        isPinned = try container.decodeIfPresent(Bool.self, forKey: .isPinned) ?? false
        isArchived = try container.decodeIfPresent(Bool.self, forKey: .isArchived) ?? false
        branchedFromConversationID = try container.decodeIfPresent(
            UUID.self,
            forKey: .branchedFromConversationID
        )
        branchedFromMessageID = try container.decodeIfPresent(
            UUID.self,
            forKey: .branchedFromMessageID
        )
        serverProfileID = try container.decodeIfPresent(
            ServerProfileID.self,
            forKey: .serverProfileID
        )
        remoteID = try container.decodeIfPresent(String.self, forKey: .remoteID)
        messages = try container.decodeIfPresent([ChatMessage].self, forKey: .messages) ?? []
    }

    /// True when the gateway owns this history and the app only displays a copy.
    var isRemoteMirror: Bool {
        remoteID != nil
    }

    var preview: String {
        guard let content = messages.last(where: {
            !$0.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        })?.content else {
            return "Start a conversation"
        }
        return String(content.prefix(280))
    }
}

nonisolated struct ConversationSummary: Identifiable, Equatable, Hashable, Sendable {
    let id: UUID
    let title: String
    let preview: String
    let updatedAt: Date
    let isPinned: Bool
    let isArchived: Bool
    let isBranch: Bool

    init(conversation: Conversation) {
        id = conversation.id
        title = conversation.title
        preview = conversation.preview
        updatedAt = conversation.updatedAt
        isPinned = conversation.isPinned
        isArchived = conversation.isArchived
        isBranch = conversation.branchedFromConversationID != nil
    }
}

nonisolated enum AppearancePreference: String, Codable, CaseIterable, Identifiable, Sendable {
    case system
    case light
    case dark

    var id: Self { self }

    var title: String {
        switch self {
        case .system: "System"
        case .light: "Light"
        case .dark: "Dark"
        }
    }
}

nonisolated struct GenerationOptions: Codable, Equatable, Hashable, Sendable {
    var temperature = 0.7
    var topP = 0.9
    var contextLength = 8_192
    var seed: Int?
    var keepAlive = "30m"
    var enableThinking = true
}

nonisolated struct AppSettings: Codable, Equatable, Hashable, Sendable {
    var serverURL: String
    var serverName: String
    var selectedModel: String
    var systemPrompt: String
    var generation: GenerationOptions
    var appearance: AppearancePreference
    var hapticsEnabled: Bool
    var hasCompletedOnboarding: Bool
    var serverCatalog: ServerProfileCatalog

    init(
        serverURL: String = "",
        serverName: String = "My Ollama",
        selectedModel: String = "",
        systemPrompt: String = "You are a thoughtful, precise assistant.",
        generation: GenerationOptions = GenerationOptions(),
        appearance: AppearancePreference = .system,
        hapticsEnabled: Bool = true,
        hasCompletedOnboarding: Bool = false,
        serverCatalog: ServerProfileCatalog = ServerProfileCatalog()
    ) {
        self.serverURL = serverURL
        self.serverName = serverName
        self.selectedModel = selectedModel
        self.systemPrompt = systemPrompt
        self.generation = generation
        self.appearance = appearance
        self.hapticsEnabled = hapticsEnabled
        self.hasCompletedOnboarding = hasCompletedOnboarding
        self.serverCatalog = serverCatalog
    }

    private enum CodingKeys: String, CodingKey {
        case serverURL
        case serverName
        case selectedModel
        case systemPrompt
        case generation
        case appearance
        case hapticsEnabled
        case hasCompletedOnboarding
        case serverCatalog
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        serverURL = try container.decodeIfPresent(String.self, forKey: .serverURL) ?? ""
        serverName = try container.decodeIfPresent(String.self, forKey: .serverName) ?? "My Ollama"
        selectedModel = try container.decodeIfPresent(String.self, forKey: .selectedModel) ?? ""
        systemPrompt = try container.decodeIfPresent(String.self, forKey: .systemPrompt)
            ?? "You are a thoughtful, precise assistant."
        generation = try container.decodeIfPresent(GenerationOptions.self, forKey: .generation)
            ?? GenerationOptions()
        appearance = try container.decodeIfPresent(AppearancePreference.self, forKey: .appearance)
            ?? .system
        hapticsEnabled = try container.decodeIfPresent(Bool.self, forKey: .hapticsEnabled) ?? true
        hasCompletedOnboarding = try container.decodeIfPresent(
            Bool.self,
            forKey: .hasCompletedOnboarding
        ) ?? false
        serverCatalog = try container.decodeIfPresent(
            ServerProfileCatalog.self,
            forKey: .serverCatalog
        ) ?? ServerProfileCatalog()
    }
}

nonisolated struct ConversationDraft: Codable, Equatable, Hashable, Sendable {
    var text: String
    var attachments: [AttachmentMetadata]

    init(text: String = "", attachments: [AttachmentMetadata] = []) {
        self.text = text
        self.attachments = attachments
    }

    var isEmpty: Bool {
        text.isEmpty && attachments.isEmpty
    }
}

nonisolated struct AppSnapshot: Codable, Equatable, Sendable {
    var settings = AppSettings()
    var conversations: [Conversation] = []
    var selectedConversationID: UUID?
    var conversationDrafts: [UUID: ConversationDraft] = [:]

    private enum CodingKeys: String, CodingKey {
        case settings
        case conversations
        case selectedConversationID
        case conversationDrafts
    }

    init(
        settings: AppSettings = AppSettings(),
        conversations: [Conversation] = [],
        selectedConversationID: UUID? = nil,
        conversationDrafts: [UUID: ConversationDraft] = [:]
    ) {
        self.settings = settings
        self.conversations = conversations
        self.selectedConversationID = selectedConversationID
        self.conversationDrafts = conversationDrafts
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        settings = try container.decodeIfPresent(AppSettings.self, forKey: .settings)
            ?? AppSettings()
        conversations = try container.decodeIfPresent(
            [Conversation].self,
            forKey: .conversations
        ) ?? []
        selectedConversationID = try container.decodeIfPresent(
            UUID.self,
            forKey: .selectedConversationID
        )
        conversationDrafts = try container.decodeIfPresent(
            [UUID: ConversationDraft].self,
            forKey: .conversationDrafts
        ) ?? [:]
    }
}

nonisolated struct AvailableModel: Identifiable, Equatable, Hashable, Sendable {
    var id: String { name }
    var name: String
    var family: String
    var parameterSize: String
    var quantization: String
    var sizeBytes: Int64
    var capabilities: Set<String>
    var contextLength: Int? = nil
    var metadataLoaded = false

    /// A model as reported by the Cagentic gateway.
    ///
    /// The gateway hands back only a model spec — `qwen2.5:7b`, `anthropic:claude-…` — with no size,
    /// family, or capability metadata, and it has no equivalent of Ollama's `/api/show`. Those
    /// fields stay empty rather than being invented, and the model is marked as fully loaded so
    /// nothing tries to fetch detail that does not exist.
    static func gatewayModel(named name: String) -> AvailableModel {
        AvailableModel(
            name: name,
            family: "",
            parameterSize: "",
            quantization: "",
            sizeBytes: 0,
            capabilities: [],
            contextLength: nil,
            metadataLoaded: true
        )
    }

    var shortName: String {
        name.replacingOccurrences(of: ":latest", with: "")
    }

    var sizeDescription: String {
        ByteCountFormatter.string(fromByteCount: sizeBytes, countStyle: .file)
    }

    var metadataDescription: String {
        [family, parameterSize, quantization, sizeBytes > 0 ? sizeDescription : ""]
            .filter { !$0.isEmpty }
            .joined(separator: " · ")
    }

    var sortedCapabilities: [String] {
        capabilities.sorted { $0.localizedStandardCompare($1) == .orderedAscending }
    }
}

nonisolated enum ConnectionState: Equatable, Sendable {
    case notConfigured
    case connecting
    case connected(version: String)
    case failed(message: String)

    var isConnected: Bool {
        if case .connected = self {
            return true
        }
        return false
    }

    var title: String {
        switch self {
        case .notConfigured: "Set up Ollama"
        case .connecting: "Connecting"
        case .connected: "Connected"
        case .failed: "Connection issue"
        }
    }
}

enum AppSheet: Identifiable, Equatable {
    case connection
    case serverManager
    case settings
    case modelLibrary
    case conversationManager
    case renameConversation(UUID)

    var id: String {
        switch self {
        case .connection: "connection"
        case .serverManager: "server-manager"
        case .settings: "settings"
        case .modelLibrary: "model-library"
        case .conversationManager: "conversation-manager"
        case .renameConversation(let id): "rename-\(id.uuidString)"
        }
    }
}

struct AppNotice: Identifiable, Equatable {
    let id = UUID()
    var title: String
    var message: String
}

extension Conversation {
    static let previewConversation = Conversation(
        title: "Plan a local-first workspace",
        modelName: "llama3.2:latest",
        messages: [
            ChatMessage(role: .user, content: "Help me plan a focused writing workspace."),
            ChatMessage(
                role: .assistant,
                content: "Start with a quiet surface and keep only the tools you reach for every day.\n\n- Put the editor at the center.\n- Move reference material one gesture away.\n- Let automation stay invisible until it is useful.",
                thinking: "The user wants a focused workspace, so the answer should prioritize a small number of practical, low-friction changes.",
                metrics: GenerationMetrics(
                    promptTokenCount: 42,
                    responseTokenCount: 58,
                    totalDurationNanoseconds: 2_800_000_000,
                    evaluationDurationNanoseconds: 2_400_000_000
                )
            ),
        ]
    )
}
