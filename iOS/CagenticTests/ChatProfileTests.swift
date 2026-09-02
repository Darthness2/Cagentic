import Foundation
import Testing
@testable import Cagentic

/// Chat profiles: named sets of instructions and dials, orthogonal to servers.
struct ChatProfileTests {
    @Test("A settings file written before profiles existed still decodes")
    func legacySettingsDecoding() throws {
        // Same shape ServerProfileTests uses for its own migration case.
        let data = Data(
            #"{"serverURL":"http://mac.local:8700","serverName":"Mac","selectedModel":"ornith-1.5:9b","systemPrompt":"Be precise","appearance":"dark","hapticsEnabled":true,"hasCompletedOnboarding":true}"#.utf8
        )

        let settings = try JSONDecoder().decode(AppSettings.self, from: data)

        #expect(settings.serverName == "Mac")
        #expect(settings.chatProfiles.profiles.isEmpty)
        #expect(settings.chatProfiles.activeProfileID == nil)
    }

    @Test("A profile written by an older build degrades to defaults")
    func partialProfileDecoding() throws {
        let data = Data(#"{"name":"Doctor","systemPrompt":"Be careful"}"#.utf8)

        let profile = try JSONDecoder().decode(ChatProfile.self, from: data)

        #expect(profile.name == "Doctor")
        #expect(profile.systemPrompt == "Be careful")
        #expect(profile.generation == GenerationOptions())
        #expect(profile.effort == nil)
        #expect(profile.preferredModel == nil)
    }

    @Test("Values round-trip")
    func roundTrips() throws {
        var profile = ChatProfile(name: "Assistant", systemPrompt: "Be brief")
        profile.effort = .high
        profile.preferredModel = "ornith-1.5:9b"
        profile.generation.temperature = 0.2

        let encoded = try JSONEncoder().encode(profile)
        #expect(try JSONDecoder().decode(ChatProfile.self, from: encoded) == profile)
    }

    @Test("The catalog never hands back an active id that resolves to nothing")
    func activeIDAlwaysResolves() {
        let real = ChatProfile(name: "Real")
        // An active id pointing at a profile that is not in the list is the state that would put
        // a nil where the picker expects a selection.
        let catalog = ChatProfileCatalog(profiles: [real], activeProfileID: ChatProfileID())

        #expect(catalog.activeProfileID == real.id)
        #expect(catalog.activeProfile?.name == "Real")
    }

    @Test("A duplicate id keeps the newest copy in the original position")
    func canonicalization() {
        let shared = ChatProfileID()
        let first = ChatProfile(id: shared, name: "Old")
        let other = ChatProfile(name: "Other")
        let replacement = ChatProfile(id: shared, name: "New")

        let catalog = ChatProfileCatalog(profiles: [first, other, replacement])

        #expect(catalog.profiles.map(\.name) == ["New", "Other"])
    }

    @Test("Removing the active profile moves the selection rather than clearing it")
    func removingActiveReselects() {
        let first = ChatProfile(name: "First")
        let second = ChatProfile(name: "Second")
        var catalog = ChatProfileCatalog(profiles: [first, second], activeProfileID: first.id)

        catalog.remove(id: first.id)

        #expect(catalog.activeProfileID == second.id)
    }

    @Test("Seeding is idempotent")
    func seedingIsIdempotent() {
        var settings = AppSettings()
        settings.systemPrompt = "Carried over"
        settings.generation.temperature = 0.31
        var catalog = ChatProfileCatalog()

        catalog.seedIfEmpty(from: settings)
        catalog.seedIfEmpty(from: settings)

        // A second launch must not add a second "Default" — that is what the fixed id is for.
        #expect(catalog.profiles.count == 1)
        #expect(catalog.profiles.first?.systemPrompt == "Carried over")
        #expect(catalog.profiles.first?.generation.temperature == 0.31)
        #expect(catalog.activeProfileID == ChatProfileID.starter)
    }

    @Test("Which dials a gateway can actually honour")
    func gatewayApplicability() {
        // The Settings sheet has been showing all of these on a gateway connection while only
        // three of them had anywhere to land.
        #expect(ChatProfileField.systemPrompt.appliesOnGateway)
        #expect(ChatProfileField.temperature.appliesOnGateway)
        #expect(ChatProfileField.effort.appliesOnGateway)
        #expect(!ChatProfileField.topP.appliesOnGateway)
        #expect(!ChatProfileField.contextLength.appliesOnGateway)
        #expect(!ChatProfileField.keepAlive.appliesOnGateway)
    }
}
