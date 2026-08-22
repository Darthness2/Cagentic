import Foundation
import Security

/// Credential material is intentionally not `Codable`, preventing accidental inclusion in app
/// snapshots. Additional authentication schemes can be added without changing the storage API.
nonisolated enum ServerCredential: Equatable, Sendable {
    case bearerToken(String)

    var bearerToken: String {
        switch self {
        case .bearerToken(let token): token
        }
    }
}

protocol ServerCredentialStoring: Sendable {
    func loadCredential(for profileID: ServerProfileID) async throws -> ServerCredential?
    func saveCredential(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws
    func saveCredentialIfAbsent(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws -> Bool
    func removeCredential(for profileID: ServerProfileID) async throws
    func reconcileCredentials(validProfileIDs: Set<ServerProfileID>) async throws
}

extension ServerCredentialStoring {
    func reconcileCredentials(validProfileIDs: Set<ServerProfileID>) async throws {}
}

nonisolated enum ServerCredentialStoreError: LocalizedError, Sendable {
    case unexpectedKeychainStatus(OSStatus)

    var errorDescription: String? {
        switch self {
        case .unexpectedKeychainStatus(let status):
            "Keychain returned status \(status)."
        }
    }
}

/// Stores one credential per immutable profile ID. Editable profile names and endpoints never
/// participate in Keychain lookup, so renaming a server cannot orphan its secret.
actor KeychainServerCredentialStore: ServerCredentialStoring {
    private let service: String
    private let accountPrefix: String

    init(
        service: String = "com.cagentic.mobile.ollama",
        accountPrefix: String = "server-profile-bearer-token"
    ) {
        self.service = service
        self.accountPrefix = accountPrefix
    }

    func loadCredential(for profileID: ServerProfileID) async throws -> ServerCredential? {
        try loadCredentialFromKeychain(for: profileID)
    }

    func saveCredential(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws {
        let token = credential.bearerToken
        guard !token.isEmpty else {
            try removeCredentialFromKeychain(for: profileID)
            return
        }

        let valueData = Data(token.utf8)
        let update: [String: Any] = [
            kSecValueData as String: valueData,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let updateStatus = SecItemUpdate(
            baseQuery(for: profileID) as CFDictionary,
            update as CFDictionary
        )
        if updateStatus == errSecSuccess {
            return
        }
        guard updateStatus == errSecItemNotFound else {
            throw ServerCredentialStoreError.unexpectedKeychainStatus(updateStatus)
        }

        let addStatus = SecItemAdd(
            addQuery(valueData: valueData, for: profileID) as CFDictionary,
            nil
        )
        if addStatus == errSecDuplicateItem {
            // Another store instance may have inserted the account after our first update.
            let retryStatus = SecItemUpdate(
                baseQuery(for: profileID) as CFDictionary,
                update as CFDictionary
            )
            guard retryStatus == errSecSuccess else {
                throw ServerCredentialStoreError.unexpectedKeychainStatus(retryStatus)
            }
            return
        }
        guard addStatus == errSecSuccess else {
            throw ServerCredentialStoreError.unexpectedKeychainStatus(addStatus)
        }
    }

    func saveCredentialIfAbsent(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws -> Bool {
        let token = credential.bearerToken
        guard !token.isEmpty else { return false }

        let status = SecItemAdd(
            addQuery(valueData: Data(token.utf8), for: profileID) as CFDictionary,
            nil
        )
        if status == errSecDuplicateItem {
            return false
        }
        guard status == errSecSuccess else {
            throw ServerCredentialStoreError.unexpectedKeychainStatus(status)
        }
        return true
    }

    func removeCredential(for profileID: ServerProfileID) async throws {
        try removeCredentialFromKeychain(for: profileID)
    }

    func reconcileCredentials(validProfileIDs: Set<ServerProfileID>) async throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecReturnAttributes as String: true,
            kSecMatchLimit as String: kSecMatchLimitAll,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            return
        }
        guard status == errSecSuccess else {
            throw ServerCredentialStoreError.unexpectedKeychainStatus(status)
        }

        let attributes: [[String: Any]]
        if let many = item as? [[String: Any]] {
            attributes = many
        } else if let one = item as? [String: Any] {
            attributes = [one]
        } else {
            attributes = []
        }
        let expectedPrefix = "\(accountPrefix)."
        for attribute in attributes {
            guard let account = attribute[kSecAttrAccount as String] as? String,
                  account.hasPrefix(expectedPrefix),
                  let uuid = UUID(uuidString: String(account.dropFirst(expectedPrefix.count)))
            else {
                continue
            }
            let profileID = ServerProfileID(rawValue: uuid)
            if !validProfileIDs.contains(profileID) {
                try removeCredentialFromKeychain(for: profileID)
            }
        }
    }

    nonisolated static func accountName(
        prefix: String = "server-profile-bearer-token",
        profileID: ServerProfileID
    ) -> String {
        "\(prefix).\(profileID.description)"
    }

    private func loadCredentialFromKeychain(
        for profileID: ServerProfileID
    ) throws -> ServerCredential? {
        var query = baseQuery(for: profileID)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw ServerCredentialStoreError.unexpectedKeychainStatus(status)
        }
        guard let data = item as? Data else {
            return nil
        }
        return .bearerToken(String(decoding: data, as: UTF8.self))
    }

    private func removeCredentialFromKeychain(for profileID: ServerProfileID) throws {
        let status = SecItemDelete(baseQuery(for: profileID) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw ServerCredentialStoreError.unexpectedKeychainStatus(status)
        }
    }

    private func addQuery(valueData: Data, for profileID: ServerProfileID) -> [String: Any] {
        var query = baseQuery(for: profileID)
        query[kSecValueData as String] = valueData
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return query
    }

    private func baseQuery(for profileID: ServerProfileID) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: Self.accountName(
                prefix: accountPrefix,
                profileID: profileID
            ),
        ]
    }
}

