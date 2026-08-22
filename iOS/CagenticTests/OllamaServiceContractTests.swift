import Foundation
import Testing
@testable import Cagentic

struct OllamaServiceContractTests {
    @Test("HTTPS requests include the version route and an optional trimmed Bearer token")
    func requestFactoryBuildsVersionRouteAndBearerHeader() throws {
        let factory = OllamaRequestFactory(
            endpoint: try OllamaEndpoint("https://studio-pc.local/api"),
            bearerToken: "  secret-token  "
        )

        let request = try factory.makeRequest(route: .version, method: "GET")

        #expect(request.url?.absoluteString == "https://studio-pc.local/api/version")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret-token")
        #expect(request.value(forHTTPHeaderField: "Accept") == "application/json")
        #expect(request.value(forHTTPHeaderField: "Content-Type") == nil)
    }

    @Test("A Bearer token is rejected before an HTTP request can be created")
    func rejectsBearerTokenOverCleartextHTTP() async throws {
        let endpoint = try OllamaEndpoint("studio-pc.local")
        let factory = OllamaRequestFactory(endpoint: endpoint, bearerToken: "secret-token")

        do {
            _ = try factory.makeRequest(route: .version, method: "GET")
            Issue.record("Expected cleartext authentication to be rejected")
        } catch let error as OllamaClientError {
            #expect(error == .insecureBearerToken)
            #expect(error.recoverySuggestion?.contains("https://") == true)
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }

        let session = makeHardeningSession()
        defer { session.invalidateAndCancel() }
        let client = OllamaClient(
            endpoint: endpoint,
            bearerToken: "secret-token",
            session: session
        )
        do {
            _ = try await client.serverVersion()
            Issue.record("Expected the client to reject cleartext authentication")
        } catch let error as OllamaClientError {
            #expect(error == .insecureBearerToken)
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }
    }

    @Test("Ordinary JSON responses stop at the configured byte limit")
    func boundsOrdinaryJSONResponses() async throws {
        let session = makeHardeningSession()
        defer { session.invalidateAndCancel() }
        let client = OllamaClient(
            endpoint: try OllamaEndpoint("oversized-response.local"),
            session: session,
            limits: testLimits(maximumJSONResponseBytes: 32)
        )

        do {
            _ = try await client.serverVersion()
            Issue.record("Expected the oversized response to be rejected")
        } catch let error as OllamaClientError {
            #expect(error == .responseTooLarge(endpoint: "/api/version", limitBytes: 32))
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }
    }

    @Test("NDJSON parsing enforces line and aggregate byte limits")
    func boundsNDJSONLinesAndAggregateBytes() throws {
        var lineLimitedParser = OllamaNDJSONLineParser(
            maximumLineBytes: 3,
            maximumAggregateBytes: 32
        )
        #expect(try lineLimitedParser.append(ascii("a")) == nil)
        #expect(try lineLimitedParser.append(ascii("b")) == nil)
        #expect(try lineLimitedParser.append(ascii("c")) == nil)
        do {
            _ = try lineLimitedParser.append(ascii("d"))
            Issue.record("Expected the oversized NDJSON line to be rejected")
        } catch let error as OllamaClientError {
            #expect(error == .streamLineTooLarge(limitBytes: 3))
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }

        var exactLineParser = OllamaNDJSONLineParser(
            maximumLineBytes: 3,
            maximumAggregateBytes: 8
        )
        #expect(try exactLineParser.append(ascii("a")) == nil)
        #expect(try exactLineParser.append(ascii("b")) == nil)
        #expect(try exactLineParser.append(ascii("c")) == nil)
        #expect(try exactLineParser.append(0x0A) == "abc")

        var aggregateLimitedParser = OllamaNDJSONLineParser(
            maximumLineBytes: 16,
            maximumAggregateBytes: 3
        )
        #expect(try aggregateLimitedParser.append(ascii("a")) == nil)
        #expect(try aggregateLimitedParser.append(ascii("b")) == nil)
        #expect(try aggregateLimitedParser.append(ascii("c")) == nil)
        do {
            _ = try aggregateLimitedParser.append(ascii("d"))
            Issue.record("Expected the aggregate chat response to be rejected")
        } catch let error as OllamaClientError {
            #expect(error == .chatResponseTooLarge(limitBytes: 3))
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }

        var lineEndingParser = OllamaNDJSONLineParser(
            maximumLineBytes: 8,
            maximumAggregateBytes: 16
        )
        #expect(try lineEndingParser.append(ascii("a")) == nil)
        #expect(try lineEndingParser.append(ascii("b")) == nil)
        #expect(try lineEndingParser.append(0x0D) == nil)
        #expect(try lineEndingParser.append(0x0A) == "ab\r")
        #expect(try lineEndingParser.append(ascii("c")) == nil)
        #expect(try lineEndingParser.append(ascii("d")) == nil)
        #expect(try lineEndingParser.finish() == "cd")
    }

