import Foundation

// MARK: - Streaming events

/// One decoded frame from the gateway's `/api/chat` SSE stream.
///
/// The gateway forwards its engine's event vocabulary verbatim, which is much richer than Ollama's
/// three-case stream: a single turn interleaves narration, tool calls, and approval requests. Only
/// the kinds the app renders are represented here; the rest decode to no events at all.
public nonisolated enum GatewayEvent: Equatable, Sendable {
    /// An incremental chunk of the current answer segment. Append.
    case contentDelta(String)
    /// The complete text of the current answer segment. Replaces whatever the deltas accumulated —
    /// the engine emits this at the end of each model round, not as an addition to it.
    case contentReplace(String)
    /// One complete reasoning block. Only arrives when the gateway's engine has streaming off;
    /// otherwise reasoning is inline in `contentDelta` wrapped in `<think>` tags.
    case thinkingBlock(String)
    case plan([String])
    case toolCall(GatewayToolCall)
    case toolOutcome(GatewayToolOutcome)
    case permissionRequest(GatewayPermissionRequest)
    case question(GatewayQuestion)
    case notice(level: GatewayNoticeLevel, text: String)
    case compacted(before: Int, after: Int)
    /// How full the model's window is, measured before the turn is sent.
    case contextUsage(used: Int, limit: Int)
    /// The turn's final summary. Does *not* end the stream — `ended` always follows.
    case completed(GatewayTurnSummary)
    /// The terminating frame. Its absence means the connection dropped mid-turn.
    case ended
}

public nonisolated enum GatewayNoticeLevel: Equatable, Sendable {
    case info
    case warning
}

public nonisolated struct GatewayToolCall: Equatable, Sendable, Identifiable {
    public let id: String
    public let name: String
    public let summary: String
    /// Human phrasing supplied by the gateway, so all three front ends word it alike.
    public let label: String
    /// The same phrase for a collapsed run, carrying an `{n}` slot.
    public let labelPlural: String

    public init(
        id: String,
        name: String,
        summary: String,
        label: String = "",
        labelPlural: String = ""
    ) {
        self.id = id
        self.name = name
        self.summary = summary
        self.label = label
        self.labelPlural = labelPlural
    }
}

/// The result of a tool call, including the denial case — the gateway always follows a denial with a
/// matching failed result, so the app only has to model one outcome per call.
public nonisolated struct GatewayToolOutcome: Equatable, Sendable, Identifiable {
    public let id: String
    public let name: String
    public let isSuccess: Bool
    public let firstLine: String

    public init(id: String, name: String, isSuccess: Bool, firstLine: String) {
        self.id = id
        self.name = name
        self.isSuccess = isSuccess
        self.firstLine = firstLine
    }
}

/// An approval the gateway is blocking on. The turn's thread is parked until an answer arrives (or
/// five minutes elapse, after which the gateway answers itself with a denial).
public nonisolated struct GatewayPermissionRequest: Equatable, Sendable, Identifiable {
    public let id: String
    public let tool: String
    public let summary: String
    public let diff: String?
    /// A narrow standing-approval pattern the gateway offered, such as `run_bash(git status*)`.
    /// It must be echoed back byte-for-byte; the gateway drops any rule it did not offer.
    public let rule: String?
    /// How a shell command will be confined. Present only for `run_bash`/`bash_async`.
    public let sandbox: String?
    public let allowsNetwork: Bool

    public init(
        id: String,
        tool: String,
        summary: String,
        diff: String? = nil,
        rule: String? = nil,
        sandbox: String? = nil,
        allowsNetwork: Bool = false
    ) {
        self.id = id
        self.tool = tool
        self.summary = summary
        self.diff = diff
        self.rule = rule
        self.sandbox = sandbox
        self.allowsNetwork = allowsNetwork
    }
}

/// The answers `/api/permission` accepts. Anything else is coerced to a denial by the gateway.
/// The model asking *you* something.
///
/// Distinct from a permission request: it wants information rather than approval, and it can be
/// outstanding at the same time as one. The turn is parked on the answer either way.
public nonisolated struct GatewayQuestion: Equatable, Sendable, Identifiable {
    public let id: String
    public let question: String
    /// Choices the model offered. May be empty, and is never exhaustive — free text is always
    /// accepted, because a list the model wrote may not contain the real answer.
    public let options: [String]

    public init(id: String, question: String, options: [String]) {
        self.id = id
        self.question = question
        self.options = options
    }
}

