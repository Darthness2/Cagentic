import Foundation
import Testing
@testable import Cagentic

struct AppModelGatewayTests {
    @Test("Connecting to a gateway adopts its models and its chats")
    func connectingAdoptsGatewayState() async throws {
        let service = MockGatewayService(
            bootstrap: GatewayBootstrap(
                version: "0.9.3",
                activeModel: "anthropic:claude-sonnet-4",
                models: ["qwen2.5:7b", "anthropic:claude-sonnet-4"],
                chats: [
                    GatewayChatSummary(
                        id: "aaaa1111bbbb",
                        title: "Refactor the parser",
                        updatedAt: Date(timeIntervalSince1970: 1_700_000_000),
                        turns: 4
                    ),
                    GatewayChatSummary(
                        id: "cccc2222dddd",
                        title: "New chat",
                        updatedAt: Date(timeIntervalSince1970: 1_700_000_500),
                        turns: 0
                    ),
                ],
                current: GatewayChatDetail(
                    id: "cccc2222dddd",
                    title: "New chat",
                    model: "anthropic:claude-sonnet-4",
                    messages: []
                )
            )
        )
        let model = makeAppModel(gateway: service)

        try await connectGateway(model)

        #expect(model.connectionState == .connected(version: "0.9.3"))
        #expect(model.isUsingGateway)
        #expect(model.activeServerKind == .gateway)
        // The gateway's own model is adopted rather than the app picking one for it.
        #expect(model.settings.selectedModel == "anthropic:claude-sonnet-4")
        #expect(model.availableModels.map(\.name) == ["anthropic:claude-sonnet-4", "qwen2.5:7b"])
        #expect(model.visibleConversationSummaries.count == 2)
        #expect(model.selectedConversation?.remoteID == "cccc2222dddd")
        #expect(model.selectedConversation?.isRemoteMirror == true)
    }

    @Test("A gateway connection without a token is refused before any request")
    func gatewayRequiresToken() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        let model = makeAppModel(gateway: service)

        let result = await model.configureConnection(
            serverURL: "192.168.1.42:8700",
            serverName: "Studio",
            kind: .gateway,
            bearerToken: "   "
        )

