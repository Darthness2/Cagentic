import Foundation
import Testing
@testable import Cagentic

struct AppModelTests {
    @Test("A successful connection loads models and permits model selection")
    func successfulConnectionAndModelSelection() async throws {
        let service = MockOllamaService(
            version: "0.12.1",
            models: [
                OllamaModel(
                    name: "qwen3:8b",
                    size: 5_100_000_000,
                    details: OllamaModelDetails(
                        family: "qwen3",
                        parameterSize: "8B",
                        quantizationLevel: "Q4_K_M"
                    )
                ),
                OllamaModel(
                    name: "llama3.2:latest",
                    size: 2_018_000_000,
                    details: OllamaModelDetails(
                        family: "llama",
                        parameterSize: "3.2B",
                        quantizationLevel: "Q4_K_M"
                    )
                ),
            ],
            capabilities: [
                "llama3.2:latest": ["completion"],
                "qwen3:8b": ["completion", "thinking"],
            ]
        )
        let tokenStore = InMemoryTokenStore()
        let model = makeAppModel(service: service, tokenStore: tokenStore)

        let result = await model.configureConnection(
            serverURL: " https://ollama.example.com/api/ ",
            serverName: " Studio PC ",
            bearerToken: " test-token "
        )

        guard case .success = result else {
            Issue.record("Expected the mock connection to succeed")
            return
        }
        #expect(model.connectionState == .connected(version: "0.12.1"))
        #expect(model.settings.serverURL == "https://ollama.example.com")
        #expect(model.settings.serverName == "Studio PC")
        #expect(model.settings.hasCompletedOnboarding)
        #expect(model.bearerToken == "test-token")
        let savedToken = try await tokenStore.loadToken()
        #expect(savedToken.isEmpty)
        #expect(model.availableModels.map(\.name) == ["llama3.2:latest", "qwen3:8b"])
        #expect(model.availableModels.first?.capabilities == ["completion"])
        #expect(model.settings.selectedModel == "llama3.2:latest")
        #expect(model.selectedConversation?.modelName == "llama3.2:latest")

        model.selectModel("qwen3:8b")

        #expect(model.settings.selectedModel == "qwen3:8b")
        #expect(model.selectedConversation?.modelName == "qwen3:8b")
    }

    @Test("Onboarding completes only after the connected service is installed")
    func onboardingCompletionWaitsForInstalledConnection() async {
        let repository = SuspendingAppRepository()
        let service = MockOllamaService(
            version: "0.12.1",
            models: [OllamaModel(name: "qwen3:8b")]
        )
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )

        let attempt = Task {
            await model.configureConnection(
                serverURL: "studio-pc.local",
                serverName: "Studio PC",
                bearerToken: ""
            )
        }

        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(1))
        while !(await repository.hasSuspendedSave), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }

        #expect(await repository.hasSuspendedSave)
        #expect(!model.settings.hasCompletedOnboarding)
        #expect(model.connectionState == .connecting)
        #expect(await repository.pendingSnapshot?.settings.hasCompletedOnboarding == true)

        await repository.resumeSuspendedSave()
        guard case .success = await attempt.value else {
            Issue.record("Expected the connection to succeed after persistence resumed")
            return
        }

        #expect(model.connectionState == .connected(version: "0.12.1"))
        #expect(model.settings.hasCompletedOnboarding)
        #expect(model.availableModels.map(\.name) == ["qwen3:8b"])

        // Mirrors the cover's onDisappear cleanup. A completed attempt is no longer
        // cancelable and must leave the installed connection intact.
        model.cancelConnectionAttempt()
        #expect(model.connectionState == .connected(version: "0.12.1"))
    }

    @Test("Installing a model refreshes metadata and selects the new model")
    func pullingModelRefreshesCatalog() async {
        let service = PullingOllamaService(models: ["qwen3:8b"])
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
        await connect(model)

        let result = await model.pullModel(named: "gemma3:4b")

        guard case .success = result else {
            Issue.record("Expected model installation to succeed")
            return
        }
        #expect(model.availableModels.map(\.name) == ["gemma3:4b", "qwen3:8b"])
        #expect(model.activeModelIdentifier == "gemma3:4b")
        await model.refreshModelMetadata()
        let installed = model.availableModels.first(where: { $0.name == "gemma3:4b" })
        #expect(installed?.capabilities == ["completion", "vision"])
        #expect(installed?.contextLength == 8_192)
    }

    @Test("Thinking, content, and completion metrics are reduced into the assistant message")
    func streamedResponseAndMetrics() async {
        let completion = OllamaChatCompletion(
            model: "qwen3:8b",
            doneReason: "stop",
            metrics: OllamaChatMetrics(
                totalDurationNanoseconds: 3_200_000_000,
                promptTokenCount: 12,
                generatedTokenCount: 20,
                generationDurationNanoseconds: 2_000_000_000
            )
        )
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .finished([
                .thinking("Check "),
                .thinking("the details."),
                .content("Hello"),
                .content(", world."),
                .completed(completion),
            ])
        )
        let model = makeAppModel(service: service)
        await connect(model)

        model.send("Explain the result")
        await waitUntil("the response completes") { !model.isGenerating }

        let messages = model.selectedConversation?.messages ?? []
        #expect(messages.count == 2)
        #expect(messages.first?.role == .user)
        #expect(messages.first?.content == "Explain the result")

        guard let response = messages.last else {
            Issue.record("Expected an assistant response")
            return
        }
        #expect(response.role == .assistant)
        #expect(response.modelName == "qwen3:8b")
        #expect(response.thinking == "Check the details.")
        #expect(response.content == "Hello, world.")
        #expect(response.state == .complete)
        #expect(response.errorDescription == nil)
        #expect(
            response.metrics == GenerationMetrics(
                promptTokenCount: 12,
                responseTokenCount: 20,
                totalDurationNanoseconds: 3_200_000_000,
                evaluationDurationNanoseconds: 2_000_000_000
            )
        )
        #expect(response.metrics?.tokensPerSecond == 10)
    }

    @Test("Assistant turns retain the model that generated them")
    func assistantTurnModelProvenance() async {
        let completion = OllamaChatCompletion(
            model: "alpha:latest",
            doneReason: "stop",
            metrics: OllamaChatMetrics()
        )
        let service = MockOllamaService(
            models: [
                OllamaModel(name: "alpha:latest"),
                OllamaModel(name: "beta:latest"),
            ],
            streamResult: .finished([
                .content("Response"),
                .completed(completion),
            ])
        )
        let model = makeAppModel(service: service)
        await connect(model)

        model.send("First")
        await waitUntil("the first response completes") { !model.isGenerating }
        model.selectModel("beta:latest")
        model.send("Second")
        await waitUntil("the second response completes") { !model.isGenerating }

        let modelNames = model.selectedConversation?.messages.compactMap { message in
            message.role == .assistant ? message.modelName : nil
        }
        #expect(modelNames == ["alpha:latest", "beta:latest"])
        #expect(model.selectedConversation?.modelName == "beta:latest")
    }

    @Test("A midstream failure preserves partial output and marks the response failed")
    func midstreamFailurePreservesPartialResponse() async {
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .failed(
                events: [
                    .thinking("Partial reasoning"),
                    .content("Partial answer"),
                ],
                error: .connectionLost
            )
        )
        let model = makeAppModel(service: service)
        await connect(model)

        model.send("Start a long answer")
        await waitUntil("the stream failure is handled") { !model.isGenerating }

        guard let response = model.selectedConversation?.messages.last else {
            Issue.record("Expected a partial assistant response")
            return
        }
        #expect(response.role == .assistant)
        #expect(response.thinking == "Partial reasoning")
        #expect(response.content == "Partial answer")
        #expect(response.state == .failed)
        #expect(response.errorDescription == "The mock stream lost its connection.")
        #expect(response.metrics == nil)
    }

    @Test("Stopping generation keeps partial content and ignores late stream events")
    func cancellationIgnoresLateStreamEvents() async {
        let stream = ControlledChatStream()
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .controlled(stream)
        )
        let model = makeAppModel(service: service)
        await connect(model)

        model.send("Keep streaming")
        await waitUntil("the controlled stream subscribes") { stream.hasSubscriber }
        stream.yield(.thinking("Initial thought"))
        stream.yield(.content("Partial"))
        await waitUntil("partial stream output is applied") {
            model.selectedConversation?.messages.last?.content == "Partial"
        }

        model.stopGenerating()

        #expect(!model.isGenerating)
        #expect(model.selectedConversation?.messages.last?.state == .cancelled)
        #expect(model.selectedConversation?.messages.last?.errorDescription == "Stopped")

        stream.yield(.content(" stale output"))
        stream.yield(
            .completed(
                OllamaChatCompletion(
                    metrics: OllamaChatMetrics(generatedTokenCount: 99)
                )
            )
        )
        stream.finish()
        await Task.yield()
        await Task.yield()

        let response = model.selectedConversation?.messages.last
        #expect(response?.content == "Partial")
        #expect(response?.thinking == "Initial thought")
        #expect(response?.state == .cancelled)
        #expect(response?.metrics == nil)
    }

    @Test("A second send and new chat are rejected while one response is active")
    func overlappingSendIsRejected() async {
        let stream = ControlledChatStream()
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .controlled(stream)
        )
        let model = makeAppModel(service: service)
        await connect(model)
        let originalConversationID = model.selectedConversationID

        model.send("First request")
        await waitUntil("the first stream subscribes") { stream.hasSubscriber }
        model.send("Second request")
        model.createConversation()

        #expect(model.selectedConversationID == originalConversationID)
        #expect(model.selectedConversation?.messages.count == 2)
        #expect(model.selectedConversation?.messages.first?.content == "First request")
        #expect(model.isGenerating)

        model.stopGenerating()
    }

    @Test("Regenerating an assistant turn replaces the stale conversation tail")
    func regenerateOlderAssistantTurn() async {
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .failed(events: [.content("Partial")], error: .connectionLost)
        )
        let model = makeAppModel(service: service)
        await connect(model)

        model.send("First turn")
        await waitUntil("the first response fails") { !model.isGenerating }
        let olderAssistantID = model.selectedConversation?.messages.last?.id

        model.send("Second turn")
        await waitUntil("the second response fails") { !model.isGenerating }
        let finalAssistantID = model.selectedConversation?.messages.last?.id

        if let olderAssistantID, let conversationID = model.selectedConversationID {
            model.retryResponse(messageID: olderAssistantID, in: conversationID)
        }
        await waitUntil("the targeted retry finishes") { !model.isGenerating }
        #expect(model.selectedConversation?.messages.count == 2)
        #expect(model.selectedConversation?.messages.first?.content == "First turn")
        #expect(model.selectedConversation?.messages.last?.id != olderAssistantID)
        #expect(model.selectedConversation?.messages.last?.id != finalAssistantID)
    }

    @Test("Editing a user turn replaces the stale conversation tail and regenerates")
    func editUserTurn() async {
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .failed(events: [.content("Partial")], error: .connectionLost)
        )
        let model = makeAppModel(service: service)
        await connect(model)

        model.send("Original request")
        await waitUntil("the first response fails") { !model.isGenerating }
        let firstUserID = model.selectedConversation?.messages.first?.id

        model.send("Later turn")
        await waitUntil("the second response fails") { !model.isGenerating }

        if let firstUserID, let conversationID = model.selectedConversationID {
            model.editUserMessage(
                messageID: firstUserID,
                in: conversationID,
                content: "Updated request"
            )
        }
        await waitUntil("the edited turn finishes") { !model.isGenerating }

        #expect(model.selectedConversation?.messages.count == 2)
        #expect(model.selectedConversation?.messages.first?.role == .user)
        #expect(model.selectedConversation?.messages.first?.content == "Updated request")
        #expect(model.selectedConversation?.messages.last?.role == .assistant)
    }

    @Test("Editing the first turn preserves a renamed conversation title")
    func editFirstTurnPreservesRenamedTitle() async throws {
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .failed(events: [.content("Partial")], error: .connectionLost)
        )
        let model = makeAppModel(service: service)
        await connect(model)

        model.send("Original request")
        await waitUntil("the original response finishes") { !model.isGenerating }
        let conversationID = try #require(model.selectedConversationID)
        let firstUserID = try #require(model.selectedConversation?.messages.first?.id)
        model.renameConversation(id: conversationID, title: "My research thread")

        model.editUserMessage(
            messageID: firstUserID,
            in: conversationID,
            content: "Updated request"
        )
        await waitUntil("the edited response finishes") { !model.isGenerating }

        #expect(model.selectedConversation?.title == "My research thread")
        #expect(model.selectedConversation?.messages.first?.content == "Updated request")
    }

    @Test("Drafts are conversation scoped and restored from persistence")
    func conversationDraftsAndSelectionPersistence() async throws {
        let repository = InMemoryAppRepository()
        let service = MockOllamaService(models: [OllamaModel(name: "qwen3:8b")])
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
        await connect(model)
        let firstConversationID = try #require(model.selectedConversationID)
        model.draft = "First draft"

        model.createConversation()
        let secondConversationID = try #require(model.selectedConversationID)
        model.draft = "Second draft"

        model.selectConversation(firstConversationID)
        #expect(model.draft == "First draft")
        model.selectConversation(secondConversationID)
        #expect(model.draft == "Second draft")

        await model.flushPersistence()
        let stored = try await repository.load()
        #expect(stored?.selectedConversationID == secondConversationID)
        #expect(stored?.conversationDrafts[firstConversationID]?.text == "First draft")
        #expect(stored?.conversationDrafts[secondConversationID]?.text == "Second draft")

        let restored = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            initialSnapshot: try #require(stored),
            isRestoring: false
        )
        restored.selectConversation(firstConversationID)
        #expect(restored.draft == "First draft")
        restored.selectConversation(secondConversationID)
        #expect(restored.draft == "Second draft")
    }

    @Test("Pinning and archiving remain mutually consistent")
    func conversationPinAndArchiveState() async throws {
        let conversation = Conversation(title: "Keep me")
        let repository = InMemoryAppRepository()
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            initialSnapshot: AppSnapshot(
                conversations: [conversation],
                selectedConversationID: conversation.id
            ),
            isRestoring: false
        )

        model.togglePinned(conversation.id)
        #expect(model.conversation(id: conversation.id)?.isPinned == true)
        model.setArchived(true, conversationID: conversation.id)
        #expect(model.conversation(id: conversation.id)?.isArchived == true)
        #expect(model.conversation(id: conversation.id)?.isPinned == false)

        model.restoreConversations([conversation.id])
        await model.flushPersistence()
        #expect(model.conversation(id: conversation.id)?.isArchived == false)
        #expect(try await repository.load()?.conversations.first?.isArchived == false)
    }

    @Test("Branching an earlier response preserves the source conversation")
    func branchingResponsePreservesSource() async throws {
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .finished([
                .content("Fresh branch"),
                .completed(OllamaChatCompletion(metrics: OllamaChatMetrics())),
            ])
        )
        let model = makeAppModel(service: service)
        await connect(model)
        model.send("First prompt")
        await waitUntil("the source response") { !model.isGenerating }
        model.send("Later prompt")
        await waitUntil("the later response") { !model.isGenerating }

        let source = try #require(model.selectedConversation)
        let responseID = try #require(
            source.messages.first(where: { $0.role == .assistant })?.id
        )
        model.branchAndRetryResponse(messageID: responseID, in: source.id)
        await waitUntil("the branch response") { !model.isGenerating }

        let branch = try #require(model.selectedConversation)
        #expect(branch.id != source.id)
        #expect(branch.branchedFromConversationID == source.id)
        #expect(branch.messages.count == 2)
        #expect(branch.messages.last?.content == "Fresh branch")
        #expect(model.conversation(id: source.id)?.messages.count == source.messages.count)
    }

    @Test("Undo restores a destructively rewritten conversation")
    func undoConversationRewrite() async throws {
        let service = MockOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            streamResult: .finished([
                .content("Response"),
                .completed(OllamaChatCompletion(metrics: OllamaChatMetrics())),
            ])
        )
        let model = makeAppModel(service: service)
        await connect(model)
        model.send("Original")
        await waitUntil("the original response") { !model.isGenerating }
        let original = try #require(model.selectedConversation)
        let userID = try #require(original.messages.first?.id)

        model.editUserMessage(messageID: userID, in: original.id, content: "Replacement")
        await waitUntil("the replacement response") { !model.isGenerating }
        #expect(model.selectedConversation?.messages.first?.content == "Replacement")
        #expect(model.lastConversationUndo != nil)

        model.undoLastConversationRewrite()

        #expect(model.selectedConversation == original)
        #expect(model.lastConversationUndo == nil)
    }

    @Test("Search scans the full transcript without retaining a duplicate corpus")
    func fullTranscriptSearchFindsOldContent() async throws {
        let conversation = Conversation(
            title: "Long chat",
            messages: [
                ChatMessage(role: .user, content: "archived-needle"),
                ChatMessage(
                    role: .assistant,
                    content: String(repeating: "newer context ", count: 2_000)
                ),
            ]
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            initialSnapshot: AppSnapshot(
                conversations: [conversation],
                selectedConversationID: conversation.id
            ),
            isRestoring: false
        )

        let results = try await model.searchConversationSummaries(
            matching: "archived-needle"
        )

        #expect(results.map(\.id) == [conversation.id])
    }

    @Test("A failed candidate connection preserves the working server")
    func failedCandidateConnectionIsTransactional() async {
        let workingService = MockOllamaService(
            version: "1.2.3",
            models: [OllamaModel(name: "qwen3:8b")]
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { endpoint, _ in
                if endpoint.baseURL.host == "working.local" {
                    return workingService
                }
                return FailingConnectionService()
            },
            isRestoring: false
        )

        let firstResult = await model.configureConnection(
            serverURL: "working.local",
            serverName: "Working PC",
            bearerToken: ""
        )
        guard case .success = firstResult else {
            Issue.record("Expected the first connection to succeed")
            return
        }

        let candidateResult = await model.configureConnection(
            serverURL: "unreachable.local",
            serverName: "Wrong PC",
            bearerToken: ""
        )

        guard case .failure = candidateResult else {
            Issue.record("Expected the candidate connection to fail")
            return
        }
        #expect(model.connectionState == .connected(version: "1.2.3"))
        #expect(model.settings.serverURL == "http://working.local:11434")
        #expect(model.settings.serverName == "Working PC")
        #expect(model.availableModels.map(\.name) == ["qwen3:8b"])
    }

    @Test("Switching servers resolves a stale conversation model before sending")
    func switchingServersKeepsDisplayedAndRequestedModelsAligned() async {
        let firstService = MockOllamaService(models: [OllamaModel(name: "first:latest")])
        let secondService = MockOllamaService(
            models: [OllamaModel(name: "second:latest")],
            streamResult: .finished([
                .content("Done"),
                .completed(OllamaChatCompletion(metrics: OllamaChatMetrics())),
            ])
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { endpoint, _ in
                endpoint.baseURL.host == "first.local" ? firstService : secondService
            },
            isRestoring: false
        )

        _ = await model.configureConnection(
            serverURL: "first.local",
            serverName: "First computer",
            bearerToken: ""
        )
        #expect(model.selectedConversation?.modelName == "first:latest")

        _ = await model.configureConnection(
            serverURL: "second.local",
            serverName: "Second computer",
            bearerToken: ""
        )

        #expect(model.selectedConversation?.modelName == "first:latest")
        #expect(model.activeModelIdentifier == "second:latest")

        model.send("Use the visible model")
        await waitUntil("the response from the second server completes") { !model.isGenerating }

        #expect(model.selectedConversation?.modelName == "second:latest")
        #expect(model.selectedConversation?.messages.last?.content == "Done")
    }

    @Test("A server with no models cannot clear an unsent draft")
    func noModelsPreservesDraft() async {
        let availableService = MockOllamaService(models: [OllamaModel(name: "first:latest")])
        let emptyService = MockOllamaService(models: [])
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { endpoint, _ in
                endpoint.baseURL.host == "first.local" ? availableService : emptyService
            },
            isRestoring: false
        )

        _ = await model.configureConnection(
            serverURL: "first.local",
            serverName: "First computer",
            bearerToken: ""
        )
        model.draft = "Keep this draft"

        _ = await model.configureConnection(
            serverURL: "empty.local",
            serverName: "Empty computer",
            bearerToken: ""
        )
        #expect(model.activeModelIdentifier.isEmpty)
        #expect(!model.canSendDraft)

        model.sendDraft()

        #expect(model.draft == "Keep this draft")
        #expect(model.selectedConversation?.messages.isEmpty == true)
    }

    @Test("New chats use the configured default instead of the current chat model")
    func newChatUsesConfiguredDefaultModel() async {
        let service = MockOllamaService(
            models: [
                OllamaModel(name: "first:latest"),
                OllamaModel(name: "second:latest"),
            ],
            streamResult: .finished([
                .content("Done"),
                .completed(OllamaChatCompletion(metrics: OllamaChatMetrics())),
            ])
        )
        let model = makeAppModel(service: service)
        await connect(model)

        model.send("Keep this chat on the first model")
        await waitUntil("the first-model response completes") { !model.isGenerating }
        #expect(model.selectedConversation?.modelName == "first:latest")

        var settings = model.settings
        settings.selectedModel = "second:latest"
        model.updateSettings(settings)
        #expect(model.activeModelIdentifier == "first:latest")

        model.createConversation()

        #expect(model.selectedConversation?.modelName == "second:latest")
    }

    @Test("Settings preserve the saved model while the server model list is unavailable")
    func disconnectedSettingsUpdatePreservesSavedModel() async throws {
        let profile = ServerProfile(
            name: "Studio Mac",
            endpoint: "https://studio.example.com",
            selectedModel: "qwen3:8b"
        )
        let settings = AppSettings(
            serverURL: profile.endpoint,
            serverName: profile.name,
            selectedModel: profile.selectedModel,
            hasCompletedOnboarding: true,
            serverCatalog: ServerProfileCatalog(
                profiles: [profile],
                activeProfileID: profile.id
            )
        )
        let repository = InMemoryAppRepository()
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            initialSnapshot: AppSnapshot(settings: settings),
            isRestoring: false
        )
        var updatedSettings = model.settings
        updatedSettings.hapticsEnabled = false

        model.updateSettings(updatedSettings)
        let persisted = await model.flushPersistence()

        #expect(persisted)
        #expect(model.settings.selectedModel == "qwen3:8b")
        #expect(model.activeServerProfile?.selectedModel == "qwen3:8b")
        #expect(try await repository.load()?.settings.selectedModel == "qwen3:8b")
    }

    @Test("Credential reconciliation failures keep restored chats and allow no-auth refresh")
    func startupCredentialFailureIsIsolatedFromChatRestore() async {
        let profile = ServerProfile(
            name: "Studio Mac",
            endpoint: "https://studio.example.com",
            selectedModel: "qwen3:8b",
            authentication: .none
        )
        let settings = AppSettings(
            serverURL: profile.endpoint,
            serverName: profile.name,
            selectedModel: profile.selectedModel,
            hasCompletedOnboarding: true,
            serverCatalog: ServerProfileCatalog(
                profiles: [profile],
                activeProfileID: profile.id
            )
        )
        let conversation = Conversation(
            title: "Restored chat",
            modelName: "qwen3:8b",
            messages: [ChatMessage(role: .user, content: "Keep this history")]
        )
        let snapshot = AppSnapshot(
            settings: settings,
            conversations: [conversation],
            selectedConversationID: conversation.id
        )
        let model = AppModel(
            repository: InMemoryAppRepository(snapshot: snapshot),
            tokenStore: InMemoryTokenStore(),
            credentialStore: StartupFailingCredentialStore(),
            serviceFactory: OllamaServiceFactory { _, _ in
                MockOllamaService(models: [OllamaModel(name: "qwen3:8b")])
            }
        )

        await model.start()

        #expect(model.connectionState == .connected(version: "test"))
        #expect(model.selectedConversation?.messages.first?.content == "Keep this history")
        #expect(model.notice?.title == "Server credentials need attention")
        #expect(model.notice?.message.contains("Your chats were restored") == true)
        #expect(model.notice?.message.contains("could not be opened") == false)
    }

    @Test("Foreground cancellation resaves a dirty revision")
    func foregroundCancellationDoesNotStrandPersistence() async throws {
        let first = Conversation(title: "First", modelName: "qwen3:8b")
        let second = Conversation(title: "Second", modelName: "qwen3:8b")
        let snapshot = AppSnapshot(
            conversations: [first, second],
            selectedConversationID: first.id
        )
        let repository = SuspendingAppRepository()
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            initialSnapshot: snapshot,
            isRestoring: false
        )

        model.selectConversation(second.id)
        model.requestPersistenceFlush()
        await waitUntil("the inactive flush suspends") {
            await repository.hasSuspendedSave
        }

        model.cancelPersistenceFlush()
        await waitUntil("the replacement flush persists") {
            await repository.saveAttemptCount >= 2
        }
        await repository.resumeSuspendedSave()
        await Task.yield()

        #expect(try await repository.load()?.selectedConversationID == second.id)
    }

    @Test("A failed snapshot load disables persistence to protect unreadable data")
    func corruptSnapshotNeverGetsOverwritten() async {
        let repository = LoadFailingAppRepository()
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore()
        )

        await model.start()
        var settings = model.settings
        settings.hapticsEnabled = false
        model.updateSettings(settings)
        let didPersist = await model.flushPersistence()

        #expect(!didPersist)
        #expect(await repository.saveAttemptCount == 0)
        #expect(model.notice?.title == "Saved data needs recovery")
        #expect(model.notice?.message.contains("saving is paused") == true)
    }

    @Test("Canceling a connection attempt restores the prior credential and server")
    func cancelledConnectionRollsBackCredential() async throws {
        let service = MockOllamaService(models: [OllamaModel(name: "qwen3:8b")])
        let credentialStore = SuspendingServerCredentialStore(suspendOnMutationNumber: 2)
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: credentialStore,
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )

        _ = await model.configureConnection(
            serverURL: "https://working.local",
            serverName: "Working computer",
            bearerToken: "old-token"
        )
        let workingProfileID = try #require(model.activeServerProfile?.id)

        let candidate = Task {
            await model.configureConnection(
                serverURL: "https://candidate.local",
                serverName: "Candidate computer",
                bearerToken: "new-token"
            )
        }
        await waitForSuspendedMutation(in: credentialStore)

        model.cancelConnectionAttempt()
        candidate.cancel()
        await credentialStore.resumeSuspendedMutation()
        let result = await candidate.value

        guard case .failure(let error) = result else {
            Issue.record("Expected the canceled candidate connection to fail")
            return
        }
        #expect(error is CancellationError)
        #expect(model.settings.serverURL == "https://working.local")
        #expect(model.settings.serverName == "Working computer")
        #expect(model.bearerToken == "old-token")
        let storedCredential = try await credentialStore.loadCredential(for: workingProfileID)
        #expect(storedCredential == .bearerToken("old-token"))
    }

    @Test("A new connection waits for a canceled attempt to finish rolling back")
    func reconnectAfterCancellationWaitsForRollback() async {
        let service = MockOllamaService(models: [OllamaModel(name: "qwen3:8b")])
        let credentialStore = SuspendingServerCredentialStore(suspendOnMutationNumber: 2)
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: credentialStore,
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )

        _ = await model.configureConnection(
            serverURL: "https://working.local",
            serverName: "Working computer",
            bearerToken: "old-token"
        )
        let canceledAttempt = Task {
            await model.configureConnection(
                serverURL: "https://candidate.local",
                serverName: "Canceled computer",
                bearerToken: "canceled-token"
            )
        }
        await waitForSuspendedMutation(in: credentialStore)

        model.cancelConnectionAttempt()
        canceledAttempt.cancel()
        let replacementAttempt = Task {
            await model.configureConnection(
                serverURL: "https://replacement.local",
                serverName: "Replacement computer",
                bearerToken: "replacement-token"
            )
        }
        await credentialStore.resumeSuspendedMutation()

        guard case .failure = await canceledAttempt.value else {
            Issue.record("Expected the canceled connection to fail")
            return
        }
        guard case .success = await replacementAttempt.value else {
            Issue.record("Expected the replacement connection to succeed after rollback")
            return
        }
        #expect(model.settings.serverURL == "https://replacement.local")
        #expect(model.settings.serverName == "Replacement computer")
        #expect(model.bearerToken == "replacement-token")
    }

    @Test("A transient persistence failure retries the latest snapshot")
    func persistenceRetriesLatestSnapshot() async throws {
        let repository = FlakyAppRepository(failuresBeforeSuccess: 1)
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            isRestoring: false
        )
        var settings = model.settings
        settings.hapticsEnabled = false

        model.updateSettings(settings)

        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(3))
        while await repository.saveAttemptCount < 2, clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(20))
        }

        #expect(await repository.saveAttemptCount == 2)
        let stored = try await repository.load()
        #expect(stored?.settings.hapticsEnabled == false)
    }

    private func makeAppModel(
        service: MockOllamaService,
        tokenStore: InMemoryTokenStore = InMemoryTokenStore()
    ) -> AppModel {
        AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: tokenStore,
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
    }

    private func connect(_ model: AppModel) async {
        let result = await model.configureConnection(
            serverURL: "studio-pc.local",
            serverName: "Studio PC",
            bearerToken: ""
        )
        if case .failure(let error) = result {
            Issue.record("Expected the mock connection to succeed: \(error)")
        }
    }

    private func waitUntil(
        _ description: String,
        condition: () -> Bool
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(1))
        while !condition(), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(condition(), "Timed out waiting for \(description)")
    }

    private func waitUntil(
        _ description: String,
        condition: () async -> Bool
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(1))
        while !(await condition()), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(await condition(), "Timed out waiting for \(description)")
    }

    private func waitForSuspendedMutation(
        in credentialStore: SuspendingServerCredentialStore
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(1))
        while !(await credentialStore.hasSuspendedMutation), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(await credentialStore.hasSuspendedMutation)
    }
}