public nonisolated enum GatewayPermissionAnswer: String, Equatable, Sendable {
    case allowOnce = "yes"
    case denyOnce = "no"
    /// Caches an allow for this tool name — process-wide, and shared with the terminal REPL.
    case allowAlways = "always"
    case denyAlways = "never"
    /// Installs the exact rule string the prompt offered, then allows this call.
    case allowRule = "rule"
}

public nonisolated struct GatewayTurnUsage: Equatable, Sendable {
    public let inputTokens: Int
    public let outputTokens: Int
    public let milliseconds: Int

    public init(inputTokens: Int, outputTokens: Int, milliseconds: Int) {
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.milliseconds = milliseconds
    }
}

public nonisolated struct GatewayTurnSummary: Equatable, Sendable {
    public let text: String
    /// This turn alone, not the session running total.
    public let usage: GatewayTurnUsage?

    public init(text: String, usage: GatewayTurnUsage?) {
        self.text = text
        self.usage = usage
    }
}

// MARK: - Bootstrap and stored chats

/// How much a chat asks before it acts.
///
/// Two of these are not new behaviour, only newly named: `restricted` is what the gateway has
/// always done — reading is silent, writing, shell and network ask — and `unrestricted` is its
/// yolo switch. `askAll` is the addition, for watching exactly what a model touches.
///
/// Per chat, not per computer: choosing `unrestricted` for a throwaway shell chat must not
/// disarm approvals for the terminal and web page on the same machine.
public nonisolated enum GatewayChatMode: String, CaseIterable, Equatable, Sendable, Identifiable {
    case restricted
    case askAll = "ask_all"
    case unrestricted

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .restricted: "Restricted"
        case .askAll: "Ask for everything"
        case .unrestricted: "Unrestricted"
        }
    }

    public var detail: String {
        switch self {
        case .restricted: "Asks before writing, running commands, or going online."
        case .askAll: "Asks before every tool, reading included."
        case .unrestricted: "Never asks. Standing deny rules still apply."
        }
    }

    public var symbol: String {
        switch self {
        case .restricted: "shield.lefthalf.filled"
        case .askAll: "hand.raised"
        case .unrestricted: "bolt"
        }
    }

    /// The gateway falls back to this for anything it does not recognise, and so does the app —
    /// an unreadable mode must never read as "no restrictions".
    public static let fallback = GatewayChatMode.restricted
}

/// The instructions and dials a chat is running under, as the gateway reports them.
public nonisolated struct GatewayChatConfig: Equatable, Sendable {
    public var mode: GatewayChatMode
    public var profileName: String
    public var systemPrompt: String
    public var temperature: Double?
    public var effort: String

    public init(
        mode: GatewayChatMode = .restricted,
        profileName: String = "",
        systemPrompt: String = "",
        temperature: Double? = nil,
        effort: String = ""
    ) {
        self.mode = mode
        self.profileName = profileName
        self.systemPrompt = systemPrompt
        self.temperature = temperature
        self.effort = effort
    }
}

nonisolated struct GatewayChatConfigPayload: Decodable, Sendable {
    struct Profile: Decodable, Sendable {
        let name: String?
        let systemPrompt: String?
        let temperature: Double?
        let effort: String?

        enum CodingKeys: String, CodingKey {
            case name
            case systemPrompt = "system_prompt"
            case temperature
            case effort
        }
    }

    let mode: String?
    let profile: Profile?
    let ok: Bool?
    let error: String?

    var value: GatewayChatConfig {
        GatewayChatConfig(
            mode: GatewayChatMode(rawValue: mode ?? "") ?? .fallback,
            profileName: profile?.name ?? "",
            systemPrompt: profile?.systemPrompt ?? "",
            temperature: profile?.temperature,
            effort: profile?.effort ?? ""
        )
    }
}

