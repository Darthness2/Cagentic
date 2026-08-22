import Testing
@testable import Cagentic

struct OllamaEndpointTests {
    @Test("A bare LAN address receives HTTP and Ollama's default port")
    func addsDefaultSchemeAndOllamaPort() throws {
        let endpoint = try OllamaEndpoint("192.168.1.42")

        #expect(endpoint.baseURL.scheme == "http")
        #expect(endpoint.baseURL.host == "192.168.1.42")
        #expect(endpoint.baseURL.port == 11_434)
        #expect(endpoint.baseURL.path == "")
    }

    @Test("Explicit HTTPS and a custom port are preserved")
    func preservesHTTPSAndExplicitPort() throws {
        let endpoint = try OllamaEndpoint("https://studio-pc.local:8443")

        #expect(endpoint.baseURL.scheme == "https")
        #expect(endpoint.baseURL.host == "studio-pc.local")
        #expect(endpoint.baseURL.port == 8_443)
    }

    @Test("An explicit HTTPS reverse proxy uses the standard HTTPS port")
    func explicitHTTPSPublicReverseProxyUsesStandardHTTPSPort() throws {
        let endpoint = try OllamaEndpoint("https://ollama.example.com/api")

        #expect(endpoint.displayAddress == "https://ollama.example.com")
        #expect(endpoint.baseURL.host == "ollama.example.com")
        #expect(endpoint.baseURL.port == nil)
    }

    @Test("An explicit HTTPS LAN address also uses the standard HTTPS port")
    func explicitHTTPSLANAddressUsesStandardHTTPSPort() throws {
        let endpoint = try OllamaEndpoint("https://192.168.50.20")

        #expect(endpoint.displayAddress == "https://192.168.50.20")
        #expect(endpoint.baseURL.port == nil)
    }

    @Test("A trailing API path and slash are removed")
    func stripsTrailingAPIPathAndSlash() throws {
        let endpoint = try OllamaEndpoint("  studio-pc.local:11434/api/  ")

        #expect(endpoint.displayAddress == "http://studio-pc.local:11434")
    }

    @Test("Private IPv4, local IPv6, and mDNS addresses are accepted")
    func acceptsPrivateAndLinkLocalAddresses() throws {
        let addresses = [
            "10.0.0.8",
            "100.64.0.1",
            "100.127.255.254",
            "172.31.20.9",
            "192.168.50.20",
            "169.254.10.2",
            "workstation.local",
            "http://[fd12:3456:789a::2]",
            "http://[fe80::2]",
            "http://[fe80::2%25en0]",
            "http://[::ffff:192.168.50.20]",
        ]

        for address in addresses {
            _ = try OllamaEndpoint(address)
        }
    }

    @Test("Malformed mDNS hostnames and IPv6 literals are rejected")
    func rejectsMalformedLocalHostnamesAndIPv6() {
        let addresses = [
            "-.local",
            ".local",
            "studio..local",
            "studio_.local",
            "studio.-pc.local",
            "http://[fd00::1::2]",
            "http://[fe80::2%25]",
            "http://[fe80::2%25en0%25extra]",
            "http://[fd12:3456::2%25en0]",
            "https://999.999.999.999",
        ]

        for address in addresses {
            expectError(for: address) { error in
                switch error {
                case .invalidAddress, .nonLANAddress:
                    true
                default:
                    false
                }
            }
        }
    }

    @Test("RFC 6598 shared address space stops at the 100.64.0.0/10 boundary")
    func enforcesRFC6598UpperBoundary() {
        expectError(for: "100.128.0.1") { error in
            if case .nonLANAddress = error { true } else { false }
        }
    }

    @Test("Loopback and unspecified addresses are rejected")
    func rejectsAddressesThatPointBackToThePhone() {
        let addresses = [
            "localhost",
            "http://localhost:11434",
            "127.0.0.1",
            "127.10.20.30:11434",
            "0.0.0.0",
            "::",
            "[::]",
            "http://[::1]:11434",
            "http://[::ffff:127.0.0.1]",
        ]

        for address in addresses {
            expectError(for: address) { error in
                if case .localDeviceAddress = error { true } else { false }
            }
        }
    }

    @Test("Public hosts require explicit HTTPS")
    func rejectsBareOrHTTPPublicHostsAndUnqualifiedHostnames() {
        let addresses = [
            "example.com",
            "http://example.com",
            "desktop-pc",
            "8.8.8.8",
            "https://8.8.8.8",
        ]

        for address in addresses {
            expectError(for: address) { error in
                if case .nonLANAddress = error { true } else { false }
            }
        }
    }

    @Test("Only the root or a trailing API base path is accepted")
    func rejectsPathsOtherThanOptionalAPI() {
        expectError(for: "192.168.1.42:11434/api/tags") { error in
            if case .unsupportedBasePath("/api/tags") = error { true } else { false }
        }
    }

    private func expectError(
        for address: String,
        matching predicate: (OllamaClientError) -> Bool
    ) {
        do {
            _ = try OllamaEndpoint(address)
            Issue.record("Expected \(address) to be rejected")
        } catch let error as OllamaClientError {
            #expect(predicate(error))
        } catch {
            Issue.record("Expected OllamaClientError for \(address), got \(error)")
        }
    }
}
