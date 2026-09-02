import Foundation
import Testing
@testable import Cagentic

/// Finishing onboarding without a server, and what the app is like afterwards.
struct OnboardingSkipTests {
    @Test("Skipping finishes onboarding without inventing a server")
    func skippingCompletesOnboarding() async throws {
        let repository = InMemoryAppRepository()
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            isRestoring: false
        )
        #expect(model.settings.hasCompletedOnboarding == false)

        model.skipOnboarding()

        #expect(model.settings.hasCompletedOnboarding)
        // Nothing was configured, so the app must not pretend otherwise.
        #expect(model.serverProfiles.isEmpty)
        #expect(model.settings.serverURL.isEmpty)
        #expect(model.connectionState == .notConfigured)
    }

    @Test("The choice survives a relaunch")
    func skippingIsPersisted() async throws {
        let repository = InMemoryAppRepository()
        let model = AppModel(
            repository: repository,
            tokenStore: InMemoryTokenStore(),
            isRestoring: false
        )

        model.skipOnboarding()
        // Onboarding is a required full-screen cover; if this did not reach disk the person would
        // be handed the same form again on every launch.
        #expect(await model.flushPersistence())

        let saved = try #require(await repository.load())
        #expect(saved.settings.hasCompletedOnboarding)
        #expect(saved.settings.serverCatalog.profiles.isEmpty)
    }

    @Test("Skipping twice is harmless")
    func skippingIsIdempotent() {
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            isRestoring: false
        )

        model.skipOnboarding()
        model.skipOnboarding()

        #expect(model.settings.hasCompletedOnboarding)
    }

    @Test("Skipping does not disturb a connection that already exists")
    func skippingLeavesAnExistingSetupAlone() {
        var settings = AppSettings()
        settings.hasCompletedOnboarding = true
        settings.serverURL = "http://studio-pc.local:11434"
        settings.serverName = "Studio PC"
        let model = AppModel(
            repository: InMemoryAppRepository(),
            tokenStore: InMemoryTokenStore(),
            initialSnapshot: AppSnapshot(settings: settings),
            isRestoring: false
        )

        model.skipOnboarding()

        #expect(model.settings.serverURL == "http://studio-pc.local:11434")
        #expect(model.settings.serverName == "Studio PC")
    }
}
