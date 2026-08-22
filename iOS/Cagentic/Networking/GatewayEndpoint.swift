import Foundation

/// A validated base URL for a Cagentic gateway.
///
/// Deliberately a separate type from `OllamaEndpoint` rather than a mode on it: the two backends
/// disagree about the default port, about which paths are legal, and — most importantly — about
/// whether a credential may travel over cleartext. Folding both into one initializer would make the
/// security rule depend on a flag that is easy to pass wrongly.
public nonisolated struct GatewayEndpoint: Hashable, Sendable {
    public static let defaultPort = 8_700

    public let baseURL: URL

    /// True when the host is an RFC1918/link-local/unique-local address or a `.local` name.
    public let isPrivateLANHost: Bool

    public init(_ rawValue: String) throws {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw GatewayClientError.emptyAddress
        }

        // Bare IPv6 unspecified/loopback literals cannot be disambiguated by prefixing a scheme.
        let lowercaseInput = trimmed.lowercased()
        if ["::", "[::]", "::1", "[::1]"].contains(lowercaseInput) {
            throw GatewayClientError.localDeviceAddress(trimmed)
        }

        let hasExplicitScheme = lowercaseInput.contains("://")
        let address = hasExplicitScheme ? trimmed : "http://\(trimmed)"
        guard var components = URLComponents(string: address),
              let rawScheme = components.scheme,
              let rawHost = components.host
        else {
            throw GatewayClientError.invalidAddress(trimmed)
        }

        let scheme = rawScheme.lowercased()
        guard scheme == "http" || scheme == "https" else {
            throw GatewayClientError.unsupportedScheme(rawScheme)
        }

        // A token in the query string would be logged by every proxy in the path, and userinfo
        // credentials are never appropriate here.
        guard components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil
        else {
            throw GatewayClientError.invalidAddress(trimmed)
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
            throw GatewayClientError.invalidAddress(trimmed)
        }
        if NetworkAddress.isDeviceLocal(validationHost) {
            throw GatewayClientError.localDeviceAddress(validationHost)
        }
        let isLAN = NetworkAddress.isLAN(validationHost)
        // A gateway reached over the public internet must be an explicit https:// URL — the token it
        // carries grants shell, file, and browser control of the host machine.
        let isSecurePublicHost = hasExplicitScheme
            && scheme == "https"
            && NetworkAddress.isValidPublicHostname(validationHost)
        guard isLAN || isSecurePublicHost else {
            throw GatewayClientError.nonLANAddress(validationHost)
        }

        let decodedPath = components.percentEncodedPath.removingPercentEncoding
            ?? components.percentEncodedPath
        let path = decodedPath == "/" ? "" : decodedPath
        guard path.isEmpty || path.lowercased() == "/api" || path.lowercased() == "/api/" else {
            throw GatewayClientError.unsupportedBasePath(path)
        }

        if let port = components.port {
            guard (1...65_535).contains(port) else {
                throw GatewayClientError.invalidPort(port)
            }
        } else if NetworkAddress.containsInvalidExplicitPort(address, host: rawHost) {
            throw GatewayClientError.invalidPort(nil)
        } else if scheme == "http" {
            // The gateway is almost never on port 80, so an address typed without a port means the
            // default gateway port. https keeps URL semantics (443) because that shape implies a
            // reverse proxy the user configured deliberately.
            components.port = Self.defaultPort
        }

        components.scheme = scheme
        components.host = normalizedURLHost
        components.percentEncodedPath = ""

        guard let normalizedURL = components.url else {
            throw GatewayClientError.invalidAddress(trimmed)
        }
        baseURL = normalizedURL
        isPrivateLANHost = isLAN
    }

    public var displayAddress: String {
        baseURL.absoluteString
    }

    public var isEncrypted: Bool {
        baseURL.scheme?.lowercased() == "https"
    }

    /// Whether this endpoint may carry the gateway token.
    ///
    /// The gateway speaks plain HTTP and has no TLS of its own, so refusing cleartext outright would
    /// make the feature unusable on a home network. Instead the token is allowed to travel
    /// unencrypted only inside a private network the user controls; anything routable needs https.
    public var allowsToken: Bool {
        isEncrypted || isPrivateLANHost
    }

    /// True when the token will cross the network unencrypted, so the UI can say so plainly.
    public var sendsTokenInClear: Bool {
        !isEncrypted && isPrivateLANHost
    }

    func url(for route: GatewayRoute) -> URL {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            return baseURL
        }
        components.percentEncodedPath = "/api/" + route.path
        return components.url ?? baseURL
    }
}

/// The gateway routes this client speaks. Deliberately limited to the v1 scope: chat, permissions,
/// abort, and the chat list. Everything else the gateway exposes stays out of the app for now.
nonisolated enum GatewayRoute: Sendable, Equatable {
    case bootstrap
    case chat
    case abort
    case permission
    case model
    case chatsNew
    case chatsLoad
    case chatsDelete
    case chatsRename

    var path: String {
        switch self {
        case .bootstrap: "bootstrap"
        case .chat: "chat"
        case .abort: "abort"
        case .permission: "permission"
        case .model: "model"
        case .chatsNew: "chats/new"
        case .chatsLoad: "chats/load"
        case .chatsDelete: "chats/delete"
        case .chatsRename: "chats/rename"
        }
    }

    var method: String {
        switch self {
        case .bootstrap: "GET"
        default: "POST"
        }
    }
}
