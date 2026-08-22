import Foundation
import Security

protocol TokenStoring: Sendable {
    func loadToken() async throws -> String
    func saveToken(_ token: String) async throws
}

enum KeychainTokenError: LocalizedError {
    case unexpectedStatus(OSStatus)

    var errorDescription: String? {
        switch self {
        case .unexpectedStatus(let status):
            "Keychain returned status \(status)."
        }
    }
}

actor KeychainTokenStore: TokenStoring {
    private let service: String
    private let account: String

    init(
        service: String = "com.cagentic.mobile.ollama",
        account: String = "active-server-bearer-token"
    ) {
        self.service = service
        self.account = account
    }

    func loadToken() async throws -> String {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            return ""
        }
        guard status == errSecSuccess else {
            throw KeychainTokenError.unexpectedStatus(status)
        }
        guard let data = item as? Data else {
            return ""
        }
        return String(decoding: data, as: UTF8.self)
    }

    func saveToken(_ token: String) async throws {
        guard !token.isEmpty else {
            let deleteStatus = SecItemDelete(baseQuery as CFDictionary)
            guard deleteStatus == errSecSuccess || deleteStatus == errSecItemNotFound else {
                throw KeychainTokenError.unexpectedStatus(deleteStatus)
            }
            return
        }

        let valueData = Data(token.utf8)
        let update: [String: Any] = [
            kSecValueData as String: valueData,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let updateStatus = SecItemUpdate(baseQuery as CFDictionary, update as CFDictionary)
        if updateStatus == errSecSuccess {
            return
        }
        guard updateStatus == errSecItemNotFound else {
            throw KeychainTokenError.unexpectedStatus(updateStatus)
        }

        var query = baseQuery
        query[kSecValueData as String] = valueData
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeychainTokenError.unexpectedStatus(status)
        }
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

actor InMemoryTokenStore: TokenStoring {
    private var token: String

    init(token: String = "") {
        self.token = token
    }

    func loadToken() async throws -> String {
        token
    }

    func saveToken(_ token: String) async throws {
        self.token = token
    }
}
