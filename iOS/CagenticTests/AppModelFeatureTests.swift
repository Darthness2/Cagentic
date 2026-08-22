@preconcurrency import ImageIO
import Foundation
import Testing
import UniformTypeIdentifiers
@testable import Cagentic

struct AppModelFeatureTests {
    @Test("Text attachments stay out of snapshots and are framed in the Ollama request")
    func textAttachmentRequestPreparation() async throws {
        let fixture = try FeatureAttachmentFixture(fileName: "project-notes.txt")
        defer { fixture.remove() }
        let referenceText = "A private local reference with the answer forty-two."
        try Data(referenceText.utf8).write(to: fixture.fileURL)

        let recorder = ChatRequestRecorder()
        let service = FeatureOllamaService(
            models: [OllamaModel(name: "qwen3:8b")],
            recorder: recorder
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            attachmentStore: AttachmentStore(rootDirectory: fixture.storageDirectory),
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
        await connect(model)

        let importResult = await model.importFileAttachments(
            from: [AttachmentImportSource(url: fixture.fileURL)]
        )
        guard case .success = importResult else {
            Issue.record("Expected the text attachment to import")
            return
        }
        model.draft = "Summarize my notes"
        model.sendDraft()
        await waitUntil("the attachment response completes") { !model.isGenerating }

        let request = try #require(recorder.lastRequest)
        let userMessage = try #require(request.messages.last(where: { $0.role == .user }))
        #expect(userMessage.images == nil)
        #expect(userMessage.content.contains("Summarize my notes"))
        #expect(userMessage.content.contains("Begin attached file: project-notes.txt"))
        #expect(userMessage.content.contains(referenceText))

        let persistedMessage = try #require(
            model.selectedConversation?.messages.first(where: { $0.role == .user })
        )
        let encodedMessage = String(
            decoding: try JSONEncoder().encode(persistedMessage),
            as: UTF8.self
        )
        #expect(persistedMessage.attachments.count == 1)
        #expect(!encodedMessage.contains(referenceText))
        #expect(!encodedMessage.lowercased().contains("base64"))
    }

    @Test("Editing a user message can remove an unavailable attachment")
    func editUserMessageRemovesAttachment() async throws {
        let fixture = try FeatureAttachmentFixture(fileName: "temporary-context.txt")
        defer { fixture.remove() }
        try Data("Temporary context".utf8).write(to: fixture.fileURL)

        let service = FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            attachmentStore: AttachmentStore(rootDirectory: fixture.storageDirectory),
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
        await connect(model)

        let importResult = await model.importFileAttachments(
            from: [AttachmentImportSource(url: fixture.fileURL)]
        )
        guard case .success = importResult else {
            Issue.record("Expected the attachment to import")
            return
        }
        model.draft = "Use this context"
        model.sendDraft()
        await waitUntil("the original response completes") { !model.isGenerating }

        let conversationID = try #require(model.selectedConversationID)
        let userMessageID = try #require(
            model.selectedConversation?.messages.first(where: { $0.role == .user })?.id
        )
        model.editUserMessage(
            messageID: userMessageID,
            in: conversationID,
            content: "Continue without the file",
            attachments: []
        )
        await waitUntil("the edited response completes") { !model.isGenerating }

