import Foundation

/// Incremental `text/event-stream` reader.
///
/// The gateway writes one `data: {json}` line per event followed by a blank line, but this parses
/// the general SSE shape — multiple `data:` fields concatenate with newlines, `:` comment lines and
/// unknown field names are ignored — so a future heartbeat frame cannot break the client. Bounded
/// the same way `OllamaNDJSONLineParser` is: a hostile or wedged server must not be able to grow
/// this buffer without limit.
nonisolated struct GatewaySSEParser: Sendable {
    private let maximumFrameBytes: Int
    private let maximumAggregateBytes: Int
    private var aggregateBytes = 0
    private var line = Data()
    private var frame = Data()
    private var frameHasData = false

    init(maximumFrameBytes: Int, maximumAggregateBytes: Int) {
        precondition(maximumFrameBytes > 0)
        precondition(maximumAggregateBytes > 0)
        self.maximumFrameBytes = maximumFrameBytes
        self.maximumAggregateBytes = maximumAggregateBytes
        line.reserveCapacity(min(maximumFrameBytes, 4_096))
    }

    /// Feeds one byte, returning a complete event payload when a frame terminates.
    mutating func append(_ byte: UInt8) throws -> String? {
        guard aggregateBytes < maximumAggregateBytes else {
            throw GatewayClientError.streamResponseTooLarge(limitBytes: maximumAggregateBytes)
        }
        aggregateBytes += 1

        guard byte == 0x0A else {
            guard line.count < maximumFrameBytes else {
                throw GatewayClientError.streamFrameTooLarge(limitBytes: maximumFrameBytes)
            }
            line.append(byte)
            return nil
        }
        return try consumeLine()
    }

    /// Flushes a frame left unterminated when the connection closed.
    mutating func finish() throws -> String? {
        if !line.isEmpty, let completed = try consumeLine() {
            return completed
        }
        return takeFrame()
    }

    private mutating func consumeLine() throws -> String? {
        defer { line.removeAll(keepingCapacity: true) }
        // Servers may use CRLF; the byte loop only splits on LF.
        var raw = line
        if raw.last == 0x0D {
            raw.removeLast()
        }
        guard let text = String(data: raw, encoding: .utf8) else {
            throw GatewayClientError.malformedStreamFrame(
                excerpt: String(decoding: raw.prefix(160), as: UTF8.self),
                reason: "The event-stream record is not valid UTF-8."
            )
        }

        // A blank line dispatches the accumulated frame.
        if text.isEmpty {
            return takeFrame()
        }
        // A leading colon marks a comment, which is how SSE keep-alives are written.
        if text.hasPrefix(":") {
            return nil
        }

        let field: Substring
        var value: Substring
        if let separator = text.firstIndex(of: ":") {
            field = text[text.startIndex..<separator]
            value = text[text.index(after: separator)...]
            if value.first == " " {
                value = value.dropFirst()
            }
        } else {
            field = text[...]
            value = ""
        }
        guard field == "data" else { return nil }

        if frameHasData {
            frame.append(0x0A)
        }
        frame.append(contentsOf: Array(value.utf8))
        frameHasData = true
        guard frame.count <= maximumFrameBytes else {
            throw GatewayClientError.streamFrameTooLarge(limitBytes: maximumFrameBytes)
        }
        return nil
    }

    private mutating func takeFrame() -> String? {
        guard frameHasData else { return nil }
        defer {
            frame.removeAll(keepingCapacity: true)
            frameHasData = false
        }
        return String(decoding: frame, as: UTF8.self)
    }
}

