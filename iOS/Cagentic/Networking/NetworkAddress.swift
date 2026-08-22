import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Host classification shared by every validated endpoint type.
///
/// These rules used to live as a file-private extension on `OllamaEndpoint`. The gateway endpoint
/// needs the identical LAN/loopback/public decisions — and the "may this credential travel over
/// cleartext?" question is answered from them — so re-implementing the CIDR logic in a second place
/// would be a security bug waiting to happen. The bodies are unchanged from that original extension.
nonisolated enum NetworkAddress {
    /// True when the host resolves back to the iPhone itself rather than to another machine.
    static func isDeviceLocal(_ host: String) -> Bool {
        let addressAndZone = ipv6AddressAndZone(from: host)
        if host.contains("%"), addressAndZone == nil {
            return false
        }
        let address = addressAndZone?.address ?? host
        if address == "localhost" || address.hasSuffix(".localhost") {
            return true
        }
        if let octets = ipv4Bytes(address) {
            return octets[0] == 127 || octets.allSatisfy { $0 == 0 }
        }
        guard let bytes = ipv6Bytes(address) else { return false }
        if bytes.allSatisfy({ $0 == 0 }) || bytes.dropLast().allSatisfy({ $0 == 0 }) && bytes[15] == 1 {
            return true
        }
        if let octets = mappedIPv4Bytes(in: bytes) {
            return octets[0] == 127 || octets.allSatisfy { $0 == 0 }
        }
        return false
    }

    /// True for private-range IPs, link-local/unique-local IPv6, and `.local` mDNS names.
    ///
    /// This is also the predicate that decides whether a secret may be sent over plain HTTP, so it
    /// must stay strictly narrower than "not obviously public".
    static func isLAN(_ host: String) -> Bool {
        if isValidLocalHostname(host) {
            return true
        }

        let addressAndZone = ipv6AddressAndZone(from: host)
        if host.contains("%"), addressAndZone == nil {
            return false
        }
        let address = addressAndZone?.address ?? host
        if let octets = ipv4Bytes(address) {
            return isPrivateIPv4(octets)
        }
        guard let bytes = ipv6Bytes(address) else { return false }
        if let octets = mappedIPv4Bytes(in: bytes) {
            return isPrivateIPv4(octets)
        }
        let isUniqueLocal = bytes[0] & 0xFE == 0xFC
        let isLinkLocal = bytes[0] == 0xFE && bytes[1] & 0xC0 == 0x80
        if addressAndZone?.zone != nil, !isLinkLocal {
            return false
        }
        return isUniqueLocal || isLinkLocal
    }

    static func ipv4Bytes(_ host: String) -> [UInt8]? {
        var address = in_addr()
        let parseResult = host.withCString { inet_pton(AF_INET, $0, &address) }
        guard parseResult == 1 else { return nil }
        return withUnsafeBytes(of: &address) { Array($0) }
    }

    static func isPrivateIPv4(_ octets: [UInt8]) -> Bool {
        octets[0] == 10
            || (octets[0] == 100 && (64...127).contains(octets[1]))
            || (octets[0] == 172 && (16...31).contains(octets[1]))
            || (octets[0] == 192 && octets[1] == 168)
            || (octets[0] == 169 && octets[1] == 254)
    }

    static func ipv6Bytes(_ host: String) -> [UInt8]? {
        var address = in6_addr()
        let parseResult = host.withCString { inet_pton(AF_INET6, $0, &address) }
        guard parseResult == 1 else { return nil }
        return withUnsafeBytes(of: &address) { Array($0) }
    }

    static func ipv6AddressAndZone(from host: String) -> (address: String, zone: String?)? {
        let pieces = host.split(separator: "%", omittingEmptySubsequences: false)
        guard pieces.count <= 2, let address = pieces.first, !address.isEmpty else { return nil }
        guard pieces.count == 2 else { return (String(address), nil) }

        let zone = pieces[1]
        guard !zone.isEmpty, zone.count <= 64, zone.allSatisfy(isValidZoneCharacter) else {
            return nil
        }
        return (String(address), String(zone))
    }

    static func isValidZoneCharacter(_ character: Character) -> Bool {
        character.isASCII
            && (character.isLetter
                || character.isNumber
                || character == "."
                || character == "_"
                || character == "~"
                || character == "-")
    }

    static func mappedIPv4Bytes(in ipv6Bytes: [UInt8]) -> [UInt8]? {
        guard ipv6Bytes.count == 16,
              ipv6Bytes.prefix(10).allSatisfy({ $0 == 0 }),
              ipv6Bytes[10] == 0xFF,
              ipv6Bytes[11] == 0xFF
        else {
            return nil
        }
        return Array(ipv6Bytes[12...15])
    }

    static func isValidLocalHostname(_ host: String) -> Bool {
        host.hasSuffix(".local") && isValidHostname(host)
    }

    static func isValidPublicHostname(_ host: String) -> Bool {
        guard host.count <= 253,
              host.contains("."),
              ipv4Bytes(host) == nil,
              !host.contains(":"),
              !host.split(separator: ".").allSatisfy({ label in
                  label.allSatisfy { $0.isASCII && $0.isNumber }
              })
        else {
            return false
        }

        return isValidHostname(host)
    }

    static func isValidHostname(_ host: String) -> Bool {
        guard host.count <= 253 else { return false }
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.count >= 2 else { return false }
        return labels.allSatisfy { label in
            guard !label.isEmpty,
                  label.count <= 63,
                  label.first != "-",
                  label.last != "-"
            else {
                return false
            }
            return label.allSatisfy {
                $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-")
            }
        }
    }

    /// Detects `host:` with no digits after the colon, which `URLComponents` silently accepts.
    static func containsInvalidExplicitPort(_ address: String, host: String) -> Bool {
        guard let authorityStart = address.range(of: "://")?.upperBound else { return false }
        let remainder = address[authorityStart...]
        let authority = remainder.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false)[0]

        if authority.hasPrefix("[") {
            guard let closingBracket = authority.firstIndex(of: "]") else { return true }
            let suffix = authority[authority.index(after: closingBracket)...]
            return suffix.hasPrefix(":") && suffix.dropFirst().isEmpty
        }

        let normalizedHost = host.lowercased()
        guard authority.lowercased().hasPrefix(normalizedHost) else { return false }
        let suffix = authority.dropFirst(normalizedHost.count)
        return suffix.hasPrefix(":") && suffix.dropFirst().isEmpty
    }
}
