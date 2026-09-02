import Testing
@testable import Cagentic

/// Reaching a computer over Tailscale rather than the local Wi-Fi.
///
/// A tailnet is a WireGuard overlay, so a MagicDNS name is treated exactly like a LAN address —
/// including for the rule about sending a credential in the clear. Before that, these names were
/// refused as "not a private LAN address", which pushed people toward pasting the `https://` URL
/// Tailscale's own interface shows. That URL goes to port 443, where `tailscale serve` may be
/// proxying something else entirely, so the app ended up talking to the wrong program.
struct TailscaleAddressTests {
    @Test("A MagicDNS name is accepted and gets the gateway's default port")
    func acceptsMagicDNSForTheGateway() throws {
        let endpoint = try GatewayEndpoint("metes-macbook-pro.tailcdbf15.ts.net")

        #expect(endpoint.baseURL.scheme == "http")
        #expect(endpoint.baseURL.host == "metes-macbook-pro.tailcdbf15.ts.net")
        // Not 443: the gateway speaks plain HTTP on its own port.
        #expect(endpoint.baseURL.port == 8_700)
        #expect(endpoint.isPrivateLANHost)
        #expect(endpoint.allowsToken)
    }

    @Test("An explicit port on a MagicDNS name is kept")
    func keepsAnExplicitPort() throws {
        let endpoint = try GatewayEndpoint("metes-macbook-pro.tailcdbf15.ts.net:8701")

        #expect(endpoint.displayAddress == "http://metes-macbook-pro.tailcdbf15.ts.net:8701")
    }

    @Test("The pairing token may cross a tailnet in the clear")
    func allowsTheTokenOverATailnet() throws {
        let endpoint = try GatewayEndpoint("studio.tailnet-name.ts.net")

        // The traffic is already WireGuard-encrypted end to end, so this is no more exposed than
        // the same request across a home LAN.
        #expect(endpoint.allowsToken)
        #expect(endpoint.sendsTokenInClear)
    }

    @Test("Ollama is reachable over a MagicDNS name too")
    func acceptsMagicDNSForOllama() throws {
        let endpoint = try OllamaEndpoint("metes-macbook-pro.tailcdbf15.ts.net")

        #expect(endpoint.baseURL.port == 11_434)
        #expect(endpoint.isPrivateLANHost)
    }

    @Test("A tailnet IP was already reachable and stays so")
    func acceptsTailnetAddresses() throws {
        // Tailscale hands out CGNAT v4 and unique-local v6.
        #expect(try GatewayEndpoint("100.110.10.26:8700").isPrivateLANHost)
        #expect(try GatewayEndpoint("http://[fd7a:115c:a1e0::ba34:a1a]:8700").isPrivateLANHost)
    }

    @Test("Only real tailnet names qualify, not anything ending in ts.net")
    func doesNotAcceptBareTSNet() {
        // A MagicDNS name always carries a machine label and a tailnet label.
        #expect(NetworkAddress.isTailscaleMagicDNS("studio.tailnet-name.ts.net"))
        #expect(!NetworkAddress.isTailscaleMagicDNS("ts.net"))
        #expect(!NetworkAddress.isTailscaleMagicDNS("nots.net"))
        #expect(!NetworkAddress.isTailscaleMagicDNS("example.com"))
        // And an ordinary public host is still refused over cleartext.
        #expect(throws: GatewayClientError.self) {
            _ = try GatewayEndpoint("gateway.example.com")
        }
    }

    @Test("An explicit HTTPS tailnet address is still honoured")
    func honoursExplicitHTTPS() throws {
        // Somebody running `tailscale serve` in front of the gateway means it, so the scheme they
        // typed is not second-guessed.
        let endpoint = try GatewayEndpoint("https://metes-macbook-pro.tailcdbf15.ts.net")

        #expect(endpoint.isEncrypted)
        #expect(endpoint.baseURL.port == nil)
        #expect(!endpoint.sendsTokenInClear)
    }
}
