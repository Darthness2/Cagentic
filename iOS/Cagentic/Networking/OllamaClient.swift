import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public typealias OllamaChatEventStream = AsyncThrowingStream<OllamaChatEvent, Error>

nonisolated struct OllamaClientLimits: Equatable, Sendable {
    static let standard = OllamaClientLimits(
        maximumJSONResponseBytes: 16 * 1_024 * 1_024,
        maximumChatRequestBytes: 32 * 1_024 * 1_024,
        maximumChatResponseBytes: 64 * 1_024 * 1_024,
        maximumNDJSONLineBytes: 1 * 1_024 * 1_024,
        maximumBufferedChatEvents: 64
    )

    let maximumJSONResponseBytes: Int
    let maximumChatRequestBytes: Int
    let maximumChatResponseBytes: Int
    let maximumNDJSONLineBytes: Int
    let maximumBufferedChatEvents: Int

    init(
        maximumJSONResponseBytes: Int,
        maximumChatRequestBytes: Int = 32 * 1_024 * 1_024,
        maximumChatResponseBytes: Int,
        maximumNDJSONLineBytes: Int,
        maximumBufferedChatEvents: Int
    ) {
        precondition(maximumJSONResponseBytes > 0)
        precondition(maximumChatRequestBytes > 0)
        precondition(maximumChatResponseBytes > 0)
        precondition(maximumNDJSONLineBytes > 0)
        precondition(maximumBufferedChatEvents > 0)
        self.maximumJSONResponseBytes = maximumJSONResponseBytes
        self.maximumChatRequestBytes = maximumChatRequestBytes
        self.maximumChatResponseBytes = maximumChatResponseBytes
        self.maximumNDJSONLineBytes = maximumNDJSONLineBytes
        self.maximumBufferedChatEvents = maximumBufferedChatEvents
    }
}

/// Store-facing abstraction for live, preview, and test Ollama implementations.
public nonisolated protocol OllamaServing: Sendable {
    func serverVersion() async throws -> OllamaServerVersion
    func models() async throws -> [OllamaModel]
    func show(model: String) async throws -> OllamaShowResponse
    func pull(model: String) async throws -> OllamaPullResponse
    func chat(_ request: OllamaChatRequest) -> OllamaChatEventStream
}

public extension OllamaServing {
    func pull(model: String) async throws -> OllamaPullResponse {
        throw OllamaClientError.modelPullUnsupported
    }
}