        guard case .failure(let error) = result else {
            Issue.record("Expected a tokenless gateway connection to fail")
            return
        }
        #expect(error as? GatewayClientError == .missingToken)
        #expect(await service.bootstrapCount == 0)
    }

    @Test("A turn records narration, tool activity, and the final answer in order")
    func turnRecordsActivityTimeline() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        await service.setChatScript([
            .contentDelta("I'll read the file"),
            .contentDelta(" first."),
            .toolCall(GatewayToolCall(id: "t1", name: "read_file", summary: "notes.txt")),
            .toolOutcome(
                GatewayToolOutcome(
                    id: "t1",
                    name: "read_file",
                    isSuccess: true,
                    firstLine: "12 lines read"
                )
            ),
            .contentDelta("The file has 12 lines."),
            .completed(
                GatewayTurnSummary(
                    text: "The file has 12 lines.",
                    usage: GatewayTurnUsage(inputTokens: 120, outputTokens: 40, milliseconds: 2_500)
                )
            ),
            .ended,
        ])
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("How long is notes.txt?")
        await waitUntil("the turn to finish") { !model.isGenerating }

        let messages = try #require(model.selectedConversation?.messages)
        #expect(messages.count == 2)
        #expect(try #require(messages.first).role == .user)

        let reply = try #require(messages.last)
        #expect(reply.state == .complete)
        // The narration before the tool call is preserved as its own step, in order.
        #expect(reply.activity.map(\.kind) == [.narration, .tool])
        #expect(reply.activity.first?.text == "I'll read the file first.")
        #expect(reply.activity.last?.toolName == "read_file")
        #expect(reply.activity.last?.toolState == .succeeded)
        #expect(reply.activity.last?.resultLine == "12 lines read")
        // Only the text after the last tool call is the answer.
        #expect(reply.content == "The file has 12 lines.")
        #expect(reply.metrics?.promptTokenCount == 120)
        #expect(reply.metrics?.responseTokenCount == 40)
        #expect(await service.sentMessages == ["How long is notes.txt?"])
    }

    @Test("A round's narration is not repeated when the engine restates it")
    func doesNotRepeatRestatedNarration() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        await service.setChatScript([
            .contentDelta("I'll take a look at the parser."),
            .plan(["Read parser.py", "Report findings"]),
            // The engine closes each model round by restating that round's complete narration.
            .contentReplace("I'll take a look at the parser."),
            .toolCall(GatewayToolCall(id: "t1", name: "read_file", summary: "parser.py")),
            .toolOutcome(
                GatewayToolOutcome(id: "t1", name: "read_file", isSuccess: true, firstLine: "ok")
            ),
            .contentDelta("It looks healthy."),
            .ended,
        ])
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("Audit the parser")
        await waitUntil("the turn to finish") { !model.isGenerating }

        let reply = try #require(model.selectedConversation?.messages.last)
        let narrations = reply.activity.filter { $0.kind == .narration }.map(\.text)
        #expect(narrations == ["I'll take a look at the parser."])
        #expect(reply.activity.map(\.kind) == [.plan, .narration, .tool])
        #expect(reply.content == "It looks healthy.")
    }

    @Test("Reasoning streamed inside think tags never reaches the answer")
    func separatesInlineReasoning() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        await service.setChatScript([
            .contentDelta("<think>They want a count"),
            .contentDelta(", not the text.</think>"),
            .contentDelta("12 lines."),
            .completed(GatewayTurnSummary(text: "12 lines.", usage: nil)),
            .ended,
        ])
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("How long?")
        await waitUntil("the turn to finish") { !model.isGenerating }

        let reply = try #require(model.selectedConversation?.messages.last)
        #expect(reply.content == "12 lines.")
        #expect(reply.thinking == "They want a count, not the text.")
    }

    @Test("Reasoning survives the engine restating a round's narration")
    func keepsReasoningAcrossContentReplace() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        await service.setChatScript([
            .contentDelta("<think>Check the parser"),
            .contentDelta(" before answering.</think>I'll take a look."),
            .plan(["Read parser.py"]),
            // Restates the narration only — the reasoning is not repeated.
            .contentReplace("I'll take a look."),
            .toolCall(GatewayToolCall(id: "t1", name: "read_file", summary: "parser.py")),
            .toolOutcome(
                GatewayToolOutcome(id: "t1", name: "read_file", isSuccess: true, firstLine: "ok")
            ),
            .contentDelta("All good."),
            .ended,
        ])
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("Audit the parser")
        await waitUntil("the turn to finish") { !model.isGenerating }

        let reply = try #require(model.selectedConversation?.messages.last)
        #expect(reply.thinking == "Check the parser before answering.")
        #expect(reply.content == "All good.")
        #expect(!reply.content.contains("<think"))
    }

    @Test("A failed tool is marked failed rather than dropped")
    func recordsFailedTool() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        await service.setChatScript([
            .toolCall(GatewayToolCall(id: "t9", name: "run_bash", summary: "ls /nope")),
            .toolOutcome(
                GatewayToolOutcome(
                    id: "t9",
                    name: "run_bash",
                    isSuccess: false,
                    firstLine: "ERROR: no such directory"
                )
            ),
            .contentDelta("That path does not exist."),
            .ended,
        ])
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("List /nope")
        await waitUntil("the turn to finish") { !model.isGenerating }

        let reply = try #require(model.selectedConversation?.messages.last)
        let tool = try #require(reply.activity.first { $0.kind == .tool })
        #expect(tool.toolState == .failed)
        #expect(tool.resultLine == "ERROR: no such directory")
        #expect(reply.state == .complete)
    }

    @Test("An approval request is surfaced and answered with its own id and rule")
    func surfacesAndAnswersPermission() async throws {
        let stream = ControlledGatewayStream()
        let service = MockGatewayService(bootstrap: .stub(), controlled: stream)
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("Check git status")
        await waitUntil("the stream to be subscribed") { stream.hasSubscriber }

        stream.yield(
            .permissionRequest(
                GatewayPermissionRequest(
                    id: "p1",
                    tool: "run_bash",
                    summary: "git status",
                    diff: nil,
                    rule: "run_bash(git status*)",
                    sandbox: "seatbelt · no network",
                    allowsNetwork: false
                )
            )
        )
        await waitUntil("the approval to be surfaced") { model.pendingPermission != nil }
        #expect(model.pendingPermission?.tool == "run_bash")
        // The turn is parked on the gateway, so the app is still generating.
        #expect(model.isGenerating)

        model.answerPendingPermission(.allowRule)
        await waitUntil("the answer to be delivered") { await service.permissionAnswers.count == 1 }

        let answer = try #require(await service.permissionAnswers.first)
        #expect(answer.id == "p1")
        #expect(answer.answer == .allowRule)
        // The rule must be echoed byte-for-byte; the gateway drops any rule it did not offer.
        #expect(answer.rule == "run_bash(git status*)")
        #expect(model.pendingPermission == nil)

        stream.yield(.contentDelta("Clean tree."))
        stream.yield(.ended)
        stream.finish()
        await waitUntil("the turn to finish") { !model.isGenerating }
    }

    @Test("Denying an approval sends a denial without a rule")
    func deniesPermission() async throws {
        let stream = ControlledGatewayStream()
        let service = MockGatewayService(bootstrap: .stub(), controlled: stream)
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("Delete everything")
        await waitUntil("the stream to be subscribed") { stream.hasSubscriber }
        stream.yield(
            .permissionRequest(
                GatewayPermissionRequest(
                    id: "p2",
                    tool: "run_bash",
                    summary: "rm -rf /",
                    rule: "run_bash(rm*)"
                )
            )
        )
        await waitUntil("the approval to be surfaced") { model.pendingPermission != nil }

        model.answerPendingPermission(.denyOnce)
        await waitUntil("the answer to be delivered") { await service.permissionAnswers.count == 1 }

        let answer = try #require(await service.permissionAnswers.first)
        #expect(answer.answer == .denyOnce)
        #expect(answer.rule == nil)

        stream.yield(.ended)
        stream.finish()
        await waitUntil("the turn to finish") { !model.isGenerating }
    }

    @Test("A refused turn is taken back out of the transcript and the text is returned")
    func rollsBackRefusedTurn() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        await service.setChatFailure(
            GatewayClientError.busy(message: "Cagentic is still working on the previous message.")
        )
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("Summarise the repo")
        await waitUntil("the refusal to be handled") { !model.isGenerating }

        // Nothing was generated, so nothing should look sent.
        #expect(model.selectedConversation?.messages.isEmpty == true)
        #expect(model.draft == "Summarise the repo")
        #expect(model.notice?.title == "The gateway is busy")
    }

    @Test("A turn that fails part-way keeps what it had already streamed")
    func keepsPartialReplyOnFailure() async throws {
        let stream = ControlledGatewayStream()
        let service = MockGatewayService(bootstrap: .stub(), controlled: stream)
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("Explain this")
        await waitUntil("the stream to be subscribed") { stream.hasSubscriber }
        stream.yield(.contentDelta("It starts by "))
        stream.finish(throwing: GatewayClientError.streamEndedBeforeCompletion)
        await waitUntil("the failure to be handled") { !model.isGenerating }

        let reply = try #require(model.selectedConversation?.messages.last)
        #expect(reply.state == .failed)
        #expect(reply.content == "It starts by")
        #expect(model.selectedConversation?.messages.count == 2)
    }

    @Test("Stopping a gateway turn aborts it on the computer")
    func stoppingAbortsOnGateway() async throws {
        let stream = ControlledGatewayStream()
        let service = MockGatewayService(bootstrap: .stub(), controlled: stream)
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        model.send("Do something long")
        await waitUntil("the stream to be subscribed") { stream.hasSubscriber }
        stream.yield(.contentDelta("Working"))
        await waitUntil("the first delta to arrive") {
            model.selectedConversation?.messages.last?.content.isEmpty == false
        }

        model.stopGenerating()
        await waitUntil("the abort to reach the gateway") { await service.abortCount == 1 }

        let reply = try #require(model.selectedConversation?.messages.last)
        #expect(reply.state == .cancelled)
        #expect(reply.content == "Working")
        #expect(model.pendingPermission == nil)
    }

    @Test("Rewriting the transcript is unavailable when the gateway owns it")
    func rewritesAreUnavailableOnGateway() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        #expect(!model.conversationsAreLocallyOwned)
        // A gateway turn is a plain message string, so there is nowhere to put an attachment.
        #expect(!model.canUseFileAttachments)
        #expect(!model.canUsePhotoAttachments)
    }

    @Test("Selecting a mirrored chat moves the gateway's own cursor")
    func selectingChatLoadsItOnGateway() async throws {
        let service = MockGatewayService(
            bootstrap: .stub(
                chats: [
                    GatewayChatSummary(id: "aaa", title: "First", updatedAt: .now, turns: 2),
                    GatewayChatSummary(id: "bbb", title: "Second", updatedAt: .now, turns: 1),
                ],
                current: GatewayChatDetail(id: "bbb", title: "Second", model: "m", messages: [])
            )
        )
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        let profileID = try #require(model.activeServerProfile?.id)
        let target = AppModel.mirroredConversationID(profileID: profileID, remoteID: "aaa")
        model.selectConversation(target)

        await waitUntil("the gateway to load the chat") { await service.loadedChatIDs == ["aaa"] }
    }

    @Test("A gateway's chats and local chats never appear in the same list")
    func scopesConversationsToTheActiveServer() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        let gatewayCount = model.visibleConversationSummaries.count
        #expect(gatewayCount > 0)
        #expect(model.visibleConversationSummaries.allSatisfy { summary in
            model.conversation(id: summary.id)?.isRemoteMirror == true
        })
    }

    @Test("Mirrored chat identifiers are stable across refreshes")
    func mirroredIdentifiersAreStable() {
        let profileID = ServerProfileID()
        let other = ServerProfileID()

        #expect(
            AppModel.mirroredConversationID(profileID: profileID, remoteID: "abc123")
                == AppModel.mirroredConversationID(profileID: profileID, remoteID: "abc123")
        )
        // The same gateway chat id on a different server is a different conversation.
        #expect(
            AppModel.mirroredConversationID(profileID: profileID, remoteID: "abc123")
                != AppModel.mirroredConversationID(profileID: other, remoteID: "abc123")
        )
        #expect(
            AppModel.mirroredConversationID(profileID: profileID, remoteID: "abc123")
                != AppModel.mirroredConversationID(profileID: profileID, remoteID: "abc124")
        )
    }

    @Test("A stored gateway chat is mirrored with its tool history")
    func mirrorsStoredToolHistory() async throws {
        let service = MockGatewayService(
            bootstrap: .stub(
                chats: [GatewayChatSummary(id: "zzz", title: "Audit", updatedAt: .now, turns: 1)],
                current: GatewayChatDetail(
                    id: "zzz",
                    title: "Audit",
                    model: "m",
                    messages: [
                        GatewayDisplayMessage(role: .user, content: "Audit the repo", tools: []),
                        GatewayDisplayMessage(
                            role: .assistant,
                            content: "Found two issues.\n```hud\n{\"type\":\"bar\"}\n```",
                            tools: [
                                GatewayToolDetail(
                                    name: "grep",
                                    summary: "TODO",
                                    isSuccess: true,
                                    firstLine: "4 matches"
                                ),
                                // Tri-state: a turn aborted between the call and its result.
                                GatewayToolDetail(
                                    name: "read_file",
                                    summary: "main.py",
                                    isSuccess: nil,
                                    firstLine: ""
                                ),
                            ]
                        ),
                    ]
                )
            )
        )
        let model = makeAppModel(gateway: service)
        try await connectGateway(model)

        let messages = try #require(model.selectedConversation?.messages)
        #expect(messages.count == 2)
        let assistant = try #require(messages.last)
        #expect(assistant.activity.map(\.toolName) == ["grep", "read_file"])
        #expect(assistant.activity.map(\.toolState) == [.succeeded, .running])
        // Widget payloads are not rendered, so they must not land in the transcript as raw JSON.
        #expect(assistant.content == "Found two issues.")
    }

    // MARK: - Helpers

    private func makeAppModel(gateway: MockGatewayService) -> AppModel {
        AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { _, _ in
                MockUnusedOllamaService()
            },
            gatewayFactory: GatewayServiceFactory { _, _ in gateway },
            isRestoring: false
        )
    }

    private func connectGateway(_ model: AppModel) async throws {
        let result = await model.configureConnection(
            serverURL: "192.168.1.42:8700",
            serverName: "Studio",
            kind: .gateway,
            bearerToken: "secret-token"
        )
        if case .failure(let error) = result {
            Issue.record("Expected the mock gateway connection to succeed: \(error)")
            throw error
        }
    }

    private func waitUntil(_ description: String, condition: () -> Bool) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while !condition(), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(condition(), "Timed out waiting for \(description)")
    }

    private func waitUntil(_ description: String, condition: () async -> Bool) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while !(await condition()), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(await condition(), "Timed out waiting for \(description)")
    }
}

