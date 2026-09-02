import Foundation

/// A durable identity for a chat profile.
///
/// Deliberately its own type rather than a bare `UUID`, and deliberately not called
/// `ProfileID`: `ServerProfileID` already exists and means a configured *server*. The two are
/// orthogonal — you pick a server, and separately a profile to talk with — so nothing in this file
/// should ever be spelled as a bare `profile`.
nonisolated struct ChatProfileID: RawRepresentable, Identifiable, Codable, Equatable, Hashable,
    Sendable, CustomStringConvertible
{
    let rawValue: UUID

    init(rawValue: UUID) {
        self.rawValue = rawValue
    }

    init() {
        self.init(rawValue: UUID())
    }

    var id: Self { self }
    var description: String { rawValue.uuidString.lowercased() }

    /// The profile seeded from whatever settings someone already had, so the list is never empty
    /// on first launch. Fixed, so seeding twice cannot produce two of them.
    static let starter = ChatProfileID(
        rawValue: UUID(uuidString: "5C0A7E11-0000-4000-A000-000000000001") ?? UUID()
    )
}

/// How hard the gateway is asked to think. Mirrors `EFFORT_LEVELS` in `cagentic/gateway.py`;
/// there is no equivalent on a direct Ollama connection, where the app drives sampling itself.
nonisolated enum ChatEffort: String, Codable, CaseIterable, Equatable, Hashable, Sendable,
    Identifiable
{
    case low
    case medium
    case high

    var id: String { rawValue }

    var title: String {
        switch self {
        case .low: "Low"
        case .medium: "Medium"
        case .high: "High"
        }
    }
}

/// A named set of instructions and dials to talk to a model with.
///
/// Independent of both the server and the model: any profile can be used with any model. A profile
/// may *name* a preferred model, which is selected when you switch to it, but it never restricts
/// which profiles are on offer.
///
/// Not every field reaches every backend. On a direct Ollama connection the app builds the request
/// itself and all of them apply. On a gateway the computer owns the loop, and only the system
/// prompt, temperature and effort have anywhere to land — see `appliesOnGateway`.
nonisolated struct ChatProfile: Identifiable, Codable, Equatable, Hashable, Sendable {
    var id: ChatProfileID
    var name: String
    var systemPrompt: String
    var generation: GenerationOptions
    var effort: ChatEffort?
    /// Selected when this profile is chosen, if it is installed. A hint, not a constraint.
    var preferredModel: String?
    var createdAt: Date
    var updatedAt: Date

    init(
        id: ChatProfileID = ChatProfileID(),
        name: String,
        systemPrompt: String = "",
        generation: GenerationOptions = GenerationOptions(),
        effort: ChatEffort? = nil,
        preferredModel: String? = nil,
        createdAt: Date = .now,
        updatedAt: Date = .now
    ) {
        self.id = id
        self.name = name
        self.systemPrompt = systemPrompt
        self.generation = generation
        self.effort = effort
        self.preferredModel = preferredModel
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    var displayName: String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "Untitled profile" : trimmed
    }

    /// The settings someone already had, as a profile, so the feature starts with something in it
    /// rather than an empty list and a plus button.
    static func starter(from settings: AppSettings) -> ChatProfile {
        ChatProfile(
            id: .starter,
            name: "Default",
            systemPrompt: settings.systemPrompt,
            generation: settings.generation,
            createdAt: .distantPast,
            updatedAt: .distantPast
        )
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case name
        case systemPrompt
        case generation
        case effort
        case preferredModel
        case createdAt
        case updatedAt
    }

    /// Tolerant for the same reason every other stored type here is: a file written by an older
    /// build must degrade to defaults rather than take the whole snapshot down with it.
    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let id = try container.decodeIfPresent(ChatProfileID.self, forKey: .id) ?? ChatProfileID()
        let created = try container.decodeIfPresent(Date.self, forKey: .createdAt) ?? .distantPast
        self.init(
            id: id,
            name: try container.decodeIfPresent(String.self, forKey: .name) ?? "",
            systemPrompt: try container.decodeIfPresent(String.self, forKey: .systemPrompt) ?? "",
            generation: try container.decodeIfPresent(GenerationOptions.self, forKey: .generation)
                ?? GenerationOptions(),
            effort: try container.decodeIfPresent(ChatEffort.self, forKey: .effort),
            preferredModel: try container.decodeIfPresent(String.self, forKey: .preferredModel),
            createdAt: created,
            updatedAt: try container.decodeIfPresent(Date.self, forKey: .updatedAt) ?? created
        )
    }
}


