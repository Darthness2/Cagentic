import Foundation
import Testing
@testable import Cagentic

struct OllamaRequestContextPlannerTests {
    @Test("Planner prunes only whole oldest turns")
    func prunesWholeOldestTurns() throws {
        let oldestUser = ChatMessage(role: .user, content: String(repeating: "u", count: 300))
        let oldestAssistant = ChatMessage(
            role: .assistant,
            content: String(repeating: "a", count: 300)
        )
        let currentUser = ChatMessage(role: .user, content: "Keep this current turn")

        let plan = try OllamaRequestContextPlanner.plan(
            messages: [oldestUser, oldestAssistant, currentUser],
            systemPrompt: "",
            maximumEncodedBytes: 1_024,
            maximumEstimatedTokens: 10_000
        )

        #expect(plan.omittedOlderTurns)
        #expect(plan.messages.map(\.id) == [currentUser.id])
    }

    @Test("Planner rejects a current turn that cannot fit")
    func rejectsOversizedCurrentTurn() {
        let currentUser = ChatMessage(
            role: .user,
            content: String(repeating: "x", count: 1_024)
        )

        #expect(throws: AttachmentError.requestContextTooLarge(maximumBytes: 700)) {
            _ = try OllamaRequestContextPlanner.plan(
                messages: [currentUser],
                systemPrompt: "",
                maximumEncodedBytes: 700,
                maximumEstimatedTokens: 10_000
            )
        }
    }

    @Test("Planner keeps mandatory system messages with the current turn")
    func keepsSystemMessages() throws {
        let system = ChatMessage(role: .system, content: "Use the local style guide")
        let olderUser = ChatMessage(role: .user, content: String(repeating: "old", count: 200))
        let olderAssistant = ChatMessage(role: .assistant, content: "Acknowledged")
        let currentUser = ChatMessage(role: .user, content: "What changed?")

        let plan = try OllamaRequestContextPlanner.plan(
            messages: [system, olderUser, olderAssistant, currentUser],
            systemPrompt: "Be concise",
            maximumEncodedBytes: 1_500,
            maximumEstimatedTokens: 10_000
        )

        #expect(plan.omittedOlderTurns)
        #expect(plan.messages.map(\.id) == [system.id, currentUser.id])
    }
}