/// Direct client for the native Ollama HTTP API running on a PC on the same LAN.
public nonisolated struct OllamaClient: OllamaServing, Sendable {
    private let session: URLSession
    private let requestFactory: OllamaRequestFactory
    private let limits: OllamaClientLimits

    public init(
        endpoint: OllamaEndpoint,
        bearerToken: String? = nil,
        session: URLSession = .shared
    ) {
        self.init(
            endpoint: endpoint,
            bearerToken: bearerToken,
            session: session,
            limits: .standard
        )
    }

    init(
        endpoint: OllamaEndpoint,
        bearerToken: String? = nil,
        session: URLSession,
        limits: OllamaClientLimits
    ) {
        self.session = session
        self.limits = limits
        requestFactory = OllamaRequestFactory(
            endpoint: endpoint,
            bearerToken: bearerToken
        )
    }

    public func serverVersion() async throws -> OllamaServerVersion {
        try await send(
            route: .version,
            method: "GET",
            responseType: OllamaServerVersion.self
        )
    }

    public func models() async throws -> [OllamaModel] {
        let response = try await send(
            route: .tags,
            method: "GET",
            responseType: OllamaTagsResponse.self
        )
        return response.models
    }

    public func show(model: String) async throws -> OllamaShowResponse {
        let normalizedModel = try Self.validatedModelName(model)
        let body = try Self.encode(ShowRequest(model: normalizedModel))
        return try await send(
            route: .show,
            method: "POST",
            body: body,
            responseType: OllamaShowResponse.self
        )
    }

    public func pull(model: String) async throws -> OllamaPullResponse {
        let normalizedModel = try Self.validatedModelName(model)
        let body = try Self.encode(PullRequest(model: normalizedModel, stream: false))
        return try await send(
            route: .pull,
            method: "POST",
            body: body,
            timeout: 1_800,
            responseType: OllamaPullResponse.self
        )
    }

    public func chat(_ request: OllamaChatRequest) -> OllamaChatEventStream {
        AsyncThrowingStream(
            bufferingPolicy: .bufferingOldest(limits.maximumBufferedChatEvents)
        ) { continuation in
            let task = Task { @concurrent in
                do {
                    let body = try Self.chatRequestBody(
                        for: request,
                        maximumBytes: limits.maximumChatRequestBytes
                    )
                    let urlRequest = try requestFactory.makeRequest(
                        route: .chat,
                        method: "POST",
                        body: body,
                        accept: "application/x-ndjson",
                        timeout: 1_800
                    )

                    let (bytes, response) = try await session.bytes(for: urlRequest)
                    guard let httpResponse = response as? HTTPURLResponse else {
                        throw OllamaClientError.invalidResponse
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

                    if httpResponse.expectedContentLength > limits.maximumChatResponseBytes {
                        throw OllamaClientError.chatResponseTooLarge(
                            limitBytes: limits.maximumChatResponseBytes
                        )
                    }

                    let decoder = OllamaChatChunkDecoder()
                    var lineParser = OllamaNDJSONLineParser(
                        maximumLineBytes: limits.maximumNDJSONLineBytes,
                        maximumAggregateBytes: limits.maximumChatResponseBytes
                    )
                    var receivedCompletion = false
                    byteLoop: for try await byte in bytes {
                        try Task.checkCancellation()
                        guard let line = try lineParser.append(byte) else { continue }
                        try Self.yieldChatEvents(
                            decoder.decode(line: line),
                            to: continuation,
                            receivedCompletion: &receivedCompletion,
                            bufferLimit: limits.maximumBufferedChatEvents
                        )
                        if receivedCompletion {
                            break byteLoop
                        }
                    }

                    if !receivedCompletion, let finalLine = try lineParser.finish() {
                        try Self.yieldChatEvents(
                            decoder.decode(line: finalLine),
                            to: continuation,
                            receivedCompletion: &receivedCompletion,
                            bufferLimit: limits.maximumBufferedChatEvents
                        )
                    }

                    guard receivedCompletion else {
                        throw OllamaClientError.streamEndedBeforeCompletion
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

extension OllamaClient {
    nonisolated private func send<Response: Decodable>(
        route: OllamaRoute,
        method: String,
        body: Data? = nil,
        timeout: TimeInterval = 30,
        responseType: Response.Type
    ) async throws -> Response {
        let request = try requestFactory.makeRequest(
            route: route,
            method: method,
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
                throw OllamaClientError.responseTooLarge(
                    endpoint: "/api/\(route.rawValue)",
                    limitBytes: limits.maximumJSONResponseBytes
                )
            }

            var responseBody = Data()
            responseBody.reserveCapacity(
                min(limits.maximumJSONResponseBytes, max(1_024, Int(receivedResponse.expectedContentLength)))
            )
            for try await byte in bytes {
                guard responseBody.count < limits.maximumJSONResponseBytes else {
                    throw OllamaClientError.responseTooLarge(
                        endpoint: "/api/\(route.rawValue)",
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
            throw OllamaClientError.invalidResponse
        }
        guard (200...299).contains(httpResponse.statusCode) else {
            throw Self.httpError(statusCode: httpResponse.statusCode, body: data)
        }

        do {
            return try JSONDecoder().decode(responseType, from: data)
        } catch {
            throw OllamaClientError.malformedResponse(
                endpoint: "/api/\(route.rawValue)",
                reason: error.localizedDescription
            )
        }
    }

    nonisolated static func chatRequestBody(
        for request: OllamaChatRequest,
        maximumBytes: Int = OllamaClientLimits.standard.maximumChatRequestBytes
    ) throws -> Data {
        let normalizedModel = try validatedModelName(request.model)
        let normalizedRequest = OllamaChatRequest(
            model: normalizedModel,
            messages: request.messages,
            options: request.options,
            keepAlive: request.keepAlive,
            think: request.think
        )
        let body = try encode(normalizedRequest)
        guard body.count <= maximumBytes else {
            throw OllamaClientError.chatRequestTooLarge(limitBytes: maximumBytes)
        }
        return body
    }

    nonisolated private static func validatedModelName(_ model: String) throws -> String {
        let normalized = model.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else {
            throw OllamaClientError.emptyModelName
        }
        return normalized
    }

    nonisolated private static func encode<Value: Encodable>(_ value: Value) throws -> Data {
        do {
            return try JSONEncoder().encode(value)
        } catch {
            throw OllamaClientError.requestEncoding(reason: error.localizedDescription)
        }
    }

    nonisolated private static func httpError(
        statusCode: Int,
        body: Data
    ) -> OllamaClientError {
        let message: String?
        if let envelope = try? JSONDecoder().decode(ErrorEnvelope.self, from: body),
           let serverMessage = envelope.error?.trimmingCharacters(in: .whitespacesAndNewlines),
           !serverMessage.isEmpty
        {
            message = serverMessage
        } else {
            let rawMessage = String(data: body, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if let rawMessage, !rawMessage.isEmpty {
                message = String(rawMessage.prefix(500))
            } else {
                message = nil
            }
        }
        return .httpStatus(code: statusCode, message: message)
    }

    nonisolated private static func mappedError(_ error: any Error) -> any Error {
        if error is CancellationError {
            return CancellationError()
        }
        if let clientError = error as? OllamaClientError {
            return clientError
        }
        if let urlError = error as? URLError {
            if urlError.code == .cancelled {
                return CancellationError()
            }
            return OllamaClientError.transport(
                code: urlError.code,
                message: urlError.localizedDescription
            )
        }
        return OllamaClientError.transport(code: nil, message: error.localizedDescription)
    }

    nonisolated private static func yieldChatEvents(
        _ events: [OllamaChatEvent],
        to continuation: OllamaChatEventStream.Continuation,
        receivedCompletion: inout Bool,
        bufferLimit: Int
    ) throws {
        for event in events {
            if case .completed = event {
                receivedCompletion = true
            }
            switch continuation.yield(event) {
            case .enqueued:
                break
            case .dropped:
                throw OllamaClientError.streamBufferOverflow(limit: bufferLimit)
            case .terminated:
                throw CancellationError()
            @unknown default:
                throw OllamaClientError.streamBufferOverflow(limit: bufferLimit)
            }
        }
    }
}

nonisolated struct OllamaRequestFactory: Sendable {
    let endpoint: OllamaEndpoint
    let bearerToken: String?

    init(endpoint: OllamaEndpoint, bearerToken: String?) {
        self.endpoint = endpoint
        let trimmedToken = bearerToken?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.bearerToken = trimmedToken?.isEmpty == false ? trimmedToken : nil
    }

    func makeRequest(
        route: OllamaRoute,
        method: String,
        body: Data? = nil,
        accept: String = "application/json",
        timeout: TimeInterval = 30
    ) throws -> URLRequest {
        if bearerToken != nil, endpoint.baseURL.scheme?.lowercased() != "https" {
            throw OllamaClientError.insecureBearerToken
        }

        var request = URLRequest(
            url: endpoint.url(for: route),
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.httpMethod = method
        request.httpBody = body
        request.setValue(accept, forHTTPHeaderField: "Accept")
        if body != nil {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let bearerToken {
            request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        }
        return request
    }
}

nonisolated struct OllamaNDJSONLineParser: Sendable {
    private let maximumLineBytes: Int
    private let maximumAggregateBytes: Int
    private var aggregateBytes = 0
    private var line = Data()

    init(maximumLineBytes: Int, maximumAggregateBytes: Int) {
        precondition(maximumLineBytes > 0)
        precondition(maximumAggregateBytes > 0)
        self.maximumLineBytes = maximumLineBytes
        self.maximumAggregateBytes = maximumAggregateBytes
        line.reserveCapacity(min(maximumLineBytes, 4_096))
    }

    mutating func append(_ byte: UInt8) throws -> String? {
        guard aggregateBytes < maximumAggregateBytes else {
            throw OllamaClientError.chatResponseTooLarge(limitBytes: maximumAggregateBytes)
        }
        aggregateBytes += 1

        if byte == 0x0A {
            return try takeLine()
        }
        guard line.count < maximumLineBytes else {
            throw OllamaClientError.streamLineTooLarge(limitBytes: maximumLineBytes)
        }
        line.append(byte)
        return nil
    }

    mutating func finish() throws -> String? {
        guard !line.isEmpty else { return nil }
        return try takeLine()
    }

    private mutating func takeLine() throws -> String {
        defer { line.removeAll(keepingCapacity: true) }
        guard let decodedLine = String(data: line, encoding: .utf8) else {
            throw OllamaClientError.malformedStreamChunk(
                excerpt: String(decoding: line.prefix(160), as: UTF8.self),
                reason: "The NDJSON record is not valid UTF-8."
            )
        }
        return decodedLine
    }
}

private nonisolated struct ShowRequest: Encodable {
    let model: String
}

private nonisolated struct PullRequest: Encodable {
    let model: String
    let stream: Bool
}

private nonisolated struct ErrorEnvelope: Decodable {
    let error: String?
}
