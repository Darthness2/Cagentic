import Foundation
import Testing
@testable import Cagentic

/// Choosing a profile or a mode, and where each one lands.
///
/// A profile is a preset you apply, not a second place the values live: applying one writes into
/// the settings the send path already reads, and — on a gateway, where the computer owns the loop
/// — tells the computer as well.
@MainActor
struct ChatProfileApplicationTests {
    @Test("Applying a profile puts its values where the send path reads them")
    func applyingWritesEffectiveSettings() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        let model = makeModel(gateway: service)
        try await connect(model)

        var profile = ChatProfile(name: "Doctor", systemPrompt: "Be a careful clinician.")
        profile.generation.temperature = 0.15
        model.applyChatProfile(profile)

        #expect(model.settings.systemPrompt == "Be a careful clinician.")
        #expect(model.settings.generation.temperature == 0.15)
        #expect(model.activeChatProfile?.id == profile.id)
    }

    @Test("On a gateway, the profile is sent to the computer too")
    func applyingReachesTheGateway() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        let model = makeModel(gateway: service)
        try await connect(model)

        model.applyChatProfile(ChatProfile(name: "Doctor", systemPrompt: "Be careful."))
        await settle(service) { await $0.chatConfigCalls.isEmpty == false }

        let call = try #require(await service.chatConfigCalls.first)
        #expect(call.profile?.name == "Doctor")
        #expect(call.profile?.systemPrompt == "Be careful.")
        // Sending a profile must not silently reset the chat's approval mode.
        #expect(call.mode == nil)
    }

    @Test("Choosing a mode reaches the gateway and shows immediately")
    func modeIsSentAndReflected() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        let model = makeModel(gateway: service)
        try await connect(model)

        model.setChatMode(.unrestricted)

        // Optimistic, so the picker does not lag a network round trip.
        #expect(model.activeChatMode == .unrestricted)
        await settle(service) { await $0.chatConfigCalls.isEmpty == false }
        let call = try #require(await service.chatConfigCalls.first)
        #expect(call.mode == .unrestricted)
        #expect(call.profile == nil)
    }

    @Test("A chat with no mode reported reads as restricted, not as unrestricted")
    func unknownModeFallsBackSafely() async throws {
        let service = MockGatewayService(bootstrap: .stub())
        let model = makeModel(gateway: service)
        try await connect(model)

        // Nothing has said otherwise, so the safe end of the range is what shows.
        #expect(model.activeChatMode == .restricted)
        #expect(GatewayChatMode(rawValue: "nonsense") == nil)
        #expect(GatewayChatMode.fallback == .restricted)
    }

    @Test("A preferred model is selected with the profile when it is installed")
    func preferredModelIsSelected() async throws {
        let service = MockGatewayService(
            bootstrap: .stub(models: ["ornith-1.5:9b", "qwen3:8b"], activeModel: "qwen3:8b")
        )
        let model = makeModel(gateway: service)
        try await connect(model)

        var profile = ChatProfile(name: "Coder")
        profile.preferredModel = "ornith-1.5:9b"
        model.applyChatProfile(profile)

        #expect(model.settings.selectedModel == "ornith-1.5:9b")
    }

    @Test("A preferred model that is not installed is ignored rather than forced")
    func missingPreferredModelIsIgnored() async throws {
        let service = MockGatewayService(
            bootstrap: .stub(models: ["qwen3:8b"], activeModel: "qwen3:8b")
        )
        let model = makeModel(gateway: service)
        try await connect(model)
        let before = model.settings.selectedModel

        var profile = ChatProfile(name: "Coder")
        profile.preferredModel = "not-installed:70b"
        model.applyChatProfile(profile)

        #expect(model.settings.selectedModel == before)
    }

    // MARK: - Harness

    private func makeModel(gateway: MockGatewayService) -> AppModel {
        AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
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

    private func settle(
        _ service: MockGatewayService,
        until condition: (MockGatewayService) async -> Bool
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(2))
        while await !condition(service), clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(5))
        }
        #expect(await condition(service), "the gateway was never told")
    }
}