private nonisolated struct MockOllamaService: OllamaServing {
    var version = "test"
    var models: [OllamaModel]
    var capabilities: [String: [String]] = [:]
    var streamResult = MockStreamResult.finished([])

    func serverVersion() async throws -> OllamaServerVersion {
        OllamaServerVersion(version: version)
    }

    func models() async throws -> [OllamaModel] {
        models
    }

    func show(model: String) async throws -> OllamaShowResponse {
        OllamaShowResponse(
            license: nil,
            modelfile: nil,
            parameters: nil,
            template: nil,
            system: nil,
            modifiedAt: nil,
            details: nil,
            modelInfo: nil,
            projectorInfo: nil,
            capabilities: capabilities[model] ?? []
        )
    }

    func chat(_ request: OllamaChatRequest) -> OllamaChatEventStream {
        streamResult.makeStream()
    }
}

private nonisolated final class PullingOllamaService: OllamaServing, @unchecked Sendable {
    private let lock = NSLock()
    private var installedModels: [String]

    init(models: [String]) {
        installedModels = models
    }

    func serverVersion() async throws -> OllamaServerVersion {
        OllamaServerVersion(version: "test")
    }

    func models() async throws -> [OllamaModel] {
        lock.withLock {
            installedModels.map { OllamaModel(name: $0) }
        }
    }

    func show(model: String) async throws -> OllamaShowResponse {
        OllamaShowResponse(
            license: nil,
            modelfile: nil,
            parameters: nil,
            template: nil,
            system: nil,
            modifiedAt: nil,
            details: OllamaModelDetails(
                family: model.hasPrefix("gemma") ? "gemma" : "qwen",
                parameterSize: model.hasPrefix("gemma") ? "4B" : "8B",
                quantizationLevel: "Q4_K_M"
            ),
            modelInfo: ["general.context_length": .integer(8_192)],
            projectorInfo: nil,
            capabilities: model.hasPrefix("gemma")
                ? ["completion", "vision"]
                : ["completion"]
        )
    }

    func pull(model: String) async throws -> OllamaPullResponse {
        lock.withLock {
            if !installedModels.contains(model) {
                installedModels.append(model)
            }
        }
        return OllamaPullResponse(status: "success")
    }

    func chat(_ request: OllamaChatRequest) -> OllamaChatEventStream {
        AsyncThrowingStream { continuation in
            continuation.finish()
        }
    }
}

