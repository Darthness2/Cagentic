import Foundation

/// Stateless decoder for one newline-delimited JSON record from `/api/chat`.
///
/// Keeping this separate from URLSession makes the protocol deterministic to test and lets preview
/// fixtures use the exact same decoding behavior as a live connection.
public nonisolated struct OllamaChatChunkDecoder: Sendable {
    public init() {}

    public func decode(line: String) throws -> [OllamaChatEvent] {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }

        let payload: ChatChunkPayload
        do {
            payload = try JSONDecoder().decode(ChatChunkPayload.self, from: Data(trimmed.utf8))
        } catch {
            throw OllamaClientError.malformedStreamChunk(
                excerpt: String(trimmed.prefix(160)),
                reason: error.localizedDescription
            )
        }

        if let message = payload.error?.trimmingCharacters(in: .whitespacesAndNewlines),
           !message.isEmpty
        {
            throw OllamaClientError.server(message: message)
        }

        var events: [OllamaChatEvent] = []
        let thinking = payload.message?.thinking ?? payload.thinking
        if let thinking, !thinking.isEmpty {
            events.append(.thinking(thinking))
        }
        if let content = payload.message?.content, !content.isEmpty {
            events.append(.content(content))
        }
        if payload.done == true {
            events.append(
                .completed(
                    OllamaChatCompletion(
                        model: payload.model,
                        createdAt: payload.createdAt,
                        doneReason: payload.doneReason,
                        metrics: OllamaChatMetrics(
                            totalDurationNanoseconds: payload.totalDuration,
                            loadDurationNanoseconds: payload.loadDuration,
                            promptTokenCount: payload.promptEvaluationCount,
                            promptEvaluationDurationNanoseconds: payload.promptEvaluationDuration,
                            generatedTokenCount: payload.evaluationCount,
                            generationDurationNanoseconds: payload.evaluationDuration
                        )
                    )
                )
            )
        }
        return events
    }
}

private nonisolated struct ChatChunkPayload: Decodable {
    let model: String?
    let createdAt: String?
    let message: MessagePayload?
    let thinking: String?
    let done: Bool?
    let doneReason: String?
    let error: String?
    let totalDuration: Int64?
    let loadDuration: Int64?
    let promptEvaluationCount: Int?
    let promptEvaluationDuration: Int64?
    let evaluationCount: Int?
    let evaluationDuration: Int64?

    enum CodingKeys: String, CodingKey {
        case model
        case createdAt = "created_at"
        case message
        case thinking
        case done
        case doneReason = "done_reason"
        case error
        case totalDuration = "total_duration"
        case loadDuration = "load_duration"
        case promptEvaluationCount = "prompt_eval_count"
        case promptEvaluationDuration = "prompt_eval_duration"
        case evaluationCount = "eval_count"
        case evaluationDuration = "eval_duration"
    }
}

private nonisolated struct MessagePayload: Decodable {
    let content: String?
    let thinking: String?
}