        let editedMessage = try #require(
            model.selectedConversation?.messages.first(where: { $0.role == .user })
        )
        #expect(editedMessage.content == "Continue without the file")
        #expect(editedMessage.attachments.isEmpty)
    }

    @Test("Vision attachments are optimized and encoded only in the outgoing request")
    func visionAttachmentRequestPreparation() async throws {
        let fixture = try FeatureAttachmentFixture(fileName: "photo.png")
        defer { fixture.remove() }
        let sourceImage = try featurePNG(width: 20, height: 12)

        let recorder = ChatRequestRecorder()
        let service = FeatureOllamaService(
            models: [OllamaModel(name: "gemma3:4b")],
            capabilities: ["gemma3:4b": ["completion", "vision"]],
            recorder: recorder
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            attachmentStore: AttachmentStore(rootDirectory: fixture.storageDirectory),
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
        await connect(model)

        let result = await model.importPhotoAttachments(
            from: [
                AttachmentPhotoDataSource(
                    data: sourceImage,
                    displayName: "Desk photo.png",
                    declaredContentTypeIdentifier: UTType.png.identifier
                ),
            ]
        )
        guard case .success = result else {
            Issue.record("Expected the photo attachment to import")
            return
        }
        model.draft = "What is shown here?"
        model.sendDraft()
        await waitUntil("the vision response completes") { !model.isGenerating }

        let request = try #require(recorder.lastRequest)
        let userMessage = try #require(request.messages.last(where: { $0.role == .user }))
        let base64 = try #require(userMessage.images?.first)
        let imageData = try #require(Data(base64Encoded: base64))
        #expect(!imageData.isEmpty)
        #expect(userMessage.content == "What is shown here?")

        let persistedMessage = try #require(
            model.selectedConversation?.messages.first(where: { $0.role == .user })
        )
        let encodedMessage = try JSONEncoder().encode(persistedMessage)
        #expect(encodedMessage.count < imageData.count)
        #expect(persistedMessage.attachments.first?.kind == .photo)
    }

    @Test("A known non-vision model preserves an image draft instead of silently sending")
    func nonVisionModelPreservesImageDraft() async throws {
        let fixture = try FeatureAttachmentFixture(fileName: "photo.png")
        defer { fixture.remove() }
        try featurePNG(width: 16, height: 16).write(to: fixture.fileURL)

        let recorder = ChatRequestRecorder()
        let service = FeatureOllamaService(
            models: [OllamaModel(name: "text-only:latest")],
            capabilities: ["text-only:latest": ["completion"]],
            recorder: recorder
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            attachmentStore: AttachmentStore(rootDirectory: fixture.storageDirectory),
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
        await connect(model)
        await waitUntil("model capabilities load") {
            model.availableModels.first?.capabilities == ["completion"]
        }

        let importResult = await model.importFileAttachments(
            from: [
                AttachmentImportSource(
                    url: fixture.fileURL,
                    declaredContentTypeIdentifier: UTType.png.identifier
                ),
            ]
        )
        guard case .success = importResult else {
            Issue.record("Expected Files import to retain the selected image")
            return
        }
        model.draft = "Keep this prompt"
        model.sendDraft()

        #expect(model.draft == "Keep this prompt")
        #expect(model.pendingAttachments.count == 1)
        #expect(model.notice?.title == "Choose a vision model")
        #expect(recorder.lastRequest == nil)
    }

    @Test("Server switching restores profile-specific credentials and model choices")
    func multiServerSelectionIsProfileScoped() async throws {
        let firstService = FeatureOllamaService(
            version: "1.0",
            models: [
                OllamaModel(name: "alpha:latest"),
                OllamaModel(name: "beta:latest"),
            ]
        )
        let secondService = FeatureOllamaService(
            version: "2.0",
            models: [OllamaModel(name: "gamma:latest")]
        )
        let credentials = InMemoryServerCredentialStore()
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: credentials,
            serviceFactory: OllamaServiceFactory { endpoint, _ in
                endpoint.baseURL.host == "first.local" ? firstService : secondService
            },
            isRestoring: false
        )

        _ = await model.addServerConnection(
            serverURL: "first.local",
            serverName: "First Mac",
            bearerToken: "first-secret"
        )
        let firstID = try #require(model.activeServerProfile?.id)
        model.selectModel("beta:latest")

        _ = await model.addServerConnection(
            serverURL: "second.local",
            serverName: "Second Mac",
            bearerToken: "second-secret"
        )
        let secondID = try #require(model.activeServerProfile?.id)
        #expect(firstID != secondID)
        #expect(model.serverProfiles.count == 2)
        #expect(model.settings.selectedModel == "gamma:latest")

        guard case .success = await model.activateServer(firstID) else {
            Issue.record("Expected the first server to reactivate")
            return
        }
        #expect(model.activeServerProfile?.id == firstID)
        #expect(model.settings.selectedModel == "beta:latest")
        #expect(model.bearerToken == "first-secret")

        guard case .success = await model.activateServer(secondID) else {
            Issue.record("Expected the second server to reactivate")
            return
        }
        #expect(model.activeServerProfile?.id == secondID)
        #expect(model.settings.selectedModel == "gamma:latest")
        #expect(model.bearerToken == "second-secret")
        #expect(try await credentials.loadCredential(for: firstID) == .bearerToken("first-secret"))
        #expect(try await credentials.loadCredential(for: secondID) == .bearerToken("second-secret"))
    }

    @Test("A failed server activation leaves the working profile active")
    func failedServerActivationIsTransactional() async throws {
        let firstService = SwitchableOllamaService(
            version: "1.0",
            models: [OllamaModel(name: "alpha:latest")]
        )
        let secondService = FeatureOllamaService(
            version: "2.0",
            models: [OllamaModel(name: "gamma:latest")]
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            serviceFactory: OllamaServiceFactory { endpoint, _ in
                if endpoint.baseURL.host == "first.local" {
                    return firstService
                }
                return secondService
            },
            isRestoring: false
        )

        _ = await model.addServerConnection(
            serverURL: "first.local",
            serverName: "First Mac",
            bearerToken: "first-secret"
        )
        let firstID = try #require(model.activeServerProfile?.id)
        _ = await model.addServerConnection(
            serverURL: "second.local",
            serverName: "Second Mac",
            bearerToken: "second-secret"
        )
        let secondID = try #require(model.activeServerProfile?.id)
        firstService.shouldFail = true

        guard case .failure = await model.activateServer(firstID) else {
            Issue.record("Expected the unavailable server activation to fail")
            return
        }

        #expect(model.activeServerProfile?.id == secondID)
        #expect(model.settings.serverName == "Second Mac")
        #expect(model.settings.selectedModel == "gamma:latest")
        #expect(model.bearerToken == "second-secret")
        #expect(model.connectionState == ConnectionState.connected(version: "2.0"))
    }

    @Test("Activating a bearer server with no credential requires token re-entry")
    func missingCredentialNeverDowngradesServerAuthentication() async {
        let currentProfile = ServerProfile(
            name: "Current Mac",
            endpoint: "https://current.example.com",
            selectedModel: "alpha:latest"
        )
        let securedProfile = ServerProfile(
            name: "Secured Mac",
            endpoint: "https://secured.example.com",
            selectedModel: "qwen3:8b",
            authentication: .bearerToken
        )
        let settings = AppSettings(
            serverURL: currentProfile.endpoint,
            serverName: currentProfile.name,
            selectedModel: currentProfile.selectedModel,
            hasCompletedOnboarding: true,
            serverCatalog: ServerProfileCatalog(
                profiles: [currentProfile, securedProfile],
                activeProfileID: currentProfile.id
            )
        )
        let recorder = ConnectionFactoryRecorder()
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: InMemoryServerCredentialStore(),
            serviceFactory: OllamaServiceFactory { endpoint, bearerToken in
                recorder.record(endpoint: endpoint.displayAddress, bearerToken: bearerToken)
                return FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
            },
            initialSnapshot: AppSnapshot(settings: settings),
            isRestoring: false
        )

        let result = await model.activateServer(securedProfile.id)

        guard case .failure(let error) = result else {
            Issue.record("Expected the missing server credential to require re-entry")
            return
        }
        #expect(error as? OllamaClientError == .credentialReentryRequired)
        #expect(recorder.calls.isEmpty)
        #expect(model.activeServerProfile?.id == currentProfile.id)
        #expect(
            model.serverProfiles.first(where: { $0.id == securedProfile.id })?.authentication
                == .bearerToken
        )
    }

    @Test("Testing a saved server reports availability without changing the active connection")
    func testingSavedServerIsNonDestructive() async throws {
        let firstService = FeatureOllamaService(
            version: "1.0",
            models: [OllamaModel(name: "alpha:latest")]
        )
        let secondService = FeatureOllamaService(
            version: "2.4.1",
            models: [
                OllamaModel(name: "gamma:latest"),
                OllamaModel(name: "delta:latest"),
            ]
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: InMemoryServerCredentialStore(),
            serviceFactory: OllamaServiceFactory { endpoint, _ in
                endpoint.baseURL.host == "first.local" ? firstService : secondService
            },
            isRestoring: false
        )

        _ = await model.addServerConnection(
            serverURL: "first.local",
            serverName: "First Mac",
            bearerToken: ""
        )
        let firstID = try #require(model.activeServerProfile?.id)
        _ = await model.addServerConnection(
            serverURL: "second.local",
            serverName: "Second Mac",
            bearerToken: ""
        )
        let secondID = try #require(model.activeServerProfile?.id)
        #expect(secondID != firstID)

        guard case .success(let test) = await model.testServerConnection(firstID) else {
            Issue.record("Expected the saved server test to succeed")
            return
        }

        #expect(test.profileID == firstID)
        #expect(test.serverVersion == "1.0")
        #expect(test.modelCount == 1)
        #expect(model.activeServerProfile?.id == secondID)
        #expect(model.settings.serverName == "Second Mac")
        #expect(model.connectionState == .connected(version: "2.4.1"))
    }

    @Test("Removing the final server returns the app to required onboarding")
    func deletingLastServerResetsConnectionSetup() async throws {
        let credentialStore = InMemoryServerCredentialStore()
        let service = FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: credentialStore,
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
        _ = await model.addServerConnection(
            serverURL: "studio.local",
            serverName: "Studio Mac",
            bearerToken: "private-token"
        )
        let profileID = try #require(model.activeServerProfile?.id)

        guard case .success = await model.deleteServer(profileID) else {
            Issue.record("Expected the final server to be removed")
            return
        }

        #expect(model.serverProfiles.isEmpty)
        #expect(model.activeServerProfile == nil)
        #expect(model.connectionState == .notConfigured)
        #expect(!model.settings.hasCompletedOnboarding)
        #expect(model.availableModels.isEmpty)
        #expect(try await credentialStore.loadCredential(for: profileID) == nil)
    }

    @Test("Removing a migrated server clears both copies of its credential")
    func deletingInactiveLegacyServerClearsMigratedCredentials() async throws {
        let legacyProfile = ServerProfile(
            id: .legacyMigration,
            name: "Old Mac",
            endpoint: "http://old-mac.local:11434",
            authentication: .bearerToken
        )
        let modernProfile = ServerProfile(
            name: "Studio Mac",
            endpoint: "http://studio.local:11434",
            selectedModel: "qwen3:8b"
        )
        let settings = AppSettings(
            serverURL: modernProfile.endpoint,
            serverName: modernProfile.name,
            selectedModel: modernProfile.selectedModel,
            hasCompletedOnboarding: true,
            serverCatalog: ServerProfileCatalog(
                profiles: [legacyProfile, modernProfile],
                activeProfileID: modernProfile.id
            )
        )
        let tokenStore = InMemoryTokenStore(token: "legacy-secret")
        let credentialStore = InMemoryServerCredentialStore()
        try await credentialStore.saveCredential(
            .bearerToken("legacy-secret"),
            for: .legacyMigration
        )
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: tokenStore,
            credentialStore: credentialStore,
            initialSnapshot: AppSnapshot(settings: settings),
            isRestoring: false
        )

        guard case .success = await model.deleteServer(.legacyMigration) else {
            Issue.record("Expected the migrated server to be removed")
            return
        }

        #expect(model.activeServerProfile?.id == modernProfile.id)
        #expect(try await credentialStore.loadCredential(for: .legacyMigration) == nil)
        #expect(try await tokenStore.loadToken() == "")
    }

    private func connect(_ model: AppModel) async {
        let result = await model.configureConnection(
            serverURL: "studio-pc.local",
            serverName: "Studio PC",
            bearerToken: ""
        )
        if case .failure(let error) = result {
            Issue.record("Expected the feature test connection to succeed: \(error)")
        }
    }

    private func waitUntil(
        _ description: String,
        condition: () -> Bool
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while !condition(), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(condition(), "Timed out waiting for \(description)")
    }

    @Test("Modern profiles never inherit the retired global token")
    func modernProfileDoesNotUseLegacyTokenFallback() async throws {
        let profile = ServerProfile(
            name: "Modern Mac",
            endpoint: "http://modern.local:11434",
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
        let legacyStore = InMemoryTokenStore(token: "must-not-leak")
        let model = AppModel(
            repository: InMemoryAppRepository(snapshot: AppSnapshot(settings: settings)),
            tokenStore: legacyStore,
            credentialStore: InMemoryServerCredentialStore(),
            serviceFactory: OllamaServiceFactory { _, _ in
                FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
            }
        )

        await model.start()

        #expect(model.bearerToken.isEmpty)
        #expect((try await legacyStore.loadToken()).isEmpty)
    }

    @Test("Changing an endpoint rotates its credential identity after a durable save")
    func endpointChangeRotatesCredentialIdentity() async throws {
        let credentials = InMemoryServerCredentialStore()
        let legacyStore = InMemoryTokenStore(token: "retired-global-token")
        let service = FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: legacyStore,
            credentialStore: credentials,
            serviceFactory: OllamaServiceFactory { _, _ in service },
            isRestoring: false
        )
        _ = await model.addServerConnection(
            serverURL: "first.local",
            serverName: "Studio",
            bearerToken: "first-secret"
        )
        let firstID = try #require(model.activeServerProfile?.id)

        guard case .success = await model.updateConnection(
            profileID: firstID,
            serverURL: "second.local",
            serverName: "Studio",
            credentialUpdate: .replaceBearerToken("second-secret")
        ) else {
            Issue.record("Expected the endpoint edit to succeed")
            return
        }

        let secondID = try #require(model.activeServerProfile?.id)
        #expect(secondID != firstID)
        #expect(try await credentials.loadCredential(for: firstID) == nil)
        #expect(
            try await credentials.loadCredential(for: secondID)
                == .bearerToken("second-secret")
        )
        #expect(try await legacyStore.loadToken() == "retired-global-token")
    }

    @Test("A saved credential is never reused to verify a different endpoint")
    func endpointChangeRequiresExplicitCredentialIntent() async throws {
        let recorder = ConnectionFactoryRecorder()
        let credentials = InMemoryServerCredentialStore()
        let service = FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: credentials,
            serviceFactory: OllamaServiceFactory { endpoint, bearerToken in
                recorder.record(endpoint: endpoint.displayAddress, bearerToken: bearerToken)
                return service
            },
            isRestoring: false
        )
        _ = await model.addServerConnection(
            serverURL: "https://first.example.com",
            serverName: "Studio",
            bearerToken: "first-secret"
        )
        let firstID = try #require(model.activeServerProfile?.id)
        recorder.reset()

        let result = await model.updateConnection(
            profileID: firstID,
            serverURL: "https://second.example.com",
            serverName: "Studio",
            credentialUpdate: .preserveExisting
        )

        guard case .failure(let error) = result else {
            Issue.record("Expected endpoint verification to require an explicit credential choice")
            return
        }
        #expect(error as? OllamaClientError == .credentialReentryRequired)
        #expect(recorder.calls.isEmpty)
        #expect(model.activeServerProfile?.id == firstID)
        #expect(model.activeServerProfile?.endpoint == "https://first.example.com")
        #expect(
            try await credentials.loadCredential(for: firstID)
                == .bearerToken("first-secret")
        )
    }

    @Test("A failed durable save restores a server edit and its prior credential")
    func serverEditRollsBackWhenPersistenceFails() async throws {
        let profile = ServerProfile(
            name: "Studio",
            endpoint: "https://first.example.com",
            selectedModel: "qwen3:8b",
            authentication: .bearerToken
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
        let credentials = InMemoryServerCredentialStore(
            credentials: [profile.id: .bearerToken("first-secret")]
        )
        let model = AppModel(
            repository: AlwaysFailingFeatureRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: credentials,
            serviceFactory: OllamaServiceFactory { _, _ in
                FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
            },
            initialSnapshot: AppSnapshot(settings: settings),
            isRestoring: false
        )

        let result = await model.updateConnection(
            profileID: profile.id,
            serverURL: "https://second.example.com",
            serverName: "New studio",
            credentialUpdate: .replaceBearerToken("second-secret")
        )

        guard case .failure(let error) = result else {
            Issue.record("Expected the server edit to fail when persistence fails")
            return
        }
        #expect(error is AppModelOperationError)
        #expect(model.activeServerProfile == profile)
        #expect(try await credentials.loadCredential(for: profile.id) == .bearerToken("first-secret"))
    }

    @Test("A failed durable save restores a deleted server")
    func serverDeletionRollsBackWhenPersistenceFails() async throws {
        let profile = ServerProfile(
            name: "Studio",
            endpoint: "https://studio.example.com",
            authentication: .bearerToken
        )
        let settings = AppSettings(
            serverURL: profile.endpoint,
            serverName: profile.name,
            hasCompletedOnboarding: true,
            serverCatalog: ServerProfileCatalog(
                profiles: [profile],
                activeProfileID: profile.id
            )
        )
        let credentials = InMemoryServerCredentialStore(
            credentials: [profile.id: .bearerToken("studio-secret")]
        )
        let model = AppModel(
            repository: AlwaysFailingFeatureRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: credentials,
            initialSnapshot: AppSnapshot(settings: settings),
            isRestoring: false
        )

        let result = await model.deleteServer(profile.id)

        guard case .failure(let error) = result else {
            Issue.record("Expected deletion to fail when persistence fails")
            return
        }
        #expect(error is AppModelOperationError)
        #expect(model.activeServerProfile == profile)
        #expect(model.settings.hasCompletedOnboarding)
        #expect(try await credentials.loadCredential(for: profile.id) == .bearerToken("studio-secret"))
    }

    @Test("Canceling an inactive legacy edit restores its own global token")
    func legacyCredentialRollbackNeverUsesActiveProfileToken() async throws {
        let legacyProfile = ServerProfile(
            id: .legacyMigration,
            name: "Old Mac",
            endpoint: "https://old.example.com",
            authentication: .bearerToken
        )
        let modernProfile = ServerProfile(
            name: "Studio",
            endpoint: "https://studio.example.com",
            selectedModel: "qwen3:8b",
            authentication: .bearerToken
        )
        let settings = AppSettings(
            serverURL: modernProfile.endpoint,
            serverName: modernProfile.name,
            selectedModel: modernProfile.selectedModel,
            hasCompletedOnboarding: true,
            serverCatalog: ServerProfileCatalog(
                profiles: [legacyProfile, modernProfile],
                activeProfileID: modernProfile.id
            )
        )
        let snapshot = AppSnapshot(settings: settings)
        let repository = SuspendingFeatureRepository(
            snapshot: snapshot,
            suspendOnSaveNumber: 2
        )
        let tokenStore = InMemoryTokenStore(token: "legacy-secret")
        let credentials = InMemoryServerCredentialStore(
            credentials: [
                legacyProfile.id: .bearerToken("legacy-secret"),
                modernProfile.id: .bearerToken("modern-secret"),
            ]
        )
        let service = FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
        let model = AppModel(
            repository: repository,
            tokenStore: tokenStore,
            credentialStore: credentials,
            serviceFactory: OllamaServiceFactory { _, _ in service }
        )
        await model.start()
        #expect(model.bearerToken == "modern-secret")

        let candidate = Task {
            await model.updateConnection(
                profileID: .legacyMigration,
                serverURL: legacyProfile.endpoint,
                serverName: legacyProfile.name,
                credentialUpdate: .replaceBearerToken("replacement-legacy-secret")
            )
        }
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while !(await repository.hasSuspendedSave), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(await repository.hasSuspendedSave)
        model.requestPersistenceFlush()
        try? await Task.sleep(for: .milliseconds(20))
        #expect(await repository.saveAttemptCount == 2)

        model.cancelConnectionAttempt()
        candidate.cancel()
        await repository.resumeSuspendedSave()

        guard case .failure = await candidate.value else {
            Issue.record("Expected the canceled legacy edit to fail")
            return
        }
        #expect(try await tokenStore.loadToken() == "legacy-secret")
        #expect(
            try await credentials.loadCredential(for: .legacyMigration)
                == .bearerToken("legacy-secret")
        )
        #expect(model.activeServerProfile?.id == modernProfile.id)
        #expect(model.bearerToken == "modern-secret")
    }

    @Test("An unauthenticated legacy profile retires stale credential material")
    func unauthenticatedLegacyProfileDoesNotLoadStaleToken() async throws {
        let profile = ServerProfile(
            id: .legacyMigration,
            name: "Old Mac",
            endpoint: "https://old.example.com",
            authentication: .none
        )
        let settings = AppSettings(
            serverURL: profile.endpoint,
            serverName: profile.name,
            hasCompletedOnboarding: true,
            serverCatalog: ServerProfileCatalog(
                profiles: [profile],
                activeProfileID: profile.id
            )
        )
        let tokenStore = InMemoryTokenStore(token: "stale-global-secret")
        let credentials = InMemoryServerCredentialStore(
            credentials: [profile.id: .bearerToken("stale-profile-secret")]
        )
        let recorder = ConnectionFactoryRecorder()
        let model = AppModel(
            repository: InMemoryAppRepository(snapshot: AppSnapshot(settings: settings)),
            tokenStore: tokenStore,
            credentialStore: credentials,
            serviceFactory: OllamaServiceFactory { endpoint, bearerToken in
                recorder.record(endpoint: endpoint.displayAddress, bearerToken: bearerToken)
                return FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
            }
        )

        await model.start()

        #expect(model.bearerToken.isEmpty)
        #expect(recorder.calls.last?.bearerToken == nil)
        #expect(try await tokenStore.loadToken() == "")
        #expect(try await credentials.loadCredential(for: profile.id) == nil)
    }

    @Test("Removing authentication keeps the old credential until metadata is durable")
    func credentialRemovalRollsBackWhenPersistenceFails() async throws {
        let profile = ServerProfile(
            name: "Studio",
            endpoint: "https://studio.example.com",
            authentication: .bearerToken
        )
        let settings = AppSettings(
            serverURL: profile.endpoint,
            serverName: profile.name,
            hasCompletedOnboarding: true,
            serverCatalog: ServerProfileCatalog(
                profiles: [profile],
                activeProfileID: profile.id
            )
        )
        let credentials = InMemoryServerCredentialStore(
            credentials: [profile.id: .bearerToken("studio-secret")]
        )
        let model = AppModel(
            repository: AlwaysFailingFeatureRepository(),
            tokenStore: InMemoryTokenStore(),
            credentialStore: credentials,
            serviceFactory: OllamaServiceFactory { _, _ in
                FeatureOllamaService(models: [OllamaModel(name: "qwen3:8b")])
            },
            initialSnapshot: AppSnapshot(settings: settings),
            isRestoring: false
        )

        let result = await model.updateConnection(
            profileID: profile.id,
            serverURL: profile.endpoint,
            serverName: profile.name,
            credentialUpdate: .remove
        )

        guard case .failure = result else {
            Issue.record("Expected credential removal to fail when persistence fails")
            return
        }
        #expect(model.activeServerProfile == profile)
        #expect(try await credentials.loadCredential(for: profile.id) == .bearerToken("studio-secret"))
    }
}

