import Foundation
import Testing
@testable import Cagentic

/// `GenerationOptions` has to tolerate a stored file that predates one of its fields.
///
/// Every other type in `AppModels.swift` hand-writes a decoder that defaults each missing key.
/// `GenerationOptions` did not, and it is nested inside `AppSettings`, which is nested inside
/// `AppSnapshot` — so one unknown-shaped `generation` object does not degrade to defaults, it
/// throws all the way out of `JSONAppRepository.load()` and the whole of someone's saved state
/// goes with it. That is the failure these guard against, and it fires on the *existing* file the
/// moment a new field is added.
struct GenerationOptionsDecodingTests {
    private func decode(_ json: String) throws -> GenerationOptions {
        try JSONDecoder().decode(GenerationOptions.self, from: Data(json.utf8))
    }

    @Test("A generation block written before a field existed still decodes")
    func partialGenerationDecodes() throws {
        // Exactly what an older build wrote: no keepAlive, no enableThinking, no topP.
        let options = try decode(#"{"temperature":0.35,"contextLength":16384}"#)

        #expect(options.temperature == 0.35)
        #expect(options.contextLength == 16_384)
        // The absent keys fall back rather than failing the decode.
        #expect(options.topP == 0.9)
        #expect(options.keepAlive == "30m")
        #expect(options.enableThinking == true)
        #expect(options.seed == nil)
    }

    @Test("An empty generation block is all defaults")
    func emptyGenerationDecodes() throws {
        #expect(try decode("{}") == GenerationOptions())
    }

    @Test("A whole settings file survives a partial generation block")
    func settingsSurvivePartialGeneration() throws {
        // The real shape of the bug: the throw happens two levels down and takes everything.
        let json = #"""
        {"serverURL":"http://mac.local:8700","serverName":"Mac","selectedModel":"ornith-1.5:9b",
         "systemPrompt":"Be precise","generation":{"temperature":0.2},
         "appearance":"dark","hapticsEnabled":true,"hasCompletedOnboarding":true}
        """#
        let settings = try JSONDecoder().decode(AppSettings.self, from: Data(json.utf8))

        #expect(settings.serverName == "Mac")
        #expect(settings.generation.temperature == 0.2)
        #expect(settings.generation.keepAlive == "30m")
    }

    @Test("Values still round-trip")
    func roundTrips() throws {
        var options = GenerationOptions()
        options.temperature = 1.4
        options.topP = 0.55
        options.contextLength = 32_768
        options.seed = 99
        options.keepAlive = "5m"
        options.enableThinking = false

        let encoded = try JSONEncoder().encode(options)
        #expect(try JSONDecoder().decode(GenerationOptions.self, from: encoded) == options)
    }
}
