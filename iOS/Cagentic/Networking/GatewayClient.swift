import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public typealias GatewayEventStream = AsyncThrowingStream<GatewayEvent, Error>

nonisolated struct GatewayClientLimits: Equatable, Sendable {
    static let standard = GatewayClientLimits(
        maximumJSONResponseBytes: 16 * 1_024 * 1_024,
        maximumChatRequestBytes: 1 * 1_024 * 1_024,
        maximumStreamBytes: 64 * 1_024 * 1_024,
        // A single frame can legitimately be large: `tool_result` carries the tool's full output,
        // and a browser screenshot arrives as base64 inside it.
        maximumFrameBytes: 8 * 1_024 * 1_024,
        maximumBufferedEvents: 128
    )

    let maximumJSONResponseBytes: Int
    let maximumChatRequestBytes: Int
    let maximumStreamBytes: Int
    let maximumFrameBytes: Int
    let maximumBufferedEvents: Int

    init(
        maximumJSONResponseBytes: Int,
        maximumChatRequestBytes: Int,
        maximumStreamBytes: Int,
        maximumFrameBytes: Int,
        maximumBufferedEvents: Int
    ) {
        precondition(maximumJSONResponseBytes > 0)
        precondition(maximumChatRequestBytes > 0)
        precondition(maximumStreamBytes > 0)
        precondition(maximumFrameBytes > 0)
        precondition(maximumBufferedEvents > 0)
        self.maximumJSONResponseBytes = maximumJSONResponseBytes
        self.maximumChatRequestBytes = maximumChatRequestBytes
        self.maximumStreamBytes = maximumStreamBytes
        self.maximumFrameBytes = maximumFrameBytes
        self.maximumBufferedEvents = maximumBufferedEvents
    }
}

/// The chat list plus the gateway's live chat, which several routes return together.
public nonisolated struct GatewayChatsSnapshot: Equatable, Sendable {
    public let chats: [GatewayChatSummary]
    public let current: GatewayChatDetail?

    public init(chats: [GatewayChatSummary], current: GatewayChatDetail?) {
        self.chats = chats
        self.current = current
    }
}

/// Store-facing abstraction for live, preview, and test gateway implementations.
///
/// Deliberately separate from `OllamaServing`: the two backends share no request shape, and the
/// gateway owns its own conversation history rather than replaying one the app assembles.
public nonisolated protocol GatewayServing: Sendable {
    func bootstrap() async throws -> GatewayBootstrap
    func chat(message: String) -> GatewayEventStream
    func answerPermission(id: String, answer: GatewayPermissionAnswer, rule: String?) async throws
    func abort() async throws
    func newChat() async throws -> GatewayChatsSnapshot
    func loadChat(id: String) async throws -> GatewayChatsSnapshot
    func deleteChat(id: String) async throws -> GatewayChatsSnapshot
    func renameChat(id: String, title: String) async throws -> [GatewayChatSummary]
    func selectModel(_ model: String) async throws -> String
}