/// Stateless decoder for one gateway SSE event payload.
///
/// Kept apart from URLSession for the same reason `OllamaChatChunkDecoder` is: the protocol is then
/// deterministic to test, and a fixture stream decodes exactly as a live connection does.
public nonisolated struct GatewayEventDecoder: Sendable {
    public init() {}

    public func decode(frame: String) throws -> [GatewayEvent] {
        let trimmed = frame.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }

        let payload: FramePayload
        do {
            payload = try JSONDecoder().decode(FramePayload.self, from: Data(trimmed.utf8))
        } catch {
            throw GatewayClientError.malformedStreamFrame(
                excerpt: String(trimmed.prefix(160)),
                reason: error.localizedDescription
            )
        }

        let data = payload.data ?? EventData()
        let text = data.text ?? ""

        switch payload.kind {
        case "delta":
            return text.isEmpty ? [] : [.contentDelta(text)]
        case "assistant", "narration":
            // Both carry the round's complete narration; the engine picks between them purely on
            // whether streaming is enabled, so they render identically.
            return [.contentReplace(text)]
        case "thinking":
            return text.isEmpty ? [] : [.thinkingBlock(text)]
        case "plan":
            let steps = (data.steps ?? []).filter { !$0.isEmpty }
            return steps.isEmpty ? [] : [.plan(steps)]
        case "tool_call":
            guard let id = data.id, let name = data.name else { return [] }
            return [.toolCall(GatewayToolCall(id: id, name: name, summary: data.summary ?? ""))]
        case "tool_denied":
            // A denial is always followed by a matching failed `tool_result`, so rendering this too
            // would duplicate the row.
            return []
        case "tool_result":
            guard let id = data.id, let name = data.name else { return [] }
            return [
                .toolOutcome(
                    GatewayToolOutcome(
                        id: id,
                        name: name,
                        isSuccess: data.ok ?? false,
                        firstLine: data.firstLine ?? ""
                    )
                )
            ]
        case "permission":
            guard let id = data.id, let tool = data.tool else { return [] }
            return [
                .permissionRequest(
                    GatewayPermissionRequest(
                        id: id,
                        tool: tool,
                        summary: data.summary ?? "",
                        diff: data.diff?.isEmpty == false ? data.diff : nil,
                        rule: data.rule?.isEmpty == false ? data.rule : nil,
                        sandbox: data.sandbox?.isEmpty == false ? data.sandbox : nil,
                        allowsNetwork: data.network ?? false
                    )
                )
            ]
        case "info":
            // The per-turn token/model routing line is live telemetry, not conversation content;
            // the web client hides it for the same reason.
            if text.isEmpty || Self.isInternalTelemetry(text) {
                return []
            }
            return [.notice(level: .info, text: text)]
        case "warn":
            return text.isEmpty ? [] : [.notice(level: .warning, text: text)]
        case "error":
            throw Self.error(for: text)
        case "compact":
            return [.compacted(before: data.before ?? 0, after: data.after ?? 0)]
        case "done":
            let usage = data.turnUsage ?? data.usage
            return [
                .completed(
                    GatewayTurnSummary(
                        text: text,
                        usage: usage.map {
                            GatewayTurnUsage(
                                inputTokens: $0.input ?? 0,
                                outputTokens: $0.output ?? 0,
                                milliseconds: $0.ms ?? 0
                            )
                        }
                    )
                )
            ]
        case "end":
            return [.ended]
        default:
            // `user`, `widget`, and anything a newer gateway adds.
            return []
        }
    }

    /// Distinguishes "the gateway refused this turn" from "the turn failed part-way", which the app
    /// treats very differently: a refusal restores the draft, a failure keeps the partial reply.
    static func error(for text: String) -> GatewayClientError {
        let lowercased = text.lowercased()
        let isRejection = lowercased.contains("still working")
            || lowercased.contains("session is busy")
            || lowercased.contains("busy or unavailable")
        if isRejection {
            return .busy(message: text)
        }
        return .server(message: text.isEmpty ? "The turn ended unexpectedly." : text)
    }

    static func isInternalTelemetry(_ text: String) -> Bool {
        guard text.hasPrefix("sending ~") else { return false }
        return text.contains(" tokens to ")
    }
}

private nonisolated struct FramePayload: Decodable {
    let kind: String
    let data: EventData?
}

/// The union of every `data` object the gateway emits. Keeping it flat means one decode pass per
/// frame; fields belonging to other kinds simply stay nil. `tool_call.args` is deliberately absent —
/// it is arbitrarily shaped JSON the app never renders, and `summary` already describes the call.
private nonisolated struct EventData: Decodable {
    var text: String?
    var steps: [String]?
    var id: String?
    var name: String?
    var summary: String?
    var ok: Bool?
    var firstLine: String?
    var tool: String?
    var diff: String?
    var rule: String?
    var sandbox: String?
    var network: Bool?
    var before: Int?
    var after: Int?
    var usage: UsagePayload?
    var turnUsage: UsagePayload?

    init() {}

    enum CodingKeys: String, CodingKey {
        case text
        case steps
        case id
        case name
        case summary
        case ok
        case firstLine = "first_line"
        case tool
        case diff
        case rule
        case sandbox
        case network
        case before
        case after
        case usage
        case turnUsage = "turn_usage"
    }
}

private nonisolated struct UsagePayload: Decodable {
    let input: Int?
    let output: Int?
    let ms: Int?
}
