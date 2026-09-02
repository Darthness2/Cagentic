import Foundation
import Testing
@testable import Cagentic

/// Sending a file to a gateway-backed chat.
///
/// A gateway turn is a plain message string — there is no field to put a file in. So each
/// attachment is uploaded first and referenced by the workspace-relative path the gateway hands
/// back, which puts it on the engine's existing `@path` mention pipeline: the same code that
/// extracts text from a PDF and inlines an image for a vision model when a mention is typed in the
/// terminal. The alternative would be a second attachment implementation to keep in step.
@MainActor
struct GatewayAttachmentTests {
    @Test("The file is uploaded and the message points at it")
    func attachmentIsUploadedAndMentioned() async throws {
        let fixture = try GatewayAttachmentFixture(fileName: "notes.txt")
        defer { fixture.remove() }
        try Data("river notes".utf8).write(to: fixture.fileURL)

        let service = MockGatewayService(bootstrap: .stub())
        let model = makeModel(gateway: service, fixture: fixture)
        try await connect(model)
        try await attach(fixture.fileURL, to: model)

        model.draft = "What does this say?"
        model.sendDraft()
        await waitForSend(service)

        let uploads = await service.uploads
        #expect(uploads.map(\.name) == ["notes.txt"])
        #expect(uploads.first?.byteCount == Data("river notes".utf8).count)

        let sent = try #require(await service.sentMessages.first)
        #expect(sent == "What does this say?\n\n@.cagentic/uploads/chat-1/notes.txt")
    }

    @Test("A path with a space is quoted so the whole of it is read")
    func spacedPathsAreQuoted() async throws {
        let fixture = try GatewayAttachmentFixture(fileName: "trip notes.txt")
        defer { fixture.remove() }
        try Data("hello".utf8).write(to: fixture.fileURL)

        let service = MockGatewayService(bootstrap: .stub())
        await service.setUploadPathPrefix(".cagentic/uploads/my chat/")
        let model = makeModel(gateway: service, fixture: fixture)
        try await connect(model)
        try await attach(fixture.fileURL, to: model)

        model.draft = "read it"
        model.sendDraft()
        await waitForSend(service)

        // Unquoted, the gateway's mention regex stops at the first space and the model is handed
        // half a filename.
        let sent = try #require(await service.sentMessages.first)
        #expect(sent.hasSuffix("@\"" + ".cagentic/uploads/my chat/trip notes.txt" + "\""))
    }

    @Test("An attachment with no text still sends something the model can act on")
    func attachmentOnlyMessage() async throws {
        let fixture = try GatewayAttachmentFixture(fileName: "notes.txt")
        defer { fixture.remove() }
        try Data("body".utf8).write(to: fixture.fileURL)

        let service = MockGatewayService(bootstrap: .stub())
        let model = makeModel(gateway: service, fixture: fixture)
        try await connect(model)
        try await attach(fixture.fileURL, to: model)

        model.draft = ""
        model.sendDraft()
        await waitForSend(service)

        let sent = try #require(await service.sentMessages.first)
        #expect(sent == "@.cagentic/uploads/chat-1/notes.txt")
    }

    @Test("A failed upload fails the turn instead of sending a message about nothing")
    func failedUploadDoesNotSend() async throws {
        let fixture = try GatewayAttachmentFixture(fileName: "notes.txt")
        defer { fixture.remove() }
        try Data("body".utf8).write(to: fixture.fileURL)

        let service = MockGatewayService(bootstrap: .stub())
        await service.setUploadFailure(
            GatewayClientError.server(message: "attachment is larger than 16MB")
        )
        let model = makeModel(gateway: service, fixture: fixture)
        try await connect(model)
        try await attach(fixture.fileURL, to: model)

        model.draft = "summarise this"
        model.sendDraft()
        await waitUntil("the turn to fail") { !model.isGenerating }

        // The message the model would have received names a file that is not there, so sending it
        // would be worse than failing.
        #expect(await service.sentMessages.isEmpty)
        let reply = try #require(model.selectedConversation?.messages.last)
        #expect(reply.state == .failed)
    }

