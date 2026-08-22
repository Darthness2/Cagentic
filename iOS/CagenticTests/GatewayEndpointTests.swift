import Testing
@testable import Cagentic

struct GatewayEndpointTests {
    @Test("A bare LAN address receives HTTP and the gateway's default port")
    func addsDefaultSchemeAndGatewayPort() throws {
        let endpoint = try GatewayEndpoint("192.168.1.42")

        #expect(endpoint.baseURL.scheme == "http")
        #expect(endpoint.baseURL.host == "192.168.1.42")
        #expect(endpoint.baseURL.port == 8_700)
        #expect(endpoint.displayAddress == "http://192.168.1.42:8700")
    }

    @Test("An explicit port is preserved, because the gateway probes for a free one")
    func preservesExplicitPort() throws {
        let endpoint = try GatewayEndpoint("studio-pc.local:8703")

        #expect(endpoint.baseURL.port == 8_703)
        #expect(endpoint.displayAddress == "http://studio-pc.local:8703")
    }

    @Test("A trailing API path and slash are removed")
    func stripsTrailingAPIPath() throws {
        #expect(try GatewayEndpoint("  192.168.1.42:8700/api/  ").displayAddress
            == "http://192.168.1.42:8700")
        #expect(try GatewayEndpoint("192.168.1.42:8700/").displayAddress
            == "http://192.168.1.42:8700")
    }

    @Test("Private, link-local, and mDNS addresses are accepted")
    func acceptsPrivateAddresses() throws {
        let addresses = [
            "10.0.0.8",
            "172.31.20.9",
            "192.168.50.20",
            "169.254.10.2",
            "workstation.local",
            "http://[fd12:3456:789a::2]",
            "http://[fe80::2]",
        ]
        for address in addresses {
            let endpoint = try GatewayEndpoint(address)
            #expect(endpoint.isPrivateLANHost, "\(address) should be treated as a LAN host")
        }
    }

    @Test("Addresses that point back at this device are rejected")
    func rejectsDeviceLocalAddresses() {
        for address in ["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]:8700"] {
            expectError(for: address) {
                if case .localDeviceAddress = $0 { return true }
                return false
            }
        }
    }

    @Test("A routable host is refused over plain HTTP")
    func rejectsCleartextPublicHost() {
        expectError(for: "gateway.example.com") {
            if case .nonLANAddress = $0 { return true }
            return false
        }
        expectError(for: "http://gateway.example.com:8700") {
            if case .nonLANAddress = $0 { return true }
            return false
        }
    }

    @Test("A routable host is accepted behind explicit HTTPS")
    func acceptsSecurePublicHost() throws {
        let endpoint = try GatewayEndpoint("https://gateway.example.com")

        #expect(endpoint.displayAddress == "https://gateway.example.com")
        // HTTPS keeps URL semantics rather than assuming the gateway's own port, because that shape
        // means a reverse proxy the user configured.
        #expect(endpoint.baseURL.port == nil)
        #expect(!endpoint.isPrivateLANHost)
        #expect(endpoint.allowsToken)
        #expect(!endpoint.sendsTokenInClear)
    }

    @Test("A token may travel in the clear only inside a private network")
    func cleartextTokenIsLimitedToPrivateNetworks() throws {
        let lan = try GatewayEndpoint("192.168.1.42:8700")
        #expect(lan.allowsToken)
        #expect(lan.sendsTokenInClear)

        let secureLAN = try GatewayEndpoint("https://studio-pc.local:8443")
        #expect(secureLAN.allowsToken)
        #expect(!secureLAN.sendsTokenInClear)
    }

    @Test("Credentials, queries, and fragments in the address are rejected")
    func rejectsUnsupportedURLComponents() {
        for address in [
            "http://user:secret@192.168.1.42:8700",
            "http://192.168.1.42:8700?token=abc",
            "http://192.168.1.42:8700#fragment",
        ] {
            expectError(for: address) {
                if case .invalidAddress = $0 { return true }
                return false
            }
        }
    }

    @Test("An unsupported base path is rejected")
    func rejectsUnsupportedBasePath() {
        expectError(for: "192.168.1.42:8700/gateway") {
            if case .unsupportedBasePath = $0 { return true }
            return false
        }
    }

    @Test("A malformed or out-of-range port is rejected")
    func rejectsInvalidPort() {
        expectError(for: "192.168.1.42:0") {
            if case .invalidPort = $0 { return true }
            return false
        }
        expectError(for: "http://192.168.1.42:") {
            if case .invalidPort = $0 { return true }
            return false
        }
    }

    @Test("An empty address is rejected")
    func rejectsEmptyAddress() {
        expectError(for: "   ") {
            if case .emptyAddress = $0 { return true }
            return false
        }
    }

    @Test("Routes are built under /api")
    func buildsRouteURLs() throws {
        let endpoint = try GatewayEndpoint("192.168.1.42:8700")

        #expect(endpoint.url(for: .bootstrap).absoluteString
            == "http://192.168.1.42:8700/api/bootstrap")
        #expect(endpoint.url(for: .chat).absoluteString == "http://192.168.1.42:8700/api/chat")
        #expect(endpoint.url(for: .chatsNew).absoluteString
            == "http://192.168.1.42:8700/api/chats/new")
    }

    private func expectError(
        for address: String,
        matching predicate: (GatewayClientError) -> Bool
    ) {
        do {
            let endpoint = try GatewayEndpoint(address)
            Issue.record("Expected \(address) to be rejected, got \(endpoint.displayAddress)")
        } catch let error as GatewayClientError {
            #expect(predicate(error), "Unexpected error for \(address): \(error)")
        } catch {
            Issue.record("Unexpected error type for \(address): \(error)")
        }
    }
}