    @Test("A full chat event buffer fails instead of silently dropping a chunk")
    func detectsChatStreamBufferOverflow() async throws {
        let session = makeHardeningSession()
        defer { session.invalidateAndCancel() }
        let client = OllamaClient(
            endpoint: try OllamaEndpoint("buffer-overflow.local"),
            session: session,
            limits: testLimits(maximumBufferedChatEvents: 1)
        )
        let request = OllamaChatRequest(
            model: "model:latest",
            messages: [.init(role: .user, content: "Hello")]
        )

        do {
            for try await _ in client.chat(request) {}
            Issue.record("Expected the bounded stream to report a dropped event")
        } catch let error as OllamaClientError {
            #expect(error == .streamBufferOverflow(limit: 1))
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }
    }

    @Test("The model name encoded for chat is trimmed and cannot be blank")
    func canonicalizesChatModelNameBeforeEncoding() throws {
        let request = OllamaChatRequest(
            model: "  model:latest\n",
            messages: [.init(role: .user, content: "Hello")]
        )
        let body = try OllamaClient.chatRequestBody(for: request)
        let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])

        #expect(object["model"] as? String == "model:latest")

        do {
            _ = try OllamaClient.chatRequestBody(
                for: OllamaChatRequest(model: " \n ", messages: request.messages)
            )
            Issue.record("Expected a blank model name to be rejected")
        } catch let error as OllamaClientError {
            #expect(error == .emptyModelName)
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }
    }

    @Test("Chat request bodies enforce their final encoded byte limit")
    func chatRequestBodyLimitIncludesJSONEscaping() throws {
        let request = OllamaChatRequest(
            model: "model:latest",
            messages: [.init(role: .user, content: String(repeating: "\\\"\n", count: 40))]
        )
        let unrestrictedBody = try OllamaClient.chatRequestBody(for: request)

        do {
            _ = try OllamaClient.chatRequestBody(
                for: request,
                maximumBytes: unrestrictedBody.count - 1
            )
            Issue.record("Expected the final JSON byte limit to be enforced")
        } catch let error as OllamaClientError {
            #expect(error == .chatRequestTooLarge(limitBytes: unrestrictedBody.count - 1))
        }
    }

    @Test("Device-local errors use device-neutral copy")
    func localAddressErrorUsesDeviceNeutralCopy() throws {
        let error = OllamaClientError.localDeviceAddress("localhost")
        let description = try #require(error.errorDescription)
        let recovery = try #require(error.recoverySuggestion)

        #expect(description.contains("this device"))
        #expect(recovery.contains("this device"))
        #expect(!description.contains("iPhone"))
        #expect(!recovery.contains("iPhone"))
    }

    @Test("Transport recovery copy respects custom server ports")
    func transportRecoveryUsesConfiguredPortCopy() throws {
        let error = OllamaClientError.transport(code: .cannotConnectToHost, message: "Offline")
        let recovery = try #require(error.recoverySuggestion)

        #expect(recovery.contains("configured server port"))
        #expect(!recovery.contains("port 11434"))
    }

    @Test("The service protocol accepts preview and test doubles")
    func serviceProtocolAcceptsPreviewOrTestDouble() {
        let service: any OllamaServing = MockOllamaService()
        #expect(service is MockOllamaService)
    }

    @Test("Model pulls use the non-streaming Ollama pull contract")
    func modelPullUsesValidatedNonStreamingRequest() async throws {
        let session = makeHardeningSession()
        defer { session.invalidateAndCancel() }
        let client = OllamaClient(
            endpoint: try OllamaEndpoint("pull-model.local"),
            session: session
        )

        let response = try await client.pull(model: "  gemma3:4b ")

        #expect(response == OllamaPullResponse(status: "success"))
    }

    private func makeHardeningSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [HardeningURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func testLimits(
        maximumJSONResponseBytes: Int = 1_024,
        maximumChatResponseBytes: Int = 4_096,
        maximumNDJSONLineBytes: Int = 1_024,
        maximumBufferedChatEvents: Int = 8
    ) -> OllamaClientLimits {
        OllamaClientLimits(
            maximumJSONResponseBytes: maximumJSONResponseBytes,
            maximumChatResponseBytes: maximumChatResponseBytes,
            maximumNDJSONLineBytes: maximumNDJSONLineBytes,
            maximumBufferedChatEvents: maximumBufferedChatEvents
        )
    }

    private func ascii(_ character: Character) -> UInt8 {
        character.asciiValue ?? 0
    }
}

