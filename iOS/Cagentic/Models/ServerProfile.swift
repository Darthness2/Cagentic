import Foundation

/// A durable identity for a configured Ollama server.
///
/// The identifier deliberately does not depend on a profile's editable name or endpoint, so
/// conversations and credentials can keep referring to the same server after its display name
/// changes. AppModel rotates the identifier when the endpoint changes so a credential can never
/// become associated with a different host after an interrupted persistence transaction.
nonisolated struct ServerProfileID: RawRepresentable, Identifiable, Codable, Equatable, Hashable,
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
}

nonisolated enum ServerAuthentication: String, Codable, CaseIterable, Equatable, Hashable, Sendable {
    case none
    case bearerToken
}

/// Which backend a configured server speaks.
///
/// The two are not interchangeable transports for the same thing: Ollama serves a stateless
/// completion API the app drives, while the Cagentic gateway runs an agentic loop that owns its own
/// conversation history and asks for tool approvals mid-turn.
nonisolated enum ServerKind: String, Codable, CaseIterable, Equatable, Hashable, Sendable,
    Identifiable
{
    case ollama
    case gateway

    var id: Self { self }

    var displayName: String {
        switch self {
        case .ollama: "Ollama"
        case .gateway: "Cagentic gateway"
        }
    }

    var defaultServerName: String {
        switch self {
        case .ollama: "My Ollama"
        case .gateway: "My gateway"
        }
    }

    /// Conversations belonging to a gateway live on the computer, so the app mirrors them for
    /// display rather than owning them.
    var ownsConversations: Bool {
        self == .ollama
    }

    /// An unknown value must not fail the whole snapshot decode; a profile written by a newer build
    /// degrades to the backend every existing profile already used.
    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = try container.decode(String.self)
        self = ServerKind(rawValue: raw) ?? .ollama
    }
}