    @Test("Two attachments are both uploaded and both mentioned, in order")
    func multipleAttachments() async throws {
        let fixture = try GatewayAttachmentFixture(fileName: "first.txt")
        defer { fixture.remove() }
        let second = fixture.directory.appending(path: "second.txt", directoryHint: .notDirectory)
        try Data("one".utf8).write(to: fixture.fileURL)
        try Data("two".utf8).write(to: second)

        let service = MockGatewayService(bootstrap: .stub())
        let model = makeModel(gateway: service, fixture: fixture)
        try await connect(model)
        try await attach(fixture.fileURL, to: model)
        try await attach(second, to: model)

        model.draft = "compare these"
        model.sendDraft()
        await waitForSend(service)

        #expect(await service.uploads.map(\.name) == ["first.txt", "second.txt"])
        let sent = try #require(await service.sentMessages.first)
        #expect(
            sent == "compare these\n\n"
                + "@.cagentic/uploads/chat-1/first.txt @.cagentic/uploads/chat-1/second.txt"
        )
    }

    @Test("An upload is named after what it contains, not what was picked")
    func uploadNameDescribesThePayload() {
        // The store normalises everything to JPEG or UTF-8 text, and the gateway decides how to
        // treat a file purely from its suffix — so these names are what make an image an image.
        #expect(Self.name(displayName: "Photo", format: .jpeg) == "Photo.jpg")
        #expect(Self.name(displayName: "IMG_0042.HEIC", format: .jpeg) == "IMG_0042.jpg")
        // A PDF arrives here already extracted to text; leaving it as .pdf would have the engine
        // try to parse plain text as one.
        #expect(Self.name(displayName: "report.pdf", format: .utf8Text) == "report.txt")
        #expect(Self.name(displayName: "notes.txt", format: .utf8Text) == "notes.txt")
        // A name that is nothing but an extension, or nothing at all, still has to be usable.
        #expect(Self.name(displayName: ".txt", format: .utf8Text) == ".txt.txt")
        #expect(Self.name(displayName: "   ", format: .jpeg) == "attachment.jpg")
    }

    private static func name(
        displayName: String,
        format: AttachmentPayloadFormat
    ) -> String {
        AppModel.uploadName(
            for: AttachmentMetadata(
                kind: format == .jpeg ? .photo : .textFile,
                displayName: displayName,
                sourceContentType: "application/octet-stream",
                payloadContentType: format == .jpeg ? "image/jpeg" : "text/plain",
                payloadFormat: format,
                byteCount: 1,
                createdAt: .now,
                storageKey: "key"
            )
        )
    }

    // MARK: - Harness

    private func makeModel(
        gateway: MockGatewayService,
        fixture: GatewayAttachmentFixture
    ) -> AppModel {
        AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            attachmentStore: AttachmentStore(rootDirectory: fixture.storageDirectory),
            serviceFactory: OllamaServiceFactory { _, _ in MockUnusedOllamaService() },
            gatewayFactory: GatewayServiceFactory { _, _ in gateway },
            isRestoring: false
        )
    }

    private func connect(_ model: AppModel) async throws {
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

    private func attach(_ url: URL, to model: AppModel) async throws {
        let result = await model.importFileAttachments(from: [AttachmentImportSource(url: url)])
        guard case .success = result else {
            Issue.record("Expected \(url.lastPathComponent) to import")
            throw AttachmentImportRefusal()
        }
    }

    private func waitForSend(_ service: MockGatewayService) async {
        await waitUntil("the gateway to receive the turn") {
            await service.sentMessages.isEmpty == false
        }
    }

    private func waitUntil(
        _ description: String,
        condition: () async -> Bool
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while await !condition(), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(await condition(), "Timed out waiting for \(description)")
    }
}

private struct AttachmentImportRefusal: Error {}

private nonisolated struct GatewayAttachmentFixture {
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