/// Which of a profile's dials the connected backend can actually honour.
///
/// This exists because the Settings sheet has been quietly lying: its temperature, top-P, context
/// and keep-alive controls are only read on the direct-Ollama branch of `AppModel.send`, so on a
/// gateway connection every one of them has been decoration. Rather than hide the controls, the
/// UI asks this and says so.
nonisolated enum ChatProfileField: String, CaseIterable, Sendable {
    case systemPrompt
    case temperature
    case effort
    case topP
    case contextLength
    case keepAlive
    case thinking

    /// `true` when a gateway can honour the field. The gateway's engine passes only a temperature
    /// to its provider client and reads `num_ctx` from a global config when the client is built,
    /// so top-P, context length and keep-alive have nowhere per-chat to land there yet.
    var appliesOnGateway: Bool {
        switch self {
        case .systemPrompt, .temperature, .effort: true
        case .topP, .contextLength, .keepAlive, .thinking: false
        }
    }

    /// Shown beside a control the connected backend will ignore.
    static let ollamaOnlyNote = "Ollama only — the gateway's computer owns this."
}

/// The persistable collection. Canonicalises decoded data the way `ServerProfileCatalog` does, so
/// no caller can be handed an active identifier that resolves to nothing, and duplicate ids cannot
/// reach a `ForEach`.
nonisolated struct ChatProfileCatalog: Codable, Equatable, Hashable, Sendable {
    static let currentSchemaVersion = 1

    var schemaVersion: Int
    private(set) var profiles: [ChatProfile]
    private(set) var activeProfileID: ChatProfileID?

    init(
        schemaVersion: Int = currentSchemaVersion,
        profiles: [ChatProfile] = [],
        activeProfileID: ChatProfileID? = nil
    ) {
        self.schemaVersion = max(schemaVersion, 1)
        self.profiles = Self.canonicalProfiles(profiles)
        self.activeProfileID = Self.resolvedActiveID(activeProfileID, in: self.profiles)
    }

    var activeProfile: ChatProfile? {
        guard let activeProfileID else { return nil }
        return profile(id: activeProfileID)
    }

    func profile(id: ChatProfileID) -> ChatProfile? {
        profiles.first(where: { $0.id == id })
    }

    mutating func upsert(_ profile: ChatProfile, makeActive: Bool = false) {
        if let index = profiles.firstIndex(where: { $0.id == profile.id }) {
            profiles[index] = profile
        } else {
            profiles.append(profile)
        }
        if activeProfileID == nil || makeActive {
            activeProfileID = profile.id
        }
    }

    @discardableResult
    mutating func select(_ id: ChatProfileID) -> Bool {
        guard profile(id: id) != nil else { return false }
        activeProfileID = id
        return true
    }

    @discardableResult
    mutating func remove(id: ChatProfileID) -> ChatProfile? {
        guard let index = profiles.firstIndex(where: { $0.id == id }) else { return nil }
        let removed = profiles.remove(at: index)
        if activeProfileID == id {
            activeProfileID = profiles.first?.id
        }
        return removed
    }

    /// Adds the starter profile if the catalog has never been populated. Idempotent: the starter
    /// has a fixed id, so a second call cannot produce a second copy.
    mutating func seedIfEmpty(from settings: AppSettings) {
        guard profiles.isEmpty else { return }
        upsert(ChatProfile.starter(from: settings), makeActive: true)
    }

    private static func canonicalProfiles(_ profiles: [ChatProfile]) -> [ChatProfile] {
        // Newest wins on a duplicate id, but the original position is kept so the list does not
        // reorder itself under someone who is looking at it.
        var seen: [ChatProfileID: ChatProfile] = [:]
        var order: [ChatProfileID] = []
        for profile in profiles {
            if seen[profile.id] == nil { order.append(profile.id) }
            seen[profile.id] = profile
        }
        return order.compactMap { seen[$0] }
    }

    private static func resolvedActiveID(
        _ proposed: ChatProfileID?,
        in profiles: [ChatProfile]
    ) -> ChatProfileID? {
        if let proposed, profiles.contains(where: { $0.id == proposed }) { return proposed }
        return profiles.first?.id
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion
        case profiles
        case activeProfileID
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let decoded = try container.decodeIfPresent([ChatProfile].self, forKey: .profiles) ?? []
        let active = try container.decodeIfPresent(ChatProfileID.self, forKey: .activeProfileID)
        self.init(
            schemaVersion: try container.decodeIfPresent(Int.self, forKey: .schemaVersion) ?? 1,
            profiles: decoded,
            activeProfileID: active
        )
    }
}