/// `GET /api/bootstrap` — the gateway's whole client-visible state in one response. It doubles as
/// the reachability probe, since a LAN device cannot fetch the gateway's HTML page.
public nonisolated struct GatewayBootstrap: Equatable, Sendable {
    public let version: String
    public let activeModel: String
    public let models: [String]
    public let chats: [GatewayChatSummary]
    public let current: GatewayChatDetail?

    public init(
        version: String,
        activeModel: String,
        models: [String],
        chats: [GatewayChatSummary],
        current: GatewayChatDetail?
    ) {
        self.version = version
        self.activeModel = activeModel
        self.models = models
        self.chats = chats
        self.current = current
    }
}

public nonisolated struct GatewayChatSummary: Equatable, Sendable, Identifiable {
    public let id: String
    public let title: String
    public let updatedAt: Date
    public let turns: Int

    public init(id: String, title: String, updatedAt: Date, turns: Int) {
        self.id = id
        self.title = title
        self.updatedAt = updatedAt
        self.turns = turns
    }
}

/// One chat's rendered history. The gateway renders stored messages for display rather than handing
/// over raw provider messages, so this is already stripped of system prompts and tool plumbing.
public nonisolated struct GatewayChatDetail: Equatable, Sendable, Identifiable {
    public let id: String
    public let title: String
    public let model: String
    public let messages: [GatewayDisplayMessage]
    /// What this chat is set to. The gateway owns it, so it is read back rather than remembered.
    public let config: GatewayChatConfig

    public init(
        id: String,
        title: String,
        model: String,
        messages: [GatewayDisplayMessage],
        config: GatewayChatConfig = GatewayChatConfig()
    ) {
        self.id = id
        self.title = title
        self.model = model
        self.messages = messages
        self.config = config
    }
}

public nonisolated struct GatewayDisplayMessage: Equatable, Sendable {
    public enum Role: String, Equatable, Sendable {
        case user
        case assistant
    }

    public let role: Role
    public let content: String
    public let tools: [GatewayToolDetail]

    public init(role: Role, content: String, tools: [GatewayToolDetail]) {
        self.role = role
        self.content = content
        self.tools = tools
    }
}

public nonisolated struct GatewayToolDetail: Equatable, Sendable {
    public let name: String
    public let label: String
    public let labelPlural: String
    public let summary: String
    /// Tri-state: `nil` means the gateway found no matching result, which happens when a turn was
    /// aborted between the call and its result.
    public let isSuccess: Bool?
    public let firstLine: String

    public init(
        name: String,
        summary: String,
        isSuccess: Bool?,
        firstLine: String,
        label: String = "",
        labelPlural: String = ""
    ) {
        self.name = name
        self.label = label
        self.labelPlural = labelPlural
        self.summary = summary
        self.isSuccess = isSuccess
        self.firstLine = firstLine
    }
}

// MARK: - Wire payloads

/// The gateway's JSON is snake_case and tolerant of missing keys; these mirror it exactly.
nonisolated struct GatewayBootstrapPayload: Decodable, Sendable {
    let version: String?
    let model: String?
    let models: [String]?
    let chats: [GatewayChatSummaryPayload]?
    let current: GatewayChatDetailPayload?

    var value: GatewayBootstrap {
        GatewayBootstrap(
            version: version ?? "",
            activeModel: model ?? "",
            models: models ?? [],
            chats: (chats ?? []).map(\.value),
            current: current?.value
        )
    }
}

nonisolated struct GatewayChatSummaryPayload: Decodable, Sendable {
    let id: String
    let title: String?
    let updatedAt: Double?
    let turns: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case updatedAt = "updated_at"
        case turns
    }

    var value: GatewayChatSummary {
        GatewayChatSummary(
            id: id,
            title: GatewayChatSummaryPayload.displayTitle(title),
            updatedAt: Date(timeIntervalSince1970: updatedAt ?? 0),
            turns: turns ?? 0
        )
    }

    /// The gateway already substitutes "New chat" for its own empty titles, but `sessions.json`
    /// written by older builds can still carry the literal "untitled".
    static func displayTitle(_ raw: String?) -> String {
        let trimmed = (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty || trimmed.lowercased() == "untitled" {
            return "New chat"
        }
        return trimmed
    }
}

