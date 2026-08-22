import Foundation

/// Separates a gateway answer into displayable text and reasoning.
///
/// With streaming on — the default — the gateway's engine never emits a separate reasoning channel.
/// Models that think out loud wrap it in `<think>` tags *inside* the delta text, and the gateway's
/// system prompt also teaches models to emit ```hud fenced JSON for the web UI's widgets. Neither is
/// stripped server-side (`_clean()` only touches stored history), so a client that renders raw
/// deltas shows tags and JSON blobs in the transcript. The web client solves this the same way.
nonisolated enum GatewayTextSanitizer {
    struct Split: Equatable, Sendable {
        var answer: String
        var reasoning: String
    }

    private static let openTags = ["<think>", "<thinking>"]
    private static let closeTags = ["</think>", "</thinking>"]

    /// Splits raw assistant text, tolerating a block still being streamed (an open tag with no
    /// close yet means everything after it is reasoning so far).
    static func split(_ raw: String) -> Split {
        // Case-insensitive, because models emit <think>, <Think>, and <THINK> alike.
        guard raw.range(of: "<think", options: .caseInsensitive) != nil else {
            return Split(answer: strippingHUDFences(raw), reasoning: "")
        }

        var answer = ""
        var reasoning = ""
        var remainder = Substring(raw)

        while let open = firstRange(of: openTags, in: remainder) {
            answer += remainder[remainder.startIndex..<open.lowerBound]
            let afterOpen = remainder[open.upperBound...]
            guard let close = firstRange(of: closeTags, in: afterOpen) else {
                // Still streaming this block.
                appendBlock(String(afterOpen), to: &reasoning)
                return Split(
                    answer: strippingHUDFences(answer),
                    reasoning: reasoning.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            }
            appendBlock(String(afterOpen[afterOpen.startIndex..<close.lowerBound]), to: &reasoning)
            remainder = afterOpen[close.upperBound...]
        }
        answer += remainder

        return Split(
            answer: strippingHUDFences(answer),
            reasoning: reasoning.trimmingCharacters(in: .whitespacesAndNewlines)
        )
    }

    /// Removes ```hud fenced blocks, which carry widget payloads the app does not render.
    static func strippingHUDFences(_ text: String) -> String {
        guard text.contains("```hud") else {
            return text.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        var output = ""
        var remainder = Substring(text)
        while let fence = remainder.range(of: "```hud") {
            output += remainder[remainder.startIndex..<fence.lowerBound]
            let afterFence = remainder[fence.upperBound...]
            guard let close = afterFence.range(of: "```") else {
                // An unterminated fence is still being streamed — drop the rest.
                remainder = afterFence[afterFence.endIndex...]
                break
            }
            remainder = afterFence[close.upperBound...]
        }
        output += remainder
        return output.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func appendBlock(_ block: String, to reasoning: inout String) {
        let trimmed = block.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if !reasoning.isEmpty {
            reasoning += "\n\n"
        }
        reasoning += trimmed
    }

    private static func firstRange(
        of candidates: [String],
        in text: Substring
    ) -> Range<Substring.Index>? {
        var earliest: Range<Substring.Index>?
        for candidate in candidates {
            guard let found = text.range(of: candidate, options: .caseInsensitive) else { continue }
            if earliest == nil || found.lowerBound < earliest!.lowerBound {
                earliest = found
            }
        }
        return earliest
    }
}