private nonisolated struct FeatureOllamaService: OllamaServing {
    var version = "test"
    var models: [OllamaModel]
    var capabilities: [String: [String]] = [:]
    var recorder: ChatRequestRecorder?

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
        recorder?.record(request)
        return AsyncThrowingStream { continuation in
            continuation.yield(.content("Done"))
            continuation.yield(
                .completed(OllamaChatCompletion(metrics: OllamaChatMetrics()))
            )
            continuation.finish()
        }
    }
}

private nonisolated final class ChatRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var request: OllamaChatRequest?

    var lastRequest: OllamaChatRequest? {
        lock.withLock { request }
    }

    func record(_ request: OllamaChatRequest) {
        lock.withLock {
            self.request = request
        }
    }
}

private nonisolated final class ConnectionFactoryRecorder: @unchecked Sendable {
    struct Call: Equatable {
        let endpoint: String
        let bearerToken: String?
    }

    private let lock = NSLock()
    private var recordedCalls: [Call] = []

    var calls: [Call] {
        lock.withLock { recordedCalls }
    }

    func record(endpoint: String, bearerToken: String?) {
        lock.withLock {
            recordedCalls.append(Call(endpoint: endpoint, bearerToken: bearerToken))
        }
    }

    func reset() {
        lock.withLock {
            recordedCalls = []
        }
    }
}