nonisolated struct GatewayChatDetailPayload: Decodable, Sendable {
    let id: String?
    let title: String?
    let model: String?
    let messages: [GatewayDisplayMessagePayload]?
    let chatConfig: GatewayChatConfigPayload?
    /// Several gateway routes report failure as an `error` key nested inside an HTTP 200 body.
    let error: String?

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case model
        case messages
        case chatConfig = "chat_config"
        case error
    }

    var value: GatewayChatDetail? {
        guard let id, error == nil else { return nil }
        return GatewayChatDetail(
            id: id,
            title: GatewayChatSummaryPayload.displayTitle(title),
            model: model ?? "",
            messages: (messages ?? []).compactMap(\.value),
            config: chatConfig?.value ?? GatewayChatConfig()
        )
    }
}

nonisolated struct GatewayDisplayMessagePayload: Decodable, Sendable {
    let role: String?
    let content: String?
    let tools: [String]?
    let toolDetails: [GatewayToolDetailPayload]?

    enum CodingKeys: String, CodingKey {
        case role
        case content
        case tools
        case toolDetails = "tool_details"
    }

    var value: GatewayDisplayMessage? {
        guard let role, let mapped = GatewayDisplayMessage.Role(rawValue: role) else { return nil }
        // Prefer the rich per-call detail; fall back to the bare name list the same way the web
        // client does, so a chat saved by an older gateway still shows its tool activity.
        let details: [GatewayToolDetail]
        if let toolDetails, !toolDetails.isEmpty {
            details = toolDetails.map(\.value)
        } else {
            details = (tools ?? []).map {
                GatewayToolDetail(name: $0, summary: "", isSuccess: nil, firstLine: "")
            }
        }
        return GatewayDisplayMessage(
            role: mapped,
            content: content ?? "",
            tools: details
        )
    }
}

nonisolated struct GatewayToolDetailPayload: Decodable, Sendable {
    let name: String?
    let label: String?
    let labelPlural: String?
    let summary: String?
    let ok: Bool?
    let firstLine: String?

    enum CodingKeys: String, CodingKey {
        case name
        case label
        case labelPlural = "label_plural"
        case summary
        case ok
        case firstLine = "first_line"
    }

    var value: GatewayToolDetail {
        GatewayToolDetail(
            name: name ?? "tool",
            summary: summary ?? "",
            isSuccess: ok,
            firstLine: firstLine ?? "",
            label: label ?? "",
            labelPlural: labelPlural ?? ""
        )
    }
}

nonisolated struct GatewayChatsPayload: Decodable, Sendable {
    let chats: [GatewayChatSummaryPayload]?
    let current: GatewayChatDetailPayload?
    let error: String?
}

/// One file saved on the gateway's computer, ready to be referenced from a message.
///
/// The gateway stores uploads inside the workspace and hands back a workspace-relative path. That
/// path is the whole point: the engine's existing `@path` mention pipeline is what extracts text
/// from a PDF and inlines an image for a vision model, so an attachment from the phone travels the
/// same road as one typed in the terminal instead of needing a second implementation.
public nonisolated struct GatewayUpload: Equatable, Sendable {
    public let name: String
    public let path: String
    public let byteCount: Int
    /// The gateway's own classification — "image", "text", and so on.
    public let kind: String

    public init(name: String, path: String, byteCount: Int, kind: String) {
        self.name = name
        self.path = path
        self.byteCount = byteCount
        self.kind = kind
    }

    /// The mention that points the model at this file.
    ///
    /// A path containing a space has to be quoted or the gateway's mention regex stops at the
    /// first one and the model is handed half a filename.
    public var mention: String {
        path.contains(" ") ? "@\"\(path)\"" : "@\(path)"
    }
}

nonisolated struct GatewayUploadPayload: Decodable, Sendable {
    let ok: Bool?
    let name: String?
    let path: String?
    let size: Int?
    let kind: String?
    /// This route reports failure inside an HTTP 200 body as well as by status code.
    let error: String?

    var value: GatewayUpload? {
        guard error == nil, let path, !path.isEmpty else { return nil }
        return GatewayUpload(
            name: name ?? "",
            path: path,
            byteCount: size ?? 0,
            kind: kind ?? ""
        )
    }
}

nonisolated struct GatewayErrorPayload: Decodable, Sendable {
    let error: String?
}