// MARK: - Test doubles

extension GatewayBootstrap {
    static func stub(
        chats: [GatewayChatSummary]? = nil,
        current: GatewayChatDetail? = nil
    ) -> GatewayBootstrap {
        let resolvedCurrent = current
            ?? GatewayChatDetail(id: "live0000", title: "New chat", model: "qwen2.5:7b", messages: [])
        let resolvedChats = chats
            ?? [
                GatewayChatSummary(
                    id: resolvedCurrent.id,
                    title: resolvedCurrent.title,
                    updatedAt: Date(timeIntervalSince1970: 1_700_000_000),
                    turns: 0
                ),
            ]
        return GatewayBootstrap(
            version: "0.9.3",
            activeModel: "qwen2.5:7b",
            models: ["qwen2.5:7b"],
            chats: resolvedChats,
            current: resolvedCurrent
        )
    }
}

private nonisolated struct PermissionAnswerRecord: Sendable, Equatable {
    let id: String
    let answer: GatewayPermissionAnswer
    let rule: String?
}

private actor MockGatewayService: GatewayServing {
    private let bootstrapValue: GatewayBootstrap
    private let controlled: ControlledGatewayStream?
    private var script: [GatewayEvent] = []
    private var failure: (any Error)?

    private(set) var bootstrapCount = 0
    private(set) var sentMessages: [String] = []
    private(set) var permissionAnswers: [PermissionAnswerRecord] = []
    private(set) var abortCount = 0
    private(set) var loadedChatIDs: [String] = []

    init(bootstrap: GatewayBootstrap, controlled: ControlledGatewayStream? = nil) {
        bootstrapValue = bootstrap
        self.controlled = controlled
    }

    func setChatScript(_ events: [GatewayEvent]) {
        script = events
    }

    func setChatFailure(_ error: any Error) {
        failure = error
    }

    func bootstrap() async throws -> GatewayBootstrap {
        bootstrapCount += 1
        return bootstrapValue
    }

    nonisolated func chat(message: String) -> GatewayEventStream {
        // `chat` is nonisolated to match the protocol, so recording hops onto the actor.
        Task { await record(message: message) }
        if let controlled {
            return controlled.makeStream()
        }
        return AsyncThrowingStream { continuation in
            Task {
                if let failure = await self.currentFailure {
                    continuation.finish(throwing: failure)
                    return
                }
                for event in await self.currentScript {
                    continuation.yield(event)
                }
                continuation.finish()
            }
        }
    }

    private var currentScript: [GatewayEvent] { script }
    private var currentFailure: (any Error)? { failure }

    private func record(message: String) {
        sentMessages.append(message)
    }

    func answerPermission(
        id: String,
        answer: GatewayPermissionAnswer,
        rule: String?
    ) async throws {
        permissionAnswers.append(PermissionAnswerRecord(id: id, answer: answer, rule: rule))
    }

    func abort() async throws {
        abortCount += 1
    }

    func newChat() async throws -> GatewayChatsSnapshot {
        GatewayChatsSnapshot(chats: bootstrapValue.chats, current: bootstrapValue.current)
    }

    func loadChat(id: String) async throws -> GatewayChatsSnapshot {
        loadedChatIDs.append(id)
        return GatewayChatsSnapshot(chats: bootstrapValue.chats, current: bootstrapValue.current)
    }

    func deleteChat(id: String) async throws -> GatewayChatsSnapshot {
        GatewayChatsSnapshot(
            chats: bootstrapValue.chats.filter { $0.id != id },
            current: bootstrapValue.current
        )
    }

    func renameChat(id: String, title: String) async throws -> [GatewayChatSummary] {
        bootstrapValue.chats
    }

    func selectModel(_ model: String) async throws -> String {
        model
    }
}

