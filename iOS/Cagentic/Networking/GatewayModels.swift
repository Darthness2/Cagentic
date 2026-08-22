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
    case notice(level: GatewayNoticeLevel, text: String)
    case compacted(before: Int, after: Int)
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

    public init(id: String, name: String, summary: String) {
        self.id = id
        self.name = name
        self.summary = summary
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

    public init(id: String, title: String, model: String, messages: [GatewayDisplayMessage]) {
        self.id = id
        self.title = title
        self.model = model
        self.messages = messages
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
    public let summary: String
    /// Tri-state: `nil` means the gateway found no matching result, which happens when a turn was
    /// aborted between the call and its result.
    public let isSuccess: Bool?
    public let firstLine: String

    public init(name: String, summary: String, isSuccess: Bool?, firstLine: String) {
        self.name = name
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
    /// Several gateway routes report failure as an `error` key nested inside an HTTP 200 body.
    let error: String?

    var value: GatewayChatDetail? {
        guard let id, error == nil else { return nil }
        return GatewayChatDetail(
            id: id,
            title: GatewayChatSummaryPayload.displayTitle(title),
            model: model ?? "",
            messages: (messages ?? []).compactMap(\.value)
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
    let summary: String?
    let ok: Bool?
    let firstLine: String?

    enum CodingKeys: String, CodingKey {
        case name
        case summary
        case ok
        case firstLine = "first_line"
    }

    var value: GatewayToolDetail {
        GatewayToolDetail(
            name: name ?? "tool",
            summary: summary ?? "",
            isSuccess: ok,
            firstLine: firstLine ?? ""
        )
    }
}

nonisolated struct GatewayChatsPayload: Decodable, Sendable {
    let chats: [GatewayChatSummaryPayload]?
    let current: GatewayChatDetailPayload?
    let error: String?
}

nonisolated struct GatewayErrorPayload: Decodable, Sendable {
    let error: String?
}
