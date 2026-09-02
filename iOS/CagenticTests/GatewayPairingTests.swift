import Foundation
import Testing
@testable import Cagentic

/// The pairing exchange: the one gateway request that carries no token.
struct GatewayPairingTests {
    @Test("A correct code returns the token and the computer's name")
    func successfulPairing() async throws {
        let session = PairingStubURLProtocol.session { _ in
            (200, #"{"token":"long-secret-token","name":"Studio","version":"0.9.3"}"#)
        }
        let endpoint = try GatewayEndpoint("192.168.1.42:8700")

        let pairing = try await pairWithGateway(
            endpoint: endpoint,
            code: "123456",
            session: session
        )

        #expect(pairing.token == "long-secret-token")
        #expect(pairing.name == "Studio")
        #expect(pairing.version == "0.9.3")
    }

    @Test("The request carries only the code, and no token header")
    func sendsOnlyTheCode() async throws {
        let recorder = RequestRecorder()
        let session = PairingStubURLProtocol.session(recorder: recorder) { _ in
            (200, #"{"token":"t","name":"Studio","version":"1"}"#)
        }

        _ = try await pairWithGateway(
            endpoint: try GatewayEndpoint("192.168.1.42:8700"),
            code: "123456",
            session: session
        )

        let request = try #require(recorder.last)
        #expect(request.url?.path == "/api/pair")
        #expect(request.httpMethod == "POST")
        // Sending a token here would defeat the point: this call exists because
        // the device does not have one yet.
        #expect(request.value(forHTTPHeaderField: "X-Cagentic-Token") == nil)
        let body = try #require(recorder.lastBody)
        let decoded = try JSONSerialization.jsonObject(with: body) as? [String: String]
        #expect(decoded == ["code": "123456"])
    }

    @Test("Anything that is not a digit is stripped before sending")
    func normalisesTheCode() async throws {
        let recorder = RequestRecorder()
        let session = PairingStubURLProtocol.session(recorder: recorder) { _ in
            (200, #"{"token":"t","name":"S","version":"1"}"#)
        }

        _ = try await pairWithGateway(
            endpoint: try GatewayEndpoint("192.168.1.42:8700"),
            code: " 123 456 ",
            session: session
        )

        let body = try #require(recorder.lastBody)
        let decoded = try JSONSerialization.jsonObject(with: body) as? [String: String]
        #expect(decoded == ["code": "123456"])
    }

    @Test("An empty code never reaches the network")
    func refusesAnEmptyCode() async {
        let recorder = RequestRecorder()
        let session = PairingStubURLProtocol.session(recorder: recorder) { _ in (200, "{}") }

        await #expect(throws: GatewayClientError.self) {
            _ = try await pairWithGateway(
                endpoint: try GatewayEndpoint("192.168.1.42:8700"),
                code: "   ",
                session: session
            )
        }
        #expect(recorder.last == nil)
    }

    @Test("The gateway's own wording survives a refusal")
    func surfacesTheGatewayMessage() async throws {
        let session = PairingStubURLProtocol.session { _ in
            (403, #"{"error":"that code is not right (3 tries left)"}"#)
        }

        do {
            _ = try await pairWithGateway(
                endpoint: try GatewayEndpoint("192.168.1.42:8700"),
                code: "000000",
                session: session
            )
            Issue.record("Expected a wrong code to be refused")
        } catch let error as GatewayClientError {
            // "3 tries left" is worth more to the reader than "HTTP 403".
            #expect(error == .server(message: "that code is not right (3 tries left)"))
        }
    }

    @Test("A success that carries no token is treated as malformed")
    func rejectsATokenlessSuccess() async {
        let session = PairingStubURLProtocol.session { _ in (200, #"{"name":"Studio"}"#) }

        await #expect(throws: GatewayClientError.self) {
            _ = try await pairWithGateway(
                endpoint: try GatewayEndpoint("192.168.1.42:8700"),
                code: "123456",
                session: session
            )
        }
    }

    @Test("Pairing is refused for an address the token could leak over")
    func refusesUnsafeAddresses() {
        // The same rule as every other gateway credential: cleartext is allowed
        // only inside a private network.
        #expect(throws: GatewayClientError.self) {
            _ = try GatewayEndpoint("gateway.example.com")
        }
        #expect(throws: GatewayClientError.self) {
            _ = try GatewayEndpoint("localhost:8700")
        }
    }

    @Test("No open window is reported as its own case, not as a wrong code")
    func distinguishesNoWindowFromAWrongCode() async throws {
        // The difference matters: a wrong code is a wrong code, while "no window" may mean the
        // value was never a code — which is what lets a six-digit gateway token still work.
        let closed = PairingStubURLProtocol.session { _ in
            (409, #"{"error":"no pairing is in progress; run /token on the computer"}"#)
        }
        await #expect(throws: GatewayClientError.pairingUnavailable) {
            _ = try await pairWithGateway(
                endpoint: try GatewayEndpoint("192.168.1.42:8700"),
                code: "123456",
                session: closed
            )
        }

        let wrong = PairingStubURLProtocol.session { _ in
            (403, #"{"error":"that code is not right (2 tries left)"}"#)
        }
        await #expect(throws: GatewayClientError.server(message: "that code is not right (2 tries left)")) {
            _ = try await pairWithGateway(
                endpoint: try GatewayEndpoint("192.168.1.42:8700"),
                code: "000000",
                session: wrong
            )
        }
    }

    @Test("The pairing route is built under /api")
    func buildsThePairingURL() throws {
        let endpoint = try GatewayEndpoint("192.168.1.42:8700")

        #expect(endpoint.url(for: .pair).absoluteString == "http://192.168.1.42:8700/api/pair")
    }
}

// MARK: - Test doubles

private nonisolated final class RequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [(URLRequest, Data?)] = []

    func record(_ request: URLRequest, body: Data?) {
        lock.lock()
        defer { lock.unlock() }
        requests.append((request, body))
    }

    var last: URLRequest? {
        lock.lock()
        defer { lock.unlock() }
        return requests.last?.0
    }

    var lastBody: Data? {
        lock.lock()
        defer { lock.unlock() }
        return requests.last?.1
    }
}

/// Answers pairing requests without a network, so the exchange can be asserted
/// end to end through the real `URLSession` code path.
private nonisolated final class PairingStubURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var responder: (@Sendable (URLRequest) -> (Int, String))?
    nonisolated(unsafe) static var recorder: RequestRecorder?

    static func session(
        recorder: RequestRecorder? = nil,
        responder: @escaping @Sendable (URLRequest) -> (Int, String)
    ) -> URLSession {
        Self.responder = responder
        Self.recorder = recorder
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [PairingStubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        // URLSession moves an httpBody into a stream, so read it back that way.
        var body = request.httpBody
        if body == nil, let stream = request.httpBodyStream {
            stream.open()
            var data = Data()
            let size = 4_096
            let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: size)
            defer { buffer.deallocate() }
            while stream.hasBytesAvailable {
                let read = stream.read(buffer, maxLength: size)
                if read <= 0 { break }
                data.append(buffer, count: read)
            }
            stream.close()
            body = data
        }
        Self.recorder?.record(request, body: body)

        let (status, payload) = Self.responder?(request) ?? (500, "{}")
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(payload.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
