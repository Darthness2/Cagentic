import Foundation

/// Errors surfaced by the Cagentic gateway client.
///
/// Written to the same rule as `OllamaClientError`: every case is presentation-ready, so a settings
/// or chat screen can show a useful recovery step without interpreting transport internals.
public nonisolated enum GatewayClientError: Error, Equatable, LocalizedError, Sendable {
    case emptyAddress
    case invalidAddress(String)
    case unsupportedScheme(String)
    case localDeviceAddress(String)
    case nonLANAddress(String)
    case unsupportedBasePath(String)
    case invalidPort(Int?)
    case missingToken
    case insecureTokenTransport(host: String)
    case requestEncoding(reason: String)
    case invalidResponse
    case unauthorized
    case busy(message: String)
    case httpStatus(code: Int, message: String?)
    case malformedResponse(endpoint: String, reason: String)
    case malformedStreamFrame(excerpt: String, reason: String)
    case responseTooLarge(endpoint: String, limitBytes: Int)
    case streamFrameTooLarge(limitBytes: Int)
    case streamResponseTooLarge(limitBytes: Int)
    case streamBufferOverflow(limit: Int)
    case streamEndedBeforeCompletion
    case transport(code: URLError.Code?, message: String)
    case server(message: String)

    /// True when the gateway refused the turn before generating anything, so the composer draft can
    /// be restored instead of the message being shown as sent. Mirrors the web client's check on the
    /// `error` event text (`app.js` sniffs "still working"/"session is busy").
    public var isTurnRejection: Bool {
        switch self {
        case .busy:
            true
        default:
            false
        }
    }

    public var errorDescription: String? {
        switch self {
        case .emptyAddress:
            "Enter the address of the computer running the Cagentic gateway."
        case let .invalidAddress(address):
            "\(address) is not a valid Cagentic gateway address."
        case let .unsupportedScheme(scheme):
            "Gateway connections must use HTTP or HTTPS, not \(scheme)."
        case let .localDeviceAddress(host):
            "\(host) points back to this device, not to the computer running the gateway."
        case let .nonLANAddress(host):
            "\(host) is not a private LAN address or a .local hostname."
        case let .unsupportedBasePath(path):
            "The gateway address has an unsupported path: \(path)."
        case let .invalidPort(port):
            if let port {
                "\(port) is not a valid TCP port."
            } else {
                "The gateway address contains an invalid TCP port."
            }
        case .missingToken:
            "The Cagentic gateway requires an access token."
        case let .insecureTokenTransport(host):
            "The gateway token cannot be sent to \(host) over an unencrypted connection."
        case let .requestEncoding(reason):
            "Could not prepare the gateway request: \(reason)"
        case .invalidResponse:
            "The gateway returned a response that was not valid HTTP."
        case .unauthorized:
            "The gateway rejected this device's access token."
        case let .busy(message):
            message.isEmpty ? "The gateway is still working on the previous message." : message
        case let .httpStatus(code, message):
            if let message, !message.isEmpty {
                "The gateway returned HTTP \(code): \(message)"
            } else {
                "The gateway returned HTTP \(code)."
            }
        case let .malformedResponse(endpoint, reason):
            "The gateway returned an invalid response from \(endpoint): \(reason)"
        case let .malformedStreamFrame(_, reason):
            "The gateway returned an invalid streaming response: \(reason)"
        case let .responseTooLarge(endpoint, limitBytes):
            "The gateway returned more than \(limitBytes) bytes from \(endpoint)."
        case let .streamFrameTooLarge(limitBytes):
            "A gateway streaming record exceeded the \(limitBytes)-byte safety limit."
        case let .streamResponseTooLarge(limitBytes):
            "The gateway response exceeded the \(limitBytes)-byte safety limit."
        case let .streamBufferOverflow(limit):
            "The app could not process the gateway's response without exceeding its \(limit)-event buffer."
        case .streamEndedBeforeCompletion:
            "The gateway response stopped before the turn finished."
        case let .transport(_, message):
            "Could not reach the Cagentic gateway: \(message)"
        case let .server(message):
            "The gateway reported an error: \(message)"
        }
    }

    public var recoverySuggestion: String? {
        switch self {
        case .emptyAddress, .invalidAddress:
            "Use your computer's LAN address and gateway port, for example 192.168.1.42:8700."
        case .unsupportedScheme:
            "Remove the scheme to use HTTP automatically, or enter an http:// or https:// URL."
        case .localDeviceAddress:
            "Use the computer's LAN IP or .local hostname. Loopback addresses cannot reach it from this device."
        case .nonLANAddress:
            "Use a private IP or .local hostname on your LAN. A gateway reached over the internet must be entered with an explicit https:// URL."
        case .unsupportedBasePath:
            "Use only the server root, for example http://192.168.1.42:8700."
        case .invalidPort:
            "Use a port from 1 through 65535. The gateway's default is 8700."
        case .missingToken:
            "Set gateway.token in ~/.config/cagentic/config.json on the computer, then enter the same value here."
        case .insecureTokenTransport:
            "Connect to the gateway on your private network, or put it behind an https:// reverse proxy."
        case .requestEncoding:
            "Shorten the message and try again."
        case .unauthorized:
            "Confirm gateway.token in ~/.config/cagentic/config.json matches the token entered here, and that the gateway was restarted after changing it."
        case .busy:
            "The gateway handles one turn at a time. Wait for the current reply to finish, then try again."
        case let .httpStatus(code, _):
            switch code {
            case 401, 403:
                "Check the access token configured for this connection."
            case 404:
                "Check the address and port, and confirm the computer is running a Cagentic gateway."
            default:
                "Confirm the gateway is running, then try again."
            }
        case .invalidResponse, .malformedResponse, .malformedStreamFrame:
            "Confirm the address points to a Cagentic gateway rather than another web service."
        case .responseTooLarge, .streamFrameTooLarge, .streamResponseTooLarge:
            "Start a new chat on the gateway, then retry."
        case .streamBufferOverflow:
            "Retry the message. If this continues, restart the gateway."
        case .streamEndedBeforeCompletion:
            "Check the computer's connection and the gateway log, then retry the message."
        case .transport:
            "Check that both devices are on the same network, that gateway.lan is true in the gateway config, and that the computer's firewall allows the gateway port."
        case .server:
            "Check the gateway log on the computer for details, then retry."
        }
    }
}