/// Direct client for a Cagentic gateway reachable on the local network.
public nonisolated struct GatewayClient: GatewayServing, Sendable {
    private let session: URLSession
    private let requestFactory: GatewayRequestFactory
    private let limits: GatewayClientLimits

    public init(
        endpoint: GatewayEndpoint,
        token: String,
        session: URLSession = .shared
    ) {
        self.init(endpoint: endpoint, token: token, session: session, limits: .standard)
    }

    init(
        endpoint: GatewayEndpoint,
        token: String,
        session: URLSession,
        limits: GatewayClientLimits
    ) {
        self.session = session
        self.limits = limits
        requestFactory = GatewayRequestFactory(endpoint: endpoint, token: token)
    }

    public func bootstrap() async throws -> GatewayBootstrap {
        let payload = try await send(
            route: .bootstrap,
            responseType: GatewayBootstrapPayload.self
        )
        return payload.value
    }

    public func answerPermission(
        id: String,
        answer: GatewayPermissionAnswer,
        rule: String?
    ) async throws {
        var body: [String: String] = ["id": id, "answer": answer.rawValue]
        // The gateway installs a rule only when it is the exact string that prompt offered, so it
        // is echoed verbatim and only alongside the answer that asks for it.
        if answer == .allowRule, let rule, !rule.isEmpty {
            body["rule"] = rule
        }
        _ = try await send(
            route: .permission,
            body: try Self.encode(body),
            responseType: GatewayErrorPayload.self
        )
    }

    public func abort() async throws {
        _ = try await send(
            route: .abort,
            body: try Self.encode([String: String]()),
            responseType: GatewayErrorPayload.self
        )
    }

    public func newChat() async throws -> GatewayChatsSnapshot {
        try await chatsRequest(route: .chatsNew, body: [String: String]())
    }

    public func loadChat(id: String) async throws -> GatewayChatsSnapshot {
        try await chatsRequest(route: .chatsLoad, body: ["id": id])
    }

    public func deleteChat(id: String) async throws -> GatewayChatsSnapshot {
        try await chatsRequest(route: .chatsDelete, body: ["id": id])
    }

    public func renameChat(id: String, title: String) async throws -> [GatewayChatSummary] {
        let snapshot = try await chatsRequest(
            route: .chatsRename,
            body: ["id": id, "title": title]
        )
        return snapshot.chats
    }

    public func selectModel(_ model: String) async throws -> String {
        let payload = try await send(
            route: .model,
            body: try Self.encode(["model": model]),
            responseType: ModelResponse.self
        )
        // This route reports failure inside an HTTP 200 body.
        if let error = payload.error, !error.isEmpty {
            throw GatewayClientError.server(message: error)
        }
        return payload.model ?? model
    }

    public func chat(message: String) -> GatewayEventStream {
        AsyncThrowingStream(
            bufferingPolicy: .bufferingOldest(limits.maximumBufferedEvents)
        ) { continuation in
            let task = Task { @concurrent in
                do {
                    // Only `message`. Sending `id` without the Collama client marker routes the turn
                    // down a path that never releases the gateway's turn lock, wedging it until the
                    // process restarts.
                    let body = try Self.encode(["message": message])
                    guard body.count <= limits.maximumChatRequestBytes else {
                        throw GatewayClientError.requestEncoding(
                            reason: "the message exceeds \(limits.maximumChatRequestBytes) bytes"
                        )
                    }
                    let urlRequest = try requestFactory.makeRequest(
                        route: .chat,
                        body: body,
                        accept: "text/event-stream",
                        // The stream has no heartbeat: an approval prompt parks it for up to five
                        // minutes and a long tool call is silent for as long as it runs.
                        timeout: 1_800
                    )

                    let (bytes, response) = try await session.bytes(for: urlRequest)
                    guard let httpResponse = response as? HTTPURLResponse else {
                        throw GatewayClientError.invalidResponse
                    }
                    guard (200...299).contains(httpResponse.statusCode) else {
                        var errorBody = Data()
                        errorBody.reserveCapacity(1_024)
                        for try await byte in bytes {
                            if errorBody.count >= 64 * 1_024 { break }
                            errorBody.append(byte)
                        }
                        throw Self.httpError(
                            statusCode: httpResponse.statusCode,
                            body: errorBody
                        )
                    }

                    let decoder = GatewayEventDecoder()
                    var parser = GatewaySSEParser(
                        maximumFrameBytes: limits.maximumFrameBytes,
                        maximumAggregateBytes: limits.maximumStreamBytes
                    )
                    var receivedEnd = false
                    byteLoop: for try await byte in bytes {
                        try Task.checkCancellation()
                        guard let frame = try parser.append(byte) else { continue }
                        try Self.yield(
                            decoder.decode(frame: frame),
                            to: continuation,
                            receivedEnd: &receivedEnd,
                            bufferLimit: limits.maximumBufferedEvents
                        )
                        if receivedEnd {
                            break byteLoop
                        }
                    }

                    if !receivedEnd, let finalFrame = try parser.finish() {
                        try Self.yield(
                            decoder.decode(frame: finalFrame),
                            to: continuation,
                            receivedEnd: &receivedEnd,
                            bufferLimit: limits.maximumBufferedEvents
                        )
                    }

                    guard receivedEnd else {
                        throw GatewayClientError.streamEndedBeforeCompletion
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: Self.mappedError(error))
                }
            }

            continuation.onTermination = { @Sendable _ in
                task.cancel()
            }
        }
    }
}

extension GatewayClient {
    private func chatsRequest(
        route: GatewayRoute,
        body: [String: String]
    ) async throws -> GatewayChatsSnapshot {
        let payload = try await send(
            route: route,
            body: try Self.encode(body),
            responseType: GatewayChatsPayload.self
        )
        if let error = payload.error, !error.isEmpty {
            throw GatewayEventDecoder.error(for: error)
        }
        // `current` can itself be an error object nested inside a 200 body.
        if let currentError = payload.current?.error, !currentError.isEmpty {
            throw GatewayEventDecoder.error(for: currentError)
        }
        return GatewayChatsSnapshot(
            chats: (payload.chats ?? []).map(\.value),
            current: payload.current?.value
        )
    }

