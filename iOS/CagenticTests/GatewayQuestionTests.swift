import Foundation
import Testing
@testable import Cagentic

/// The model asking *you* something.
///
/// `ask_user_question` reads stdin, which belongs to the terminal REPL, so on a gateway it used
/// to return an error telling the model to ask in its reply instead. The gateway now puts the
/// question to whoever is watching and parks the turn on the answer.
@MainActor
struct GatewayQuestionTests {
    @Test("A question decodes with its options")
    func decodesQuestion() throws {
        let events = try GatewayEventDecoder().decode(
            frame: #"""
            {"kind":"question","data":{"id":"q1","question":"Which database?",
            "options":["Postgres","SQLite",""]}}
            """#
        )

        guard case .question(let question) = try #require(events.first) else {
            Issue.record("Expected a question")
            return
        }
        #expect(question.id == "q1")
        #expect(question.question == "Which database?")
        // Blank choices are dropped rather than rendered as an empty button.
        #expect(question.options == ["Postgres", "SQLite"])
    }

    @Test("A question with no text is not shown at all")
    func ignoresEmptyQuestion() throws {
        #expect(try GatewayEventDecoder().decode(
            frame: #"{"kind":"question","data":{"id":"q1","question":""}}"#
        ).isEmpty)
        #expect(try GatewayEventDecoder().decode(
            frame: #"{"kind":"question","data":{"question":"No id"}}"#
        ).isEmpty)
    }

    @Test("Answering sends it back and clears the card")
    func answeringSends() async throws {
        let stream = ControlledGatewayStream()
        let service = MockGatewayService(bootstrap: .stub(), controlled: stream)
        let model = try await connectedModel(gateway: service)

        model.send("Set up a database")
        await settle { stream.hasSubscriber }
        stream.yield(.question(GatewayQuestion(id: "q1", question: "Which?", options: ["A", "B"])))
        await settle { await MainActor.run { model.pendingQuestion?.id == "q1" } }

        model.answerPendingQuestion("B")

        // Cleared straight away: the turn is parked on this, and leaving the card up while the
        // answer is in flight invites a second one.
        #expect(model.pendingQuestion == nil)
        await settle { await service.questionAnswers.isEmpty == false }
        #expect(await service.questionAnswers == [QuestionAnswerRecord(id: "q1", answer: "B")])
    }

    @Test("An empty answer is not sent")
    func emptyAnswerIsIgnored() async throws {
        let stream = ControlledGatewayStream()
        let service = MockGatewayService(bootstrap: .stub(), controlled: stream)
        let model = try await connectedModel(gateway: service)

        model.send("Set up a database")
        await settle { stream.hasSubscriber }
        stream.yield(.question(GatewayQuestion(id: "q1", question: "Which?", options: [])))
        await settle { await MainActor.run { model.pendingQuestion?.id == "q1" } }

        model.answerPendingQuestion("   ")

        // Sending blank would park the turn on a meaningless answer rather than a real one.
        #expect(model.pendingQuestion?.id == "q1")
        #expect(await service.questionAnswers.isEmpty)
    }

    // MARK: - Harness

    private func connectedModel(gateway: MockGatewayService) async throws -> AppModel {
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

    private func settle(_ condition: @escaping @Sendable () -> Bool) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while !condition(), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(condition())
    }

    private func settle(until condition: () async -> Bool) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while await !condition(), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(await condition(), "the gateway never received the answer")
    }
}