private nonisolated enum MockStreamResult: Sendable {
    case finished([OllamaChatEvent])
    case failed(events: [OllamaChatEvent], error: MockStreamError)
    case controlled(ControlledChatStream)

    func makeStream() -> OllamaChatEventStream {
        switch self {
        case .finished(let events):
            AsyncThrowingStream { continuation in
                for event in events {
                    continuation.yield(event)
                }
                continuation.finish()
            }
        case .failed(let events, let error):
            AsyncThrowingStream { continuation in
                for event in events {
                    continuation.yield(event)
                }
                continuation.finish(throwing: error)
            }
        case .controlled(let stream):
            stream.makeStream()
        }
    }
}

private nonisolated enum MockStreamError: LocalizedError, Sendable {
    case connectionLost

    var errorDescription: String? {
        "The mock stream lost its connection."
    }
}

private nonisolated struct FailingConnectionService: OllamaServing {
    func serverVersion() async throws -> OllamaServerVersion {
        throw MockStreamError.connectionLost
    }

    func models() async throws -> [OllamaModel] {
        throw MockStreamError.connectionLost
    }

    func show(model: String) async throws -> OllamaShowResponse {
        throw MockStreamError.connectionLost
    }

    func chat(_ request: OllamaChatRequest) -> OllamaChatEventStream {
        AsyncThrowingStream { continuation in
            continuation.finish(throwing: MockStreamError.connectionLost)
        }
    }
}