/// A deterministic actor-backed implementation for previews and tests.
actor InMemoryServerCredentialStore: ServerCredentialStoring {
    private var credentials: [ServerProfileID: ServerCredential]

    init(credentials: [ServerProfileID: ServerCredential] = [:]) {
        self.credentials = credentials
    }

    func loadCredential(for profileID: ServerProfileID) async throws -> ServerCredential? {
        credentials[profileID]
    }

    func saveCredential(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws {
        if credential.bearerToken.isEmpty {
            credentials.removeValue(forKey: profileID)
        } else {
            credentials[profileID] = credential
        }
    }

    func saveCredentialIfAbsent(
        _ credential: ServerCredential,
        for profileID: ServerProfileID
    ) async throws -> Bool {
        guard !credential.bearerToken.isEmpty, credentials[profileID] == nil else {
            return false
        }
        credentials[profileID] = credential
        return true
    }

    func removeCredential(for profileID: ServerProfileID) async throws {
        credentials.removeValue(forKey: profileID)
    }

    func reconcileCredentials(validProfileIDs: Set<ServerProfileID>) async throws {
        credentials = credentials.filter { validProfileIDs.contains($0.key) }
    }
}

/// Copies the legacy fixed-account token without deleting it, allowing old and new app code to
/// coexist during a staged migration. `saveCredentialIfAbsent` prevents concurrent retries from
/// overwriting a credential already entered for the profile.
nonisolated enum ServerCredentialMigration {
    static func copyLegacyTokenIfNeeded(
        from legacyStore: any TokenStoring,
        to profileStore: any ServerCredentialStoring,
        profileID: ServerProfileID
    ) async throws -> Bool {
        let legacyToken = try await legacyStore.loadToken()
        guard !legacyToken.isEmpty else { return false }
        return try await profileStore.saveCredentialIfAbsent(
            .bearerToken(legacyToken),
            for: profileID
        )
    }
}
