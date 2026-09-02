"""Advertise the gateway on the local network over mDNS (Bonjour).

Hand-rolled for the same reason the MCP client and the HTTP gateway are: the
runtime dependency list is deliberately four packages, and pulling in `zeroconf`
to answer three record types is not a trade this project makes.

Only the responder half of mDNS is implemented — Cagentic advertises, it never
browses — and only for IPv4. That is enough for a phone to find this machine:
Apple's Network framework does the browsing side, and its own system responder
handles the querying, caching, and conflict detection this module would
otherwise have to.

The advertised service is `_cagentic._tcp.local.`, carrying TXT metadata so a
client can tell gateways apart before connecting.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading

_log = logging.getLogger("cagentic.discovery")

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
SERVICE_TYPE = "_cagentic._tcp.local."

# Record types we answer.
_TYPE_A = 1
_TYPE_PTR = 12
_TYPE_TXT = 16
_TYPE_SRV = 33
_TYPE_ANY = 255
_CLASS_IN = 1
# Set on unique records so browsers replace rather than accumulate them.
_CACHE_FLUSH = 0x8000

# A short TTL on the unique records keeps a stale entry from lingering after the
# gateway stops; the pointer record is allowed the conventional longer life.
_TTL_HOST = 120
_TTL_PTR = 4500


def local_ipv4() -> str | None:
    """Best guess at this machine's LAN address.

    Opens an unconnected UDP socket toward a routable address and reads back
    the interface the kernel picked. No packet is sent.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("198.51.100.1", 9))  # TEST-NET-3, never routed
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    if not address or address.startswith("127."):
        return None
    return address


def _sanitize_label(value: str, fallback: str) -> str:
    """Reduce a hostname to something legal in a DNS label."""
    cleaned = "".join(char if char.isalnum() or char == "-" else "-" for char in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:63] or fallback


def machine_name() -> str:
    """A human-facing name for this computer, shown in the phone's list."""
    # os.uname exists only on posix. Looking it up instead of calling it
    # directly keeps one spelling correct on both platforms: a bare os.uname()
    # is an attr-defined error when mypy targets Windows, and the `type: ignore`
    # that silences it is then reported as unused everywhere else.
    uname = getattr(os, "uname", None)
    preferred = (lambda: uname().nodename,) if uname is not None else ()
    for getter in (*preferred, socket.gethostname):
        try:
            raw = getter()
        except OSError:
            continue
        if raw:
            return str(raw).split(".")[0]
    return "Cagentic"


def _encode_name(name: str) -> bytes:
    """Encode a dotted DNS name as length-prefixed labels.

    Compression pointers are legal in responses but never required; writing
    names in full keeps this readable and costs a few dozen bytes.
    """
    out = bytearray()
    for label in name.rstrip(".").split("."):
        encoded = label.encode("utf-8")[:63]
        out.append(len(encoded))
        out += encoded
    out.append(0)
    return bytes(out)


def _encode_txt(pairs: dict[str, str]) -> bytes:
    """TXT rdata: a sequence of length-prefixed `key=value` strings."""
    out = bytearray()
    for key, value in pairs.items():
        entry = f"{key}={value}".encode("utf-8")[:255]
        out.append(len(entry))
        out += entry
    if not out:
        out.append(0)  # a TXT record must carry at least one (empty) string
    return bytes(out)


