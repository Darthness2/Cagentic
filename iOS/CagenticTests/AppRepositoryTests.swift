import Foundation
import Testing
@testable import Cagentic

struct AppRepositoryTests {
    @Test("Snapshot round-trips through the JSON repository")
    func snapshotRoundTrip() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        let fileURL = directory.appending(path: "state.json", directoryHint: .notDirectory)
        let repository = JSONAppRepository(fileURL: fileURL)

        let conversation = Conversation.previewConversation
        let attachmentID = UUID()
        let draftAttachment = AttachmentMetadata(
            id: attachmentID,
            kind: .textFile,
            displayName: "notes.txt",
            sourceContentType: "text/plain",
            payloadContentType: "text/plain; charset=utf-8",
            payloadFormat: .utf8Text,
            byteCount: 5,
            storageKey: "\(attachmentID.uuidString.lowercased()).txt",
            extractedCharacterCount: 5
        )
        let snapshot = AppSnapshot(
            settings: AppSettings(
                serverURL: "http://192.168.1.44:11434",
                serverName: "Studio PC",
                selectedModel: "llama3.2:latest",
                systemPrompt: "Be concise.",
                generation: GenerationOptions(),
                appearance: .dark,
                hapticsEnabled: true,
                hasCompletedOnboarding: true
            ),
            conversations: [conversation],
            selectedConversationID: conversation.id,
            conversationDrafts: [
                conversation.id: ConversationDraft(
                    text: "Remember this",
                    attachments: [draftAttachment]
                )
            ]
        )

        try await repository.save(snapshot)
        let loaded = try await repository.load()

        #expect(loaded == snapshot)
    }

    @Test("Missing state file loads as an empty result")
    func missingFileReturnsNil() async throws {
        let fileURL = FileManager.default.temporaryDirectory
            .appending(path: UUID().uuidString)
            .appending(path: "missing.json")
        let repository = JSONAppRepository(fileURL: fileURL)

        let loaded = try await repository.load()

        #expect(loaded == nil)
    }

    @Test("An older save revision cannot overwrite a newer snapshot")
    func staleRevisionIsIgnored() async throws {
        let fileURL = FileManager.default.temporaryDirectory
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
            .appending(path: "state.json", directoryHint: .notDirectory)
        let repository = JSONAppRepository(fileURL: fileURL)
        var newestSettings = AppSettings()
        newestSettings.serverName = "Newest"
        var staleSettings = AppSettings()
        staleSettings.serverName = "Stale"
        let newest = AppSnapshot(settings: newestSettings)

        try await repository.save(newest, revision: 2)
        try await repository.save(AppSnapshot(settings: staleSettings), revision: 1)

        let loaded = try await repository.load()
        #expect(loaded == newest)
    }
}