/// Drives a gateway stream event by event so a test can assert on mid-turn state.
private nonisolated final class ControlledGatewayStream: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: GatewayEventStream.Continuation?

    var hasSubscriber: Bool {
        lock.lock()
        defer { lock.unlock() }
        return continuation != nil
    }

    func makeStream() -> GatewayEventStream {
        AsyncThrowingStream { continuation in
            lock.lock()
            self.continuation = continuation
            lock.unlock()
        }
    }

    func yield(_ event: GatewayEvent) {
        lock.lock()
        let continuation = continuation
        lock.unlock()
        continuation?.yield(event)
    }

    func finish(throwing error: (any Error)? = nil) {
        lock.lock()
        let continuation = continuation
        self.continuation = nil
        lock.unlock()
        continuation?.finish(throwing: error)
    }
}

/// Proves the Ollama backend is never reached while a gateway profile is active.
private nonisolated struct MockUnusedOllamaService: OllamaServing {
    func serverVersion() async throws -> OllamaServerVersion {
        Issue.record("The Ollama backend must not be used for a gateway profile")
        throw OllamaClientError.invalidResponse
    }

    func models() async throws -> [OllamaModel] {
        Issue.record("The Ollama backend must not be used for a gateway profile")
        throw OllamaClientError.invalidResponse
    }

    func show(model: String) async throws -> OllamaShowResponse {
        throw OllamaClientError.invalidResponse
    }

    func chat(_ request: OllamaChatRequest) -> OllamaChatEventStream {
        AsyncThrowingStream { $0.finish(throwing: OllamaClientError.invalidResponse) }
    }
}