def _record(name: str, rtype: int, ttl: int, rdata: bytes, unique: bool) -> bytes:
    rclass = _CLASS_IN | (_CACHE_FLUSH if unique else 0)
    return _encode_name(name) + struct.pack("!HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a DNS name, following compression pointers.

    Returns the name and the offset just past the name *in the original
    position* — pointers do not advance the caller's cursor beyond the pointer.
    """
    labels: list[str] = []
    jumped = False
    end = offset
    steps = 0
    while True:
        if offset >= len(data):
            raise ValueError("name runs past end of packet")
        length = data[offset]
        if length & 0xC0 == 0xC0:  # compression pointer
            if offset + 1 >= len(data):
                raise ValueError("truncated compression pointer")
            if not jumped:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            jumped = True
            steps += 1
            if steps > 32:
                raise ValueError("compression pointer loop")
            continue
        offset += 1
        if length == 0:
            if not jumped:
                end = offset
            break
        labels.append(data[offset : offset + length].decode("utf-8", "replace"))
        offset += length
    return ".".join(labels) + ".", end


class ServiceAdvertiser:
    """Answers mDNS queries for one advertised gateway."""

    def __init__(
        self,
        port: int,
        instance: str | None = None,
        txt: dict[str, str] | None = None,
    ) -> None:
        self.port = int(port)
        raw_instance = instance or machine_name()
        # The instance label is what a person sees in the phone's list, so it
        # keeps its spaces; only the host label has to be DNS-clean.
        self.instance = raw_instance[:63] or "Cagentic"
        # The host label is deliberately *not* the machine's own name. macOS and
        # Linux already publish `<machine>.local` through their own responders,
        # and that name answers with every address the machine has — including
        # 127.0.0.1 and ::1. A phone resolving an SRV record that points there
        # picks one of those loopback addresses and connects to itself. Claiming
        # a name of our own, which no other responder answers for, keeps the
        # record pointing at exactly one usable address.
        self.host_label = "cagentic-" + _sanitize_label(raw_instance, "gateway")[:53]
        self.hostname = f"{self.host_label}.local."
        self.service_name = f"{self.instance}.{SERVICE_TYPE}"
        self.txt = txt or {}
        self.address = local_ipv4()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        if self._thread is not None:
            return True
        if self.address is None:
            _log.info("mDNS not started: no LAN address on this machine")
            return False
        try:
            sock = self._open_socket()
        except OSError:
            _log.warning("mDNS could not join the multicast group", exc_info=True)
            return False
        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._announce()
        return True

    def stop(self) -> None:
        self._stop.set()
        # A goodbye packet (TTL 0) retracts the advertisement immediately rather
        # than leaving browsers to wait out the TTL on a gateway that is gone.
        try:
            self._send(self._response_records(goodbye=True))
        except OSError:
            pass
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    # -- socket ------------------------------------------------------------

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Several responders share 5353 on a typical machine (Bonjour itself,
        # Avahi); without SO_REUSEPORT the bind loses to whoever got there first.
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", MDNS_PORT))
        membership = socket.inet_aton(MDNS_ADDR) + socket.inet_aton(self.address or "0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(1.0)
        return sock

    def _serve(self) -> None:
        while not self._stop.is_set():
            sock = self._socket
            if sock is None:
                return
            try:
                data, _sender = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    _log.debug("mDNS receive failed", exc_info=True)
                return
            try:
                if self._wants_us(data):
                    self._send(self._response_records())
            except Exception:
                # A malformed packet from anywhere on the network must never
                # take the responder down.
                _log.debug("ignored malformed mDNS packet", exc_info=True)

    def _send(self, answers: list[bytes]) -> None:
        sock = self._socket
        if sock is None or not answers:
            return
        header = struct.pack("!HHHHHH", 0, 0x8400, 0, len(answers), 0, 0)
        sock.sendto(header + b"".join(answers), (MDNS_ADDR, MDNS_PORT))

    def _announce(self) -> None:
        try:
            self._send(self._response_records())
        except OSError:
            _log.debug("mDNS announcement failed", exc_info=True)

    # -- packets -----------------------------------------------------------

    def _wants_us(self, data: bytes) -> bool:
        """True when a query asks for our service type, instance, or hostname."""
        if len(data) < 12:
            return False
        flags, qdcount = struct.unpack("!HH", data[2:6])
        if flags & 0x8000:  # a response, not a query
            return False
        offset = 12
        for _ in range(qdcount):
            name, offset = _read_name(data, offset)
            if offset + 4 > len(data):
                return False
            qtype, _qclass = struct.unpack("!HH", data[offset : offset + 4])
            offset += 4
            lowered = name.lower()
            if lowered == SERVICE_TYPE and qtype in (_TYPE_PTR, _TYPE_ANY):
                return True
            if lowered == self.service_name.lower() and qtype in (
                _TYPE_SRV,
                _TYPE_TXT,
                _TYPE_ANY,
            ):
                return True
            if lowered == self.hostname.lower() and qtype in (_TYPE_A, _TYPE_ANY):
                return True
        return False

    def _response_records(self, goodbye: bool = False) -> list[bytes]:
        if self.address is None:
            return []
        ptr_ttl = 0 if goodbye else _TTL_PTR
        host_ttl = 0 if goodbye else _TTL_HOST
        srv = struct.pack("!HHH", 0, 0, self.port) + _encode_name(self.hostname)
        return [
            _record(SERVICE_TYPE, _TYPE_PTR, ptr_ttl, _encode_name(self.service_name), False),
            _record(self.service_name, _TYPE_SRV, host_ttl, srv, True),
            _record(self.service_name, _TYPE_TXT, host_ttl, _encode_txt(self.txt), True),
            _record(self.hostname, _TYPE_A, host_ttl, socket.inet_aton(self.address), True),
        ]