    private func send<Response: Decodable>(
        route: GatewayRoute,
        body: Data? = nil,
        timeout: TimeInterval = 30,
        responseType: Response.Type
    ) async throws -> Response {
        let request = try requestFactory.makeRequest(
            route: route,
            body: body,
            accept: "application/json",
            timeout: timeout
        )

        let data: Data
        let response: URLResponse
        do {
            let (bytes, receivedResponse) = try await session.bytes(for: request)
            response = receivedResponse

            if receivedResponse.expectedContentLength > limits.maximumJSONResponseBytes {
                throw GatewayClientError.responseTooLarge(
                    endpoint: "/api/\(route.path)",
                    limitBytes: limits.maximumJSONResponseBytes
                )
            }

            var responseBody = Data()
            responseBody.reserveCapacity(
                min(
                    limits.maximumJSONResponseBytes,
                    max(1_024, Int(receivedResponse.expectedContentLength))
                )
            )
            for try await byte in bytes {
                guard responseBody.count < limits.maximumJSONResponseBytes else {
                    throw GatewayClientError.responseTooLarge(
                        endpoint: "/api/\(route.path)",
                        limitBytes: limits.maximumJSONResponseBytes
                    )
                }
                responseBody.append(byte)
            }
            data = responseBody
        } catch {
            throw Self.mappedError(error)
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw GatewayClientError.invalidResponse
        }
        guard (200...299).contains(httpResponse.statusCode) else {
            throw Self.httpError(statusCode: httpResponse.statusCode, body: data)
        }

        do {
            return try JSONDecoder().decode(responseType, from: data)
        } catch {
            throw GatewayClientError.malformedResponse(
                endpoint: "/api/\(route.path)",
                reason: error.localizedDescription
            )
        }
    }

    nonisolated static func encode(_ value: [String: String]) throws -> Data {
        do {
            return try JSONEncoder().encode(value)
        } catch {
            throw GatewayClientError.requestEncoding(reason: error.localizedDescription)
        }
    }

    nonisolated static func httpError(statusCode: Int, body: Data) -> GatewayClientError {
        let message: String?
        if let payload = try? JSONDecoder().decode(GatewayErrorPayload.self, from: body),
           let raw = payload.error?.trimmingCharacters(in: .whitespacesAndNewlines),
           !raw.isEmpty
        {
            message = String(raw.prefix(500))
        } else {
            let raw = String(decoding: body.prefix(500), as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            message = raw.isEmpty ? nil : raw
        }
        switch statusCode {
        case 401, 403:
            return .unauthorized
        case 409:
            // The gateway refuses a second turn before it has started streaming, precisely so the
            // client can restore the draft instead of showing the message as sent.
            return .busy(message: message ?? "")
        default:
            return .httpStatus(code: statusCode, message: message)
        }
    }

    nonisolated static func mappedError(_ error: any Error) -> any Error {
        if error is CancellationError {
            return CancellationError()
        }
        if let clientError = error as? GatewayClientError {
            return clientError
        }
        if let urlError = error as? URLError {
            if urlError.code == .cancelled {
                return CancellationError()
            }
            return GatewayClientError.transport(
                code: urlError.code,
                message: urlError.localizedDescription
            )
        }
        return GatewayClientError.transport(code: nil, message: error.localizedDescription)
    }

    nonisolated static func yield(
        _ events: [GatewayEvent],
        to continuation: GatewayEventStream.Continuation,
        receivedEnd: inout Bool,
        bufferLimit: Int
    ) throws {
        for event in events {
            if case .ended = event {
                receivedEnd = true
            }
            switch continuation.yield(event) {
            case .enqueued:
                break
            case .dropped:
                throw GatewayClientError.streamBufferOverflow(limit: bufferLimit)
            case .terminated:
                throw CancellationError()
            @unknown default:
                throw GatewayClientError.streamBufferOverflow(limit: bufferLimit)
            }
        }
    }
}

nonisolated struct GatewayRequestFactory: Sendable {
    let endpoint: GatewayEndpoint
    let token: String

    init(endpoint: GatewayEndpoint, token: String) {
        self.endpoint = endpoint
        self.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func makeRequest(
        route: GatewayRoute,
        body: Data? = nil,
        accept: String = "application/json",
        timeout: TimeInterval = 30
    ) throws -> URLRequest {
        // Every gateway route is token-gated, so a missing token is a configuration error rather
        // than an anonymous request worth attempting.
        guard !token.isEmpty else {
            throw GatewayClientError.missingToken
        }
        // The token grants shell, file, and browser control of the host machine. It may cross the
        // network unencrypted only inside a private network the user controls.
        guard endpoint.allowsToken else {
            throw GatewayClientError.insecureTokenTransport(
                host: endpoint.baseURL.host ?? endpoint.displayAddress
            )
        }

        var request = URLRequest(
            url: endpoint.url(for: route),
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.httpMethod = route.method
        request.httpBody = body
        request.setValue(accept, forHTTPHeaderField: "Accept")
        if body != nil {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        // The header form, never the `?token=` query fallback: a URL query would be written to
        // every proxy and server log in the path.
        request.setValue(token, forHTTPHeaderField: "X-Cagentic-Token")
        return request
    }
}

private nonisolated struct ModelResponse: Decodable {
    let model: String?
    let error: String?
}
