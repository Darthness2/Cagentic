import Foundation
import Testing
@testable import Cagentic

struct ServerProfileTests {
    @Test("Server profile identity survives a Codable round trip")
    func stableIdentityRoundTrip() throws {
        let profile = ServerProfile(
            id: ServerProfileID(
                rawValue: UUID(uuidString: "780511bb-ea91-4628-a416-bb3010343b18")!
            ),
            name: "Studio Mac",
            endpoint: "https://studio.example.com",
            selectedModel: "gemma3:12b",
            authentication: .bearerToken,
            createdAt: Date(timeIntervalSince1970: 100),
            lastConnectedAt: Date(timeIntervalSince1970: 200),
            lastServerVersion: "0.9.1"
        )

        let data = try JSONEncoder().encode(profile)
        let decoded = try JSONDecoder().decode(ServerProfile.self, from: data)

        #expect(decoded == profile)
    }

    @Test("Legacy single-server settings migrate with an idempotent profile ID")
    func legacySettingsMigration() {
        let settings = AppSettings(
            serverURL: "  http://studio-mac.local:11434  ",
            serverName: "  Studio Mac  ",
            selectedModel: "gemma3:12b"
        )
        let migrationDate = Date(timeIntervalSince1970: 500)

        let first = ServerProfileCatalog.migratingLegacySettings(
            settings,
            hasStoredCredential: true,
            migrationDate: migrationDate
        )
        let second = ServerProfileCatalog.migratingLegacySettings(
            settings,
            hasStoredCredential: true,
            migrationDate: migrationDate
        )

        #expect(first == second)
        #expect(first.activeProfile?.id == .legacyMigration)
        #expect(first.activeProfile?.name == "Studio Mac")
        #expect(first.activeProfile?.endpoint == "http://studio-mac.local:11434")
        #expect(first.activeProfile?.selectedModel == "gemma3:12b")
        #expect(first.activeProfile?.authentication == .bearerToken)
    }

    @Test("An empty legacy endpoint does not create a phantom server")
    func emptyLegacySettingsMigration() {
        let catalog = ServerProfileCatalog.migratingLegacySettings(
            AppSettings(serverURL: " \n "),
            hasStoredCredential: false,
            migrationDate: Date(timeIntervalSince1970: 500)
        )

        #expect(catalog.profiles.isEmpty)
        #expect(catalog.activeProfileID == nil)
    }

    @Test("Legacy settings JSON decodes without a server catalog")
    func legacySettingsDecoding() throws {
        let data = Data(
            #"{"serverURL":"http://old-mac.local:11434","serverName":"Old Mac","selectedModel":"llama3.2:latest","systemPrompt":"Be precise","appearance":"system","hapticsEnabled":true,"hasCompletedOnboarding":true}"#.utf8
        )

        let settings = try JSONDecoder().decode(AppSettings.self, from: data)

        #expect(settings.serverURL == "http://old-mac.local:11434")
        #expect(settings.serverName == "Old Mac")
        #expect(settings.selectedModel == "llama3.2:latest")
        #expect(settings.serverCatalog.profiles.isEmpty)
    }

    @Test("Catalog canonicalization preserves stable identity and a valid active server")
    func catalogCanonicalization() {
        let sharedID = ServerProfileID()
        let first = ServerProfile(id: sharedID, name: "Old", endpoint: "old.local")
        let replacement = ServerProfile(id: sharedID, name: "New", endpoint: "new.local")
        let catalog = ServerProfileCatalog(
            profiles: [first, replacement],
            activeProfileID: ServerProfileID()
        )

        #expect(catalog.profiles.count == 1)
        #expect(catalog.profiles.first?.name == "New")
        #expect(catalog.activeProfileID == sharedID)
    }

    @Test("Removing the active server selects the next persisted profile")
    func removalSelectsNextProfile() {
        let first = ServerProfile(name: "First", endpoint: "first.local")
        let second = ServerProfile(name: "Second", endpoint: "second.local")
        var catalog = ServerProfileCatalog(
            profiles: [first, second],
            activeProfileID: first.id
        )

        let removed = catalog.remove(id: first.id)

        #expect(removed == first)
        #expect(catalog.activeProfileID == second.id)
        #expect(catalog.activeProfile == second)
    }
}

