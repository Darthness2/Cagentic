import Foundation
import Testing
@testable import Cagentic

/// Editing and regenerating on a gateway-backed chat.
///
/// Both were greyed out for every gateway conversation, on the assumption that a chat the gateway
/// owns cannot be rewritten. It can — `/api/chat/edit` truncates from a turn and re-runs it, and
/// the gateway's own web page has always used it. What genuinely cannot work there is *branching*,
/// which forks a chat into a second one the gateway has no concept of.
@MainActor
struct GatewayEditTests {
    @Test("Regenerating asks the gateway to re-run that turn")
    func regenerateResends() async throws {
        let service = MockGatewayService(bootstrap: .stub(current: transcript()))
        let model = try await connected(gateway: service)
        let conversation = try #require(model.selectedConversation)
        let reply = try #require(conversation.messages.last { $0.role == .assistant })

        model.retryResponse(messageID: reply.id, in: conversation.id)

        await settle { await service.edits.isEmpty == false }
        // Index 1: the *second* visible user message, which is what the route counts. Counting
        // all messages instead would have re-run the wrong turn.
        #expect(await service.edits == [EditRecord(index: 1, message: "second question")])
    }

    @Test("Editing sends the new text for that turn")
    func editResends() async throws {
        let service = MockGatewayService(bootstrap: .stub(current: transcript()))
        let model = try await connected(gateway: service)
        let conversation = try #require(model.selectedConversation)
        let firstUser = try #require(conversation.messages.first { $0.role == .user })

        model.editUserMessage(
            messageID: firstUser.id,
            in: conversation.id,
            content: "rewritten question"
        )

        await settle { await service.edits.isEmpty == false }
        #expect(await service.edits == [EditRecord(index: 0, message: "rewritten question")])
    }

    @Test("The mirror is trimmed to what the gateway will have")
    func mirrorIsTrimmed() async throws {
        let service = MockGatewayService(bootstrap: .stub(current: transcript()))
        let model = try await connected(gateway: service)
        let conversation = try #require(model.selectedConversation)
        let firstUser = try #require(conversation.messages.first { $0.role == .user })

        model.editUserMessage(
            messageID: firstUser.id,
            in: conversation.id,
            content: "rewritten question"
        )

        // The later turns are gone on both sides, and the edited turn is waiting on a reply.
        let messages = try #require(model.selectedConversation?.messages)
        #expect(messages.count == 2)
        #expect(messages.first?.content == "rewritten question")
        #expect(messages.last?.role == .assistant)
        #expect(messages.last?.state == .streaming)
    }

    @Test("An empty edit is refused rather than sent")
    func emptyEditIsRefused() async throws {
        let service = MockGatewayService(bootstrap: .stub(current: transcript()))
        let model = try await connected(gateway: service)
        let conversation = try #require(model.selectedConversation)
        let firstUser = try #require(conversation.messages.first { $0.role == .user })

        model.editUserMessage(messageID: firstUser.id, in: conversation.id, content: "   ")

        #expect(await service.edits.isEmpty)
        // And nothing was trimmed on the strength of an edit that never happened.
        #expect(model.selectedConversation?.messages.count == conversation.messages.count)
    }

    @Test("Branching stays local, because the gateway has no fork")
    func branchingIsLocalOnly() async throws {
        let service = MockGatewayService(bootstrap: .stub(current: transcript()))
        let model = try await connected(gateway: service)

        #expect(!model.conversationsAreLocallyOwned)
    }

    // MARK: - Harness

    private func transcript() -> GatewayChatDetail {
        GatewayChatDetail(
            id: "live0000",
            title: "Chat",
            model: "qwen2.5:7b",
            messages: [
                GatewayDisplayMessage(role: .user, content: "first question", tools: []),
                GatewayDisplayMessage(role: .assistant, content: "first answer", tools: []),
                GatewayDisplayMessage(role: .user, content: "second question", tools: []),
                GatewayDisplayMessage(role: .assistant, content: "second answer", tools: []),
            ]
        )
    }

    private func connected(gateway: MockGatewayService) async throws -> AppModel {
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { _, _ in MockUnusedOllamaService() },
            gatewayFactory: GatewayServiceFactory { _, _ in gateway },
            isRestoring: false
        )
        let result = await model.configureConnection(
            serverURL: "192.168.1.42:8700",
            serverName: "Studio",
            kind: .gateway,
            bearerToken: "secret-token"
        )
        if case .failure(let error) = result { throw error }
        return model
    }

    private func settle(until condition: () async -> Bool) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while await !condition(), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(await condition(), "the gateway was never asked to re-run the turn")
    }
}