private nonisolated final class HardeningURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        let body: Data
        let statusCode: Int
        switch request.url?.host {
        case "oversized-response.local":
            statusCode = 200
            body = Data(
                (#"{"version":""# + String(repeating: "x", count: 128) + #""}"#).utf8
            )
        case "buffer-overflow.local":
            statusCode = 200
            body = Data(
                #"{"message":{"thinking":"a","content":"b"},"done":true}"#.utf8
            ) + Data([0x0A])
        case "pull-model.local":
            let object = requestBody().flatMap {
                try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
            }
            let isValid = request.url?.path == "/api/pull"
                && request.httpMethod == "POST"
                && object?["model"] as? String == "gemma3:4b"
                && object?["stream"] as? Bool == false
            statusCode = isValid ? 200 : 400
            body = Data(
                (isValid ? #"{"status":"success"}"# : #"{"error":"invalid pull request"}"#)
                    .utf8
            )
        default:
            statusCode = 200
            body = Data(#"{"version":"test"}"#.utf8)
        }

        guard let url = request.url,
              let response = HTTPURLResponse(
                  url: url,
                  statusCode: statusCode,
                  httpVersion: "HTTP/1.1",
                  headerFields: ["Content-Type": "application/json"]
              )
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private func requestBody() -> Data? {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1_024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

private nonisolated struct MockOllamaService: OllamaServing {
    func serverVersion() async throws -> OllamaServerVersion {
        OllamaServerVersion(version: "preview")
    }

    func models() async throws -> [OllamaModel] {
        [OllamaModel(name: "preview:latest")]
    }

    func show(model: String) async throws -> OllamaShowResponse {
        try JSONDecoder().decode(OllamaShowResponse.self, from: Data("{}".utf8))
    }

    func chat(_ request: OllamaChatRequest) -> OllamaChatEventStream {
        AsyncThrowingStream { continuation in
            continuation.yield(.content("Preview response"))
            continuation.yield(
                .completed(
                    OllamaChatCompletion(
                        model: request.model,
                        doneReason: "stop",
                        metrics: OllamaChatMetrics(generatedTokenCount: 2)
                    )
                )
            )
            continuation.finish()
        }
    }
}
