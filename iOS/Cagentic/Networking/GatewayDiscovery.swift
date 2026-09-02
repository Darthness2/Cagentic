import Foundation
import Network

/// A Cagentic gateway seen advertising itself on the local network.
public nonisolated struct DiscoveredGateway: Identifiable, Equatable, Sendable {
    /// The Bonjour instance name, which is the computer's name as a person reads it.
    public let name: String
    /// The version the gateway advertises, when it publishes one.
    public let version: String?
    /// Kept so the endpoint can be resolved on demand; browsing alone does not
    /// yield an address, only a service to connect to.
    let endpoint: NWEndpoint

    public var id: String { name }

    public static func == (lhs: DiscoveredGateway, rhs: DiscoveredGateway) -> Bool {
        lhs.name == rhs.name && lhs.version == rhs.version && lhs.endpoint == rhs.endpoint
    }
}

/// Watches the local network for gateways advertising `_cagentic._tcp`.
///
/// Bonjour rather than a broadcast of our own: since iOS 14, sending directly to
/// a broadcast or multicast address requires an entitlement Apple grants case by
/// case, while browsing through the system's own responder needs only the local
/// network permission this app already asks for.
@MainActor
@Observable
public final class GatewayDiscovery {
    public private(set) var gateways: [DiscoveredGateway] = []
    public private(set) var isSearching = false
    /// Set when the browser itself fails, which on this platform almost always
    /// means local network access was refused.
    public private(set) var failure: String?

    @ObservationIgnored private var browser: NWBrowser?

    public init() {}

    deinit {
        browser?.cancel()
    }

    public func start() {
        guard browser == nil else { return }
        failure = nil
        isSearching = true

        let parameters = NWParameters()
        parameters.includePeerToPeer = false
        let descriptor = NWBrowser.Descriptor.bonjourWithTXTRecord(
            type: "_cagentic._tcp",
            domain: nil
        )
        let browser = NWBrowser(for: descriptor, using: parameters)

        browser.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in
                guard let self else { return }
                switch state {
                case .failed(let error):
                    self.failure = Self.message(for: error)
                    self.isSearching = false
                    self.browser?.cancel()
                    self.browser = nil
                case .cancelled:
                    self.isSearching = false
                case .ready:
                    self.failure = nil
                default:
                    break
                }
            }
        }
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            let found = results.compactMap(Self.gateway(from:))
                .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
            Task { @MainActor in
                self?.gateways = found
            }
        }

        self.browser = browser
        browser.start(queue: .main)
    }

    public func stop() {
        browser?.cancel()
        browser = nil
        isSearching = false
    }

    /// Turns a browse result into an address the HTTP client can use.
    ///
    /// A Bonjour result names a service, not a host: the address only exists
    /// once something connects. Opening a short-lived connection and reading the
    /// path it resolved to is the supported way to learn it.
    public func resolve(_ gateway: DiscoveredGateway) async throws -> GatewayEndpoint {
        let resolved: (host: String, port: UInt16) = try await withCheckedThrowingContinuation {
            continuation in
            let connection = NWConnection(to: gateway.endpoint, using: .tcp)
            let hasResumed = ResumeGuard()

            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    guard let remote = connection.currentPath?.remoteEndpoint,
                          case .hostPort(let host, let port) = remote,
                          let address = Self.address(from: host)
                    else {
                        if hasResumed.claim() {
                            continuation.resume(throwing: GatewayClientError.invalidResponse)
                        }
                        connection.cancel()
                        return
                    }
                    if hasResumed.claim() {
                        continuation.resume(returning: (address, port.rawValue))
                    }
                    connection.cancel()
                case .failed(let error):
                    if hasResumed.claim() {
                        continuation.resume(
                            throwing: GatewayClientError.transport(
                                code: nil,
                                message: error.localizedDescription
                            )
                        )
                    }
                    connection.cancel()
                case .cancelled:
                    if hasResumed.claim() {
                        continuation.resume(throwing: CancellationError())
                    }
                default:
                    break
                }
            }
            connection.start(queue: .global(qos: .userInitiated))
        }

        do {
            return try GatewayEndpoint("http://\(resolved.host):\(resolved.port)")
        } catch GatewayClientError.localDeviceAddress {
            // The advertisement resolved to this device. That means whatever published the
            // service pointed its SRV record at a hostname the machine's own responder also
            // answers for — those answers include 127.0.0.1 and ::1, which name the phone.
            // Say so plainly rather than reporting "cannot reach the gateway".
            throw GatewayClientError.malformedResponse(
                endpoint: "_cagentic._tcp",
                reason: "the computer advertised an address that points back at this device. "
                    + "Update Cagentic on the computer, then run /token again."
            )
        }
    }

    // MARK: - Helpers

    private nonisolated static func gateway(from result: NWBrowser.Result) -> DiscoveredGateway? {
        guard case .service(let name, _, _, _) = result.endpoint else { return nil }
        var version: String?
        if case .bonjour(let record) = result.metadata {
            version = record.getEntry(for: "v").flatMap { entry in
                if case .string(let value) = entry { return value }
                return nil
            }
        }
        return DiscoveredGateway(name: name, version: version, endpoint: result.endpoint)
    }

    private nonisolated static func address(from host: NWEndpoint.Host) -> String? {
        switch host {
        case .ipv4(let address):
            return String(describing: address).split(separator: "%").first.map(String.init)
        case .ipv6(let address):
            // A literal IPv6 address has to be bracketed inside a URL.
            let text = String(describing: address).split(separator: "%").first.map(String.init)
            return text.map { "[\($0)]" }
        case .name(let name, _):
            return name
        @unknown default:
            return nil
        }
    }

    private nonisolated static func message(for error: NWError) -> String {
        // The browser fails this way when the user has denied local network
        // access, and the generic text does not say so.
        if case .posix(let code) = error, code == .EPERM || code == .EACCES {
            return "Cagentic needs permission to find devices on your network. "
                + "Allow it in Settings › Privacy & Security › Local Network."
        }
        return error.localizedDescription
    }
}

/// Guards a continuation against the several connection states that can each
/// try to finish the same resolution.
private nonisolated final class ResumeGuard: @unchecked Sendable {
    private let lock = NSLock()
    private var used = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if used { return false }
        used = true
        return true
    }
}
