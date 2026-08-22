import Foundation

/// A validated base URL for an Ollama server on the local network.
public nonisolated struct OllamaEndpoint: Hashable, Sendable {
    public static let defaultPort = 11_434

    public let baseURL: URL

    /// True when the host is an RFC1918/link-local/unique-local address or a `.local` name.
    /// The gateway backend allows its token over cleartext HTTP only when this is true.
    public let isPrivateLANHost: Bool

    public init(_ rawValue: String) throws {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw OllamaClientError.emptyAddress
        }

        // Bare IPv6 unspecified/loopback addresses cannot be made unambiguous by simply adding a
        // scheme, so catch them before URLComponents parsing.
        let lowercaseInput = trimmed.lowercased()
        if ["::", "[::]", "::1", "[::1]"].contains(lowercaseInput) {
            throw OllamaClientError.localDeviceAddress(trimmed)
        }

        let hasExplicitScheme = lowercaseInput.contains("://")
        let address = hasExplicitScheme ? trimmed : "http://\(trimmed)"
        guard var components = URLComponents(string: address),
              let rawScheme = components.scheme,
              let rawHost = components.host
        else {
            throw OllamaClientError.invalidAddress(trimmed)
        }

        let scheme = rawScheme.lowercased()
        guard scheme == "http" || scheme == "https" else {
            throw OllamaClientError.unsupportedScheme(rawScheme)
        }

        guard components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil
        else {
            throw OllamaClientError.invalidAddress(trimmed)
        }

        var normalizedURLHost = rawHost.lowercased()
        if normalizedURLHost.hasSuffix(".") {
            normalizedURLHost.removeLast()
        }
        let validationHost: String
        if normalizedURLHost.hasPrefix("["), normalizedURLHost.hasSuffix("]") {
            validationHost = String(normalizedURLHost.dropFirst().dropLast())
        } else {
            validationHost = normalizedURLHost
        }

        if validationHost.hasSuffix(".local"), !NetworkAddress.isValidLocalHostname(validationHost) {
            throw OllamaClientError.invalidAddress(trimmed)
        }
        if NetworkAddress.isDeviceLocal(validationHost) {
            throw OllamaClientError.localDeviceAddress(validationHost)
        }
        let isSecurePublicReverseProxy = hasExplicitScheme
            && scheme == "https"
            && NetworkAddress.isValidPublicHostname(validationHost)
        guard NetworkAddress.isLAN(validationHost) || isSecurePublicReverseProxy else {
            throw OllamaClientError.nonLANAddress(validationHost)
        }

        let decodedPath = components.percentEncodedPath.removingPercentEncoding
            ?? components.percentEncodedPath
        let path = decodedPath == "/" ? "" : decodedPath
        guard path.isEmpty || path.lowercased() == "/api" || path.lowercased() == "/api/" else {
            throw OllamaClientError.unsupportedBasePath(path)
        }

        if let port = components.port {
            guard (1...65_535).contains(port) else {
                throw OllamaClientError.invalidPort(port)
            }
        } else if NetworkAddress.containsInvalidExplicitPort(address, host: rawHost) {
            throw OllamaClientError.invalidPort(nil)
        } else if scheme == "http" {
            components.port = Self.defaultPort
        }

        components.scheme = scheme
        components.host = normalizedURLHost
        components.percentEncodedPath = ""

        guard let normalizedURL = components.url else {
            throw OllamaClientError.invalidAddress(trimmed)
        }
        baseURL = normalizedURL
        isPrivateLANHost = NetworkAddress.isLAN(validationHost)
    }

    public var displayAddress: String {
        baseURL.absoluteString
    }

    func url(for route: OllamaRoute) -> URL {
        baseURL
            .appendingPathComponent("api", isDirectory: true)
            .appendingPathComponent(route.rawValue, isDirectory: false)
    }
}

nonisolated enum OllamaRoute: String, Sendable {
    case version
    case tags
    case show
    case pull
    case chat
}