private actor SuspendingServerCredentialStore: ServerCredentialStoring {
    private var credentials: [ServerProfileID: ServerCredential] = [:]
    private let suspendOnMutationNumber: Int
    private var mutationNumber = 0
    private var suspendedContinuation: CheckedContinuation<Void, Never>?

    init(suspendOnMutationNumber: Int) {
        self.suspendOnMutationNumber = suspendOnMutationNumber
    }

    var hasSuspendedMutation: Bool {
        suspendedContinuation != nil
    }

    func loadCredential(for profileID: ServerProfileID) async throws -> ServerCredential? {
        credentials[profileID]
    }

    func saveCredential(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws {
        await suspendIfNeeded()
        credentials[profileID] = credential
    }

    func saveCredentialIfAbsent(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws -> Bool {
        guard credentials[profileID] == nil else { return false }
        await suspendIfNeeded()
        credentials[profileID] = credential
        return true
    }

    func removeCredential(for profileID: ServerProfileID) async throws {
        await suspendIfNeeded()
        credentials[profileID] = nil
    }

    func resumeSuspendedMutation() {
        suspendedContinuation?.resume()
        suspendedContinuation = nil
    }

    private func suspendIfNeeded() async {
        mutationNumber += 1
        if mutationNumber == suspendOnMutationNumber {
            await withCheckedContinuation { continuation in
                suspendedContinuation = continuation
            }
        }
    }
}

private actor FlakyAppRepository: AppPersisting {
    private let failuresBeforeSuccess: Int
    private var attempts = 0
    private var snapshot: AppSnapshot?

    init(failuresBeforeSuccess: Int) {
        self.failuresBeforeSuccess = failuresBeforeSuccess
    }

    var saveAttemptCount: Int {
        attempts
    }

    func load() async throws -> AppSnapshot? {
        snapshot
    }

    func save(_ snapshot: AppSnapshot, revision: Int) async throws {
        attempts += 1
        if attempts <= failuresBeforeSuccess {
            throw PersistenceTestError.writeFailed
        }
        self.snapshot = snapshot
    }
}

private actor SuspendingAppRepository: AppPersisting {
    private var snapshot: AppSnapshot?
    private var saveNumber = 0
    private var suspendedContinuation: CheckedContinuation<Void, Never>?
    private(set) var pendingSnapshot: AppSnapshot?

    var hasSuspendedSave: Bool {
        suspendedContinuation != nil
    }

    var saveAttemptCount: Int {
        saveNumber
    }

    func load() async throws -> AppSnapshot? {
        snapshot
    }

    func save(_ snapshot: AppSnapshot, revision: Int) async throws {
        saveNumber += 1
        if saveNumber == 1 {
            pendingSnapshot = snapshot
            await withCheckedContinuation { continuation in
                suspendedContinuation = continuation
            }
            try Task.checkCancellation()
            pendingSnapshot = nil
        }
        self.snapshot = snapshot
    }

    func resumeSuspendedSave() {
        suspendedContinuation?.resume()
        suspendedContinuation = nil
    }
}

private actor StartupFailingCredentialStore: ServerCredentialStoring {
    func loadCredential(for profileID: ServerProfileID) async throws -> ServerCredential? {
        throw PersistenceTestError.credentialUnavailable
    }

    func saveCredential(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws {
        throw PersistenceTestError.credentialUnavailable
    }

    func saveCredentialIfAbsent(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws -> Bool {
        throw PersistenceTestError.credentialUnavailable
    }

    func removeCredential(for profileID: ServerProfileID) async throws {
        throw PersistenceTestError.credentialUnavailable
    }

    func reconcileCredentials(validProfileIDs: Set<ServerProfileID>) async throws {
        throw PersistenceTestError.credentialUnavailable
    }
}

private actor LoadFailingAppRepository: AppPersisting {
    private var saveAttempts = 0

    var saveAttemptCount: Int {
        saveAttempts
    }

    func load() async throws -> AppSnapshot? {
        throw PersistenceTestError.unreadableSnapshot
    }

    func save(_ snapshot: AppSnapshot, revision: Int) async throws {
        saveAttempts += 1
    }
}

private nonisolated enum PersistenceTestError: LocalizedError, Sendable {
    case writeFailed
    case credentialUnavailable
    case unreadableSnapshot

    var errorDescription: String? {
        switch self {
        case .writeFailed:
            "The mock write failed."
        case .credentialUnavailable:
            "The mock credential store is unavailable."
        case .unreadableSnapshot:
            "The mock snapshot is unreadable."
        }
    }
}

private nonisolated final class ControlledChatStream: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: OllamaChatEventStream.Continuation?

    var hasSubscriber: Bool {
        lock.lock()
        defer { lock.unlock() }
        return continuation != nil
    }

    func makeStream() -> OllamaChatEventStream {
        AsyncThrowingStream { continuation in
            lock.lock()
            self.continuation = continuation
            lock.unlock()
        }
    }

    func yield(_ event: OllamaChatEvent) {
        lock.lock()
        let continuation = continuation
        lock.unlock()
        continuation?.yield(event)
    }

    func finish() {
        lock.lock()
        let continuation = continuation
        lock.unlock()
        continuation?.finish()
    }
}
