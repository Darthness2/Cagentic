import Foundation

/// Errors surfaced by the direct Ollama client.
///
/// Each case is intentionally presentation-ready so a settings or chat screen can show a useful
/// recovery step without interpreting transport internals.
public nonisolated enum OllamaClientError: Error, Equatable, LocalizedError, Sendable {
    case emptyAddress
    case invalidAddress(String)
    case unsupportedScheme(String)
    case localDeviceAddress(String)
    case nonLANAddress(String)
    case unsupportedBasePath(String)
    case invalidPort(Int?)
    case insecureBearerToken
    case credentialReentryRequired
    case emptyModelName
    case modelPullUnsupported
    case requestEncoding(reason: String)
    case invalidResponse
    case responseTooLarge(endpoint: String, limitBytes: Int)
    case transport(code: URLError.Code?, message: String)
    case httpStatus(code: Int, message: String?)
    case malformedResponse(endpoint: String, reason: String)
    case malformedStreamChunk(excerpt: String, reason: String)
    case chatRequestTooLarge(limitBytes: Int)
    case chatResponseTooLarge(limitBytes: Int)
    case streamLineTooLarge(limitBytes: Int)
    case streamBufferOverflow(limit: Int)
    case server(message: String)
    case streamEndedBeforeCompletion

    public var errorDescription: String? {
        switch self {
        case .emptyAddress:
            "Enter the address of the PC running Ollama."
        case let .invalidAddress(address):
            "\(address) is not a valid Ollama server address."
        case let .unsupportedScheme(scheme):
            "Ollama connections must use HTTP or HTTPS, not \(scheme)."
        case let .localDeviceAddress(host):
            "\(host) points back to this device, not to the computer running Ollama."
        case let .nonLANAddress(host):
            "\(host) is not a private LAN address or a .local hostname."
        case let .unsupportedBasePath(path):
            "The Ollama address has an unsupported path: \(path)."
        case let .invalidPort(port):
            if let port {
                "\(port) is not a valid TCP port."
            } else {
                "The Ollama address contains an invalid TCP port."
            }
        case .insecureBearerToken:
            "A Bearer token cannot be sent over an unencrypted HTTP connection."
        case .credentialReentryRequired:
            "A saved Bearer token cannot be reused for a different server address."
        case .emptyModelName:
            "Choose an Ollama model before sending a request."
        case .modelPullUnsupported:
            "This Ollama connection does not support installing models from the app."
        case let .requestEncoding(reason):
            "Could not prepare the Ollama request: \(reason)"
        case .invalidResponse:
            "The server returned a response that was not valid HTTP."
        case let .responseTooLarge(endpoint, limitBytes):
            "Ollama returned more than \(limitBytes) bytes from \(endpoint)."
        case let .transport(_, message):
            "Could not reach Ollama: \(message)"
        case let .httpStatus(code, message):
            if let message, !message.isEmpty {
                "Ollama returned HTTP \(code): \(message)"
            } else {
                "Ollama returned HTTP \(code)."
            }
        case let .malformedResponse(endpoint, reason):
            "Ollama returned an invalid response from \(endpoint): \(reason)"
        case let .malformedStreamChunk(_, reason):
            "Ollama returned an invalid streaming response: \(reason)"
        case let .chatRequestTooLarge(limitBytes):
            "The Ollama chat request exceeded the \(limitBytes)-byte safety limit."
        case let .chatResponseTooLarge(limitBytes):
            "The Ollama chat response exceeded the \(limitBytes)-byte safety limit."
        case let .streamLineTooLarge(limitBytes):
            "An Ollama streaming record exceeded the \(limitBytes)-byte safety limit."
        case let .streamBufferOverflow(limit):
            "The app could not process Ollama's response without exceeding its \(limit)-event buffer."
        case let .server(message):
            "Ollama reported an error: \(message)"
        case .streamEndedBeforeCompletion:
            "The Ollama response stopped before it sent a completion record."
        }
    }

    public var recoverySuggestion: String? {
        switch self {
        case .emptyAddress, .invalidAddress:
            "Use your PC's LAN address, for example 192.168.1.42 or studio-pc.local."
        case .unsupportedScheme:
            "Remove the scheme to use HTTP automatically, or enter an http:// or https:// URL."
        case .localDeviceAddress:
            "Use the computer's LAN IP or .local hostname. Loopback addresses cannot reach it from this device."
        case .nonLANAddress:
            "Use a private IP or .local hostname on your LAN. A public reverse proxy must be entered with an explicit https:// URL."
        case .unsupportedBasePath:
            "Use only the server root or a trailing /api, for example http://192.168.1.42:11434/api."
        case .invalidPort:
            "Use a port from 1 through 65535. Ollama's default is 11434."
        case .insecureBearerToken:
            "Use an https:// Ollama address, or remove the Bearer token for this connection."
        case .credentialReentryRequired:
            "Re-enter the token for the new address, or explicitly connect without one."
        case .emptyModelName:
            "Refresh the model list and select a model installed on the PC."
        case .modelPullUnsupported:
            "Run ollama pull on the computer, then refresh the model list."
        case .requestEncoding:
            "Review the model settings and message content, then try again."
        case .transport:
            "Check that both devices are on the same network, Ollama is listening on the LAN, and the PC firewall allows the configured server port."
        case let .httpStatus(code, _):
            switch code {
            case 401, 403:
                "Check the Bearer token configured for this connection."
            case 404:
                "Check the server address and confirm the selected model is installed."
            default:
                "Verify Ollama is running, then try again."
            }
        case .invalidResponse, .malformedResponse, .malformedStreamChunk:
            "Confirm the address points to an Ollama server rather than another web service."
        case .chatRequestTooLarge:
            "Start a new chat or send fewer attachments, then try again."
        case .responseTooLarge, .chatResponseTooLarge, .streamLineTooLarge:
            "Confirm the address points to Ollama and reduce the requested response size before retrying."
        case .streamBufferOverflow:
            "Retry the message. If this continues, reduce the model's output length."
        case .server:
            "Check that the selected model is installed and that the PC has enough free memory, then retry."
        case .streamEndedBeforeCompletion:
            "Check the PC's connection and Ollama logs, then retry the message."
        }
    }
}