struct ServerCredentialStoreTests {
    @Test("Credentials are isolated by immutable server profile identity")
    func credentialsAreProfileScoped() async throws {
        let firstID = ServerProfileID()
        let secondID = ServerProfileID()
        let store = InMemoryServerCredentialStore()

        try await store.saveCredential(.bearerToken("first-secret"), for: firstID)
        try await store.saveCredential(.bearerToken("second-secret"), for: secondID)
        try await store.removeCredential(for: firstID)

        let firstCredential = try await store.loadCredential(for: firstID)
        let secondCredential = try await store.loadCredential(for: secondID)
        #expect(firstCredential == nil)
        #expect(secondCredential == .bearerToken("second-secret"))
    }

    @Test("Concurrent conditional saves accept only one credential")
    func conditionalSaveIsAtomic() async throws {
        let profileID = ServerProfileID()
        let store = InMemoryServerCredentialStore()

        let results = await withTaskGroup(of: Bool.self, returning: [Bool].self) { group in
            for index in 0..<12 {
                group.addTask {
                    (try? await store.saveCredentialIfAbsent(
                        .bearerToken("secret-\(index)"),
                        for: profileID
                    )) ?? false
                }
            }

            var values: [Bool] = []
            for await result in group {
                values.append(result)
            }
            return values
        }

        #expect(results.filter(\.self).count == 1)
        let credential = try await store.loadCredential(for: profileID)
        #expect(credential != nil)
    }

    @Test("Credential reconciliation removes only profiles absent from the catalog")
    func credentialReconciliation() async throws {
        let retainedID = ServerProfileID()
        let orphanedID = ServerProfileID()
        let store = InMemoryServerCredentialStore(
            credentials: [
                retainedID: .bearerToken("keep"),
                orphanedID: .bearerToken("remove"),
            ]
        )

        try await store.reconcileCredentials(validProfileIDs: [retainedID])

        #expect(try await store.loadCredential(for: retainedID) == .bearerToken("keep"))
        #expect(try await store.loadCredential(for: orphanedID) == nil)
    }

    @Test("Legacy token migration copies once and never overwrites a profile credential")
    func legacyCredentialMigrationDoesNotOverwrite() async throws {
        let profileID = ServerProfileID.legacyMigration
        let legacyStore = InMemoryTokenStore(token: "legacy-secret")
        let profileStore = InMemoryServerCredentialStore()

        let copied = try await ServerCredentialMigration.copyLegacyTokenIfNeeded(
            from: legacyStore,
            to: profileStore,
            profileID: profileID
        )
        try await profileStore.saveCredential(.bearerToken("new-secret"), for: profileID)
        let copiedAgain = try await ServerCredentialMigration.copyLegacyTokenIfNeeded(
            from: legacyStore,
            to: profileStore,
            profileID: profileID
        )

        let profileCredential = try await profileStore.loadCredential(for: profileID)
        let legacyToken = try await legacyStore.loadToken()
        #expect(copied)
        #expect(!copiedAgain)
        #expect(profileCredential == .bearerToken("new-secret"))
        #expect(legacyToken == "legacy-secret")
    }

    @Test("Keychain account names are stable and profile-specific")
    func keychainAccountNamesAreStable() {
        let firstID = ServerProfileID(
            rawValue: UUID(uuidString: "2879b8ea-3f32-4dc2-98c7-cfd94e9de49a")!
        )
        let secondID = ServerProfileID(
            rawValue: UUID(uuidString: "40a779f5-420d-4894-968f-ae7df88a0d35")!
        )

        let first = KeychainServerCredentialStore.accountName(profileID: firstID)
        let firstAgain = KeychainServerCredentialStore.accountName(profileID: firstID)
        let second = KeychainServerCredentialStore.accountName(profileID: secondID)

        #expect(first == firstAgain)
        #expect(first != second)
        #expect(first.hasSuffix(firstID.description))
    }
}
