import Foundation

nonisolated struct OllamaRequestContextPlan: Equatable, Sendable {
    let messages: [ChatMessage]
    let omittedOlderTurns: Bool
}

nonisolated enum OllamaRequestContextPlanner {
    static let omissionMarker =
        "Older conversation turns were omitted from this request to stay within the local context limit."

    static func plan(
        messages: [ChatMessage],
        systemPrompt: String,
        maximumEncodedBytes: Int,
        maximumEstimatedTokens: Int
    ) throws -> OllamaRequestContextPlan {
        let systemMessages = messages.filter { $0.role == .system }
        let conversationalMessages = messages.filter { $0.role != .system }
        let turns = groupedTurns(from: conversationalMessages)
        guard let newestTurn = turns.last else {
            return OllamaRequestContextPlan(
                messages: systemMessages,
                omittedOlderTurns: false
            )
        }

        let requiredMessages = systemMessages + newestTurn
        let reservedCost = try cost(
            forText: systemPrompt + omissionMarker,
            fixedEncodedBytes: 512,
            fixedTokens: 24
        )
        var selectedCost = try requiredMessages.reduce(reservedCost) { partial, message in
            try partial.adding(cost(for: message))
        }
        guard selectedCost.fits(
            maximumEncodedBytes: maximumEncodedBytes,
            maximumEstimatedTokens: maximumEstimatedTokens
        ) else {
            throw AttachmentError.requestContextTooLarge(maximumBytes: maximumEncodedBytes)
        }

        var firstSelectedTurnIndex = turns.index(before: turns.endIndex)
        while firstSelectedTurnIndex > turns.startIndex {
            let candidateIndex = turns.index(before: firstSelectedTurnIndex)
            let candidateCost = try turns[candidateIndex].reduce(ContextCost.zero) {
                partial,
                message in
                try partial.adding(cost(for: message))
            }
            let expandedCost = try selectedCost.adding(candidateCost)
            guard expandedCost.fits(
                maximumEncodedBytes: maximumEncodedBytes,
                maximumEstimatedTokens: maximumEstimatedTokens
            ) else {
                break
            }
            selectedCost = expandedCost
            firstSelectedTurnIndex = candidateIndex
        }

        let selectedTurns = turns[firstSelectedTurnIndex...].flatMap { $0 }
        return OllamaRequestContextPlan(
            messages: systemMessages + selectedTurns,
            omittedOlderTurns: firstSelectedTurnIndex > turns.startIndex
        )
    }

    private static func groupedTurns(from messages: [ChatMessage]) -> [[ChatMessage]] {
        var turns: [[ChatMessage]] = []
        for message in messages {
            if message.role == .user || turns.isEmpty {
                turns.append([message])
            } else {
                turns[turns.index(before: turns.endIndex)].append(message)
            }
        }
        return turns
    }

    private static func cost(for message: ChatMessage) throws -> ContextCost {
        var result = try cost(
            forText: message.content + message.thinking,
            fixedEncodedBytes: 256,
            fixedTokens: 16
        )
        for attachment in message.attachments {
            guard attachment.byteCount >= 0,
                  attachment.byteCount <= Int64(Int.max)
            else {
                throw AttachmentError.invalidMetadata
            }
            let byteCount = Int(attachment.byteCount)
            let attachmentCost: ContextCost
            if attachment.isOllamaImage {
                let encodedBytes = try base64EncodedLength(for: byteCount)
                attachmentCost = ContextCost(
                    encodedBytes: try checkedSum(encodedBytes, 256),
                    estimatedTokens: 512
                )
            } else {
                attachmentCost = try cost(
                    forTextByteCount: byteCount,
                    fixedEncodedBytes: 512,
                    fixedTokens: 24
                )
            }
            result = try result.adding(attachmentCost)
        }
        return result
    }

    private static func cost(
        forText text: String,
        fixedEncodedBytes: Int,
        fixedTokens: Int
    ) throws -> ContextCost {
        try cost(
            forTextByteCount: text.utf8.count,
            fixedEncodedBytes: fixedEncodedBytes,
            fixedTokens: fixedTokens
        )
    }

    private static func cost(
        forTextByteCount textByteCount: Int,
        fixedEncodedBytes: Int,
        fixedTokens: Int
    ) throws -> ContextCost {
        ContextCost(
            encodedBytes: try checkedSum(textByteCount, fixedEncodedBytes),
            estimatedTokens: try checkedSum(
                try checkedSum(textByteCount, 3) / 4,
                fixedTokens
            )
        )
    }

    private static func base64EncodedLength(for byteCount: Int) throws -> Int {
        let (adjusted, overflow) = byteCount.addingReportingOverflow(2)
        guard !overflow else {
            throw AttachmentError.invalidMetadata
        }
        let groups = adjusted / 3
        let (result, multiplicationOverflow) = groups.multipliedReportingOverflow(by: 4)
        guard !multiplicationOverflow else {
            throw AttachmentError.invalidMetadata
        }
        return result
    }

    private static func checkedSum(_ lhs: Int, _ rhs: Int) throws -> Int {
        let (result, overflow) = lhs.addingReportingOverflow(rhs)
        guard !overflow else {
            throw AttachmentError.invalidMetadata
        }
        return result
    }
}

private nonisolated struct ContextCost: Sendable {
    let encodedBytes: Int
    let estimatedTokens: Int

    static let zero = ContextCost(encodedBytes: 0, estimatedTokens: 0)

    func adding(_ other: ContextCost) throws -> ContextCost {
        let (encodedBytes, encodedOverflow) = self.encodedBytes.addingReportingOverflow(
            other.encodedBytes
        )
        let (estimatedTokens, tokenOverflow) = self.estimatedTokens.addingReportingOverflow(
            other.estimatedTokens
        )
        guard !encodedOverflow, !tokenOverflow else {
            throw AttachmentError.invalidMetadata
        }
        return ContextCost(
            encodedBytes: encodedBytes,
            estimatedTokens: estimatedTokens
        )
    }

    func fits(maximumEncodedBytes: Int, maximumEstimatedTokens: Int) -> Bool {
        encodedBytes <= maximumEncodedBytes && estimatedTokens <= maximumEstimatedTokens
    }
}