/// Persistable server metadata. Credentials are intentionally excluded and live in Keychain.
nonisolated struct ServerProfile: Identifiable, Codable, Equatable, Hashable, Sendable {
    let id: ServerProfileID
    var name: String
    var endpoint: String
    var kind: ServerKind
    var selectedModel: String
    var authentication: ServerAuthentication
    var createdAt: Date
    var updatedAt: Date
    var lastConnectedAt: Date?
    var lastServerVersion: String?

    init(
        id: ServerProfileID = ServerProfileID(),
        name: String,
        endpoint: String,
        kind: ServerKind = .ollama,
        selectedModel: String = "",
        authentication: ServerAuthentication = .none,
        createdAt: Date = .now,
        updatedAt: Date? = nil,
        lastConnectedAt: Date? = nil,
        lastServerVersion: String? = nil
    ) {
        self.id = id
        self.name = name
        self.endpoint = endpoint
        self.kind = kind
        self.selectedModel = selectedModel
        self.authentication = authentication
        self.createdAt = createdAt
        self.updatedAt = updatedAt ?? createdAt
        self.lastConnectedAt = lastConnectedAt
        self.lastServerVersion = lastServerVersion
    }

    var displayName: String {
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmedName.isEmpty ? kind.defaultServerName : trimmedName
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case name
        case endpoint
        case kind
        case selectedModel
        case authentication
        case createdAt
        case updatedAt
        case lastConnectedAt
        case lastServerVersion
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(ServerProfileID.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        endpoint = try container.decode(String.self, forKey: .endpoint)
        // Absent for every profile written before the gateway backend existed.
        kind = try container.decodeIfPresent(ServerKind.self, forKey: .kind) ?? .ollama
        selectedModel = try container.decodeIfPresent(String.self, forKey: .selectedModel) ?? ""
        authentication = try container.decodeIfPresent(
            ServerAuthentication.self,
            forKey: .authentication
        ) ?? .none
        createdAt = try container.decodeIfPresent(Date.self, forKey: .createdAt) ?? .distantPast
        updatedAt = try container.decodeIfPresent(Date.self, forKey: .updatedAt) ?? createdAt
        lastConnectedAt = try container.decodeIfPresent(Date.self, forKey: .lastConnectedAt)
        lastServerVersion = try container.decodeIfPresent(String.self, forKey: .lastServerVersion)
    }
}

/// The persistable multi-server collection. It canonicalizes decoded data so callers never have an
/// active identifier that does not resolve to a profile and duplicate IDs cannot enter `ForEach`.
nonisolated struct ServerProfileCatalog: Codable, Equatable, Hashable, Sendable {
    static let currentSchemaVersion = 1

    var schemaVersion: Int
    private(set) var profiles: [ServerProfile]
    private(set) var activeProfileID: ServerProfileID?

    init(
        schemaVersion: Int = currentSchemaVersion,
        profiles: [ServerProfile] = [],
        activeProfileID: ServerProfileID? = nil
    ) {
        self.schemaVersion = max(schemaVersion, 1)
        self.profiles = Self.canonicalProfiles(profiles)
        self.activeProfileID = Self.resolvedActiveID(activeProfileID, in: self.profiles)
    }

    var activeProfile: ServerProfile? {
        guard let activeProfileID else { return nil }
        return profile(id: activeProfileID)
    }

    func profile(id: ServerProfileID) -> ServerProfile? {
        profiles.first(where: { $0.id == id })
    }

    mutating func upsert(_ profile: ServerProfile, makeActive: Bool = false) {
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
    mutating func select(_ id: ServerProfileID) -> Bool {
        guard profile(id: id) != nil else { return false }
        activeProfileID = id
        return true
    }

    @discardableResult
    mutating func remove(id: ServerProfileID) -> ServerProfile? {
        guard let index = profiles.firstIndex(where: { $0.id == id }) else { return nil }
        let removed = profiles.remove(at: index)
        if activeProfileID == id {
            activeProfileID = profiles.first?.id
        }
        return removed
    }

    /// Converts the existing single-server settings without changing the legacy snapshot shape.
    /// A fixed ID makes the migration idempotent across retries and interrupted first launches.
    static func migratingLegacySettings(
        _ settings: AppSettings,
        hasStoredCredential: Bool,
        migrationDate: Date
    ) -> ServerProfileCatalog {
        let endpoint = settings.serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !endpoint.isEmpty else { return ServerProfileCatalog() }

        let name = settings.serverName.trimmingCharacters(in: .whitespacesAndNewlines)
        let profile = ServerProfile(
            id: .legacyMigration,
            name: name.isEmpty ? "My Ollama" : name,
            endpoint: endpoint,
            selectedModel: settings.selectedModel,
            authentication: hasStoredCredential ? .bearerToken : .none,
            createdAt: migrationDate
        )
        return ServerProfileCatalog(profiles: [profile], activeProfileID: profile.id)
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion
        case profiles
        case activeProfileID
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let decodedProfiles = try container.decodeIfPresent(
            [ServerProfile].self,
            forKey: .profiles
        ) ?? []
        let decodedActiveID = try container.decodeIfPresent(
            ServerProfileID.self,
            forKey: .activeProfileID
        )

        schemaVersion = max(
            try container.decodeIfPresent(Int.self, forKey: .schemaVersion) ?? 1,
            1
        )
        profiles = Self.canonicalProfiles(decodedProfiles)
        activeProfileID = Self.resolvedActiveID(decodedActiveID, in: profiles)
    }

    private static func canonicalProfiles(_ input: [ServerProfile]) -> [ServerProfile] {
        var result: [ServerProfile] = []
        var indices: [ServerProfileID: Int] = [:]

        for profile in input {
            if let index = indices[profile.id] {
                // Preserve the first position while letting the newest decoded value win.
                result[index] = profile
            } else {
                indices[profile.id] = result.count
                result.append(profile)
            }
        }
        return result
    }

    private static func resolvedActiveID(
        _ candidate: ServerProfileID?,
        in profiles: [ServerProfile]
    ) -> ServerProfileID? {
        if let candidate, profiles.contains(where: { $0.id == candidate }) {
            return candidate
        }
        return profiles.first?.id
    }
}

nonisolated extension ServerProfileID {
    /// Reserved for migration from the pre-profile, single-server settings schema.
    static let legacyMigration = ServerProfileID(
        rawValue: UUID(uuidString: "7df8c97c-381d-4b86-8de1-a177b8a9f40f")!
    )
}