private actor AlwaysFailingFeatureRepository: AppPersisting {
    func load() async throws -> AppSnapshot? { nil }

    func save(_ snapshot: AppSnapshot, revision: Int) async throws {
        throw FeaturePersistenceError.writeFailed
    }
}

private actor SuspendingFeatureRepository: AppPersisting {
    private var snapshot: AppSnapshot?
    private let suspendOnSaveNumber: Int
    private var saveNumber = 0
    private var suspendedContinuation: CheckedContinuation<Void, Never>?

    init(snapshot: AppSnapshot?, suspendOnSaveNumber: Int) {
        self.snapshot = snapshot
        self.suspendOnSaveNumber = suspendOnSaveNumber
    }

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
        if saveNumber == suspendOnSaveNumber {
            await withCheckedContinuation { continuation in
                suspendedContinuation = continuation
            }
        }
        try Task.checkCancellation()
        self.snapshot = snapshot
    }

    func resumeSuspendedSave() {
        suspendedContinuation?.resume()
        suspendedContinuation = nil
    }
}

private nonisolated enum FeaturePersistenceError: Error, Sendable {
    case writeFailed
}

private nonisolated final class SwitchableOllamaService: OllamaServing, @unchecked Sendable {
    private let lock = NSLock()
    private let version: String
    private let availableModels: [OllamaModel]
    private var failureEnabled = false

    init(version: String, models: [OllamaModel]) {
        self.version = version
        availableModels = models
    }

    var shouldFail: Bool {
        get { lock.withLock { failureEnabled } }
        set { lock.withLock { failureEnabled = newValue } }
    }

    func serverVersion() async throws -> OllamaServerVersion {
        try checkAvailability()
        return OllamaServerVersion(version: version)
    }

    func models() async throws -> [OllamaModel] {
        try checkAvailability()
        return availableModels
    }

    func show(model: String) async throws -> OllamaShowResponse {
        try checkAvailability()
        return OllamaShowResponse(
            license: nil,
            modelfile: nil,
            parameters: nil,
            template: nil,
            system: nil,
            modifiedAt: nil,
            details: nil,
            modelInfo: nil,
            projectorInfo: nil,
            capabilities: []
        )
    }

    func chat(_ request: OllamaChatRequest) -> OllamaChatEventStream {
        AsyncThrowingStream { continuation in
            continuation.finish()
        }
    }

    private func checkAvailability() throws {
        if shouldFail {
            throw FeatureConnectionError.unavailable
        }
    }
}

private nonisolated enum FeatureConnectionError: LocalizedError, Sendable {
    case unavailable

    var errorDescription: String? {
        "The feature-test server is unavailable."
    }
}

private nonisolated struct FeatureAttachmentFixture {
    let directory: URL
    let fileURL: URL
    let storageDirectory: URL

    init(fileName: String) throws {
        directory = FileManager.default.temporaryDirectory
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        fileURL = directory.appending(path: fileName, directoryHint: .notDirectory)
        storageDirectory = directory.appending(path: "stored", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    func remove() {
        try? FileManager.default.removeItem(at: directory)
    }
}

private nonisolated func featurePNG(width: Int, height: Int) throws -> Data {
    let context = try #require(
        CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
    )
    context.setFillColor(red: 0.49, green: 0.31, blue: 0.57, alpha: 1)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    let image = try #require(context.makeImage())

    let data = NSMutableData()
    let destination = try #require(
        CGImageDestinationCreateWithData(
            data,
            UTType.png.identifier as CFString,
            1,
            nil
        )
    )
    CGImageDestinationAddImage(destination, image, nil)
    #expect(CGImageDestinationFinalize(destination))
    return data as Data
}
