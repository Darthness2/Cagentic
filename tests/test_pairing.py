"""Pairing a device, and the mDNS advertisement that lets it find the computer."""

from __future__ import annotations

import struct

import pytest

from cagentic import discovery
from cagentic import gateway as gateway_module
from cagentic.agent import Agent
from cagentic.gateway import Gateway


class _NullClient:
    def chat(self, _model, _messages, **_kwargs):
        return {"content": ""}

    def chat_stream(self, _model, _messages, **_kwargs):
        yield ""

    def list_models(self):
        return []


def _gateway(tmp_path, monkeypatch, *, cfg=None) -> Gateway:
    monkeypatch.setattr(gateway_module, "_warm_model_cache", lambda _cfg: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    agent = Agent(_NullClient(), "test-model", tmp_path, stream=True)
    return Gateway(agent, cfg if cfg is not None else {}, port=18991)


# -- the pairing window ------------------------------------------------------


def test_a_correct_code_hands_over_the_token_exactly_once(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch)
    pairing = gw.begin_pairing()

    result, status = gw.redeem_pairing_code(pairing["code"])
    assert status == 200
    assert result["token"] == gw.token

    # The window is consumed: a code is worth one device, not a standing key.
    repeat, repeat_status = gw.redeem_pairing_code(pairing["code"])
    assert repeat_status == 409
    assert "token" not in repeat


def test_no_code_is_accepted_before_a_window_is_opened(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch)

    result, status = gw.redeem_pairing_code("000000")
    assert status == 409
    assert "token" not in result


def test_a_window_closes_after_five_wrong_guesses(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch)
    pairing = gw.begin_pairing()
    wrong = "000000" if pairing["code"] != "000000" else "111111"

    for _ in range(gw.PAIR_MAX_ATTEMPTS):
        result, status = gw.redeem_pairing_code(wrong)
        assert status == 403
        assert "token" not in result

    # Even the right code is refused now: guessing has to cost the window, or
    # six digits is a space worth walking.
    result, status = gw.redeem_pairing_code(pairing["code"])
    assert status in (403, 429)
    assert "token" not in result


def test_an_expired_window_stops_working(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch)
    pairing = gw.begin_pairing()

    monkeypatch.setattr(
        gateway_module.time,
        "monotonic",
        lambda: gw._pair_expires_at + 1,
    )
    result, status = gw.redeem_pairing_code(pairing["code"])
    assert status == 409
    assert "token" not in result


def test_cancelling_closes_the_window(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch)
    pairing = gw.begin_pairing()
    assert gw.pairing_status()["open"] is True

    gw.cancel_pairing()
    assert gw.pairing_status()["open"] is False
    _result, status = gw.redeem_pairing_code(pairing["code"])
    assert status == 409


def test_pairing_reports_an_address_a_phone_can_reach(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(discovery, "local_ipv4", lambda: "192.168.1.42")
    gw.port = 8700

    pairing = gw.begin_pairing()
    # Never the loopback address: that names the phone, not the computer.
    assert pairing["address"] == "http://192.168.1.42:8700"
    assert "localhost" not in pairing["address"]


def test_the_web_token_command_refuses_when_the_gateway_is_loopback_only(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch, cfg={"gateway": {"lan": False}})

    result = gw.handle_cmd("token")
    assert result["ok"] is False
    assert "gateway.lan" in result["text"]
    assert gw.pairing_status()["open"] is False


def test_the_web_token_command_opens_a_window_in_lan_mode(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch, cfg={"gateway": {"lan": True}})

    result = gw.handle_cmd("token")
    assert result["ok"] is True
    status = gw.pairing_status()
    assert status["open"] is True
    assert status["code"] in result["text"]

    assert gw.handle_cmd("token", "off")["ok"] is True
    assert gw.pairing_status()["open"] is False


# -- mDNS advertising --------------------------------------------------------


def test_names_round_trip_through_the_wire_format():
    encoded = discovery._encode_name("Studio._cagentic._tcp.local.")
    name, end = discovery._read_name(encoded, 0)

    assert name == "Studio._cagentic._tcp.local."
    assert end == len(encoded)


def test_a_compression_pointer_is_followed_without_looping():
    # "local." at offset 0, then a name whose second label is a pointer back.
    base = discovery._encode_name("local.")
    packet = base + b"\x03abc" + b"\xc0\x00"
    name, end = discovery._read_name(packet, len(base))

    assert name == "abc.local."
    assert end == len(packet)


def test_a_pointer_that_loops_is_rejected_rather_than_hanging():
    packet = b"\xc0\x00"  # points at itself
    with pytest.raises(ValueError):
        discovery._read_name(packet, 0)


def test_only_queries_for_our_service_get_an_answer():
    advertiser = discovery.ServiceAdvertiser(port=8700, instance="Studio")
    advertiser.address = "192.168.1.42"

    def query(name: str, qtype: int) -> bytes:
        header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
        return header + discovery._encode_name(name) + struct.pack("!HH", qtype, 1)

    assert advertiser._wants_us(query(discovery.SERVICE_TYPE, 12)) is True
    assert advertiser._wants_us(query("Studio._cagentic._tcp.local.", 33)) is True
    assert advertiser._wants_us(query("cagentic-Studio.local.", 1)) is True
    # Somebody else's service, and our own responses, must be ignored.
    assert advertiser._wants_us(query("_http._tcp.local.", 12)) is False
    response = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    assert advertiser._wants_us(response) is False


def test_the_machines_own_hostname_is_left_to_the_system_responder():
    """The bug this guards: a phone resolving to 127.0.0.1 and connecting to itself.

    macOS and Linux already publish `<machine>.local`, and that name answers with
    every address the machine has, loopback included. Pointing our SRV record at
    it handed clients a choice that included addresses that can never reach the
    gateway, so the advertised host has to be a name only we answer for.
    """
    advertiser = discovery.ServiceAdvertiser(port=8700, instance="Studio")
    advertiser.address = "192.168.1.42"

    assert advertiser.hostname == "cagentic-Studio.local."
    assert (
        advertiser._wants_us(
            struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
            + discovery._encode_name("Studio.local.")
            + struct.pack("!HH", 1, 1)
        )
        is False
    )

    # And the SRV record has to point at the name we claimed, not the machine's.
    srv = advertiser._response_records()[1]
    assert b"cagentic-Studio" in srv


def test_an_advertisement_carries_the_records_a_browser_needs():
    advertiser = discovery.ServiceAdvertiser(
        port=8700,
        instance="Studio",
        txt={"v": "1.2.3", "auth": "token"},
    )
    advertiser.address = "192.168.1.42"

    records = advertiser._response_records()
    assert len(records) == 4

    blob = b"".join(records)
    assert b"_cagentic" in blob
    assert b"v=1.2.3" in blob
    assert b"auth=token" in blob
    # The SRV record has to carry the real port or the phone cannot connect.
    assert struct.pack("!H", 8700) in blob
    assert b"\xc0\xa8\x01\x2a" in blob  # 192.168.1.42 packed into the A record


def test_retracting_the_advertisement_zeroes_the_lifetimes():
    advertiser = discovery.ServiceAdvertiser(port=8700, instance="Studio")
    advertiser.address = "192.168.1.42"

    for record in advertiser._response_records(goodbye=True):
        # TTL sits after the name, type, and class.
        ttl_offset = len(record) - 6 - _rdata_length(record)
        assert struct.unpack("!I", record[ttl_offset : ttl_offset + 4])[0] == 0


def _rdata_length(record: bytes) -> int:
    return struct.unpack("!H", record[-2 - _trailing_rdata(record) : -_trailing_rdata(record)])[0]


def _trailing_rdata(record: bytes) -> int:
    # Walk the name to find where the fixed-size fields start.
    _name, offset = discovery._read_name(record, 0)
    _rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", record[offset : offset + 10])
    return rdlength


def test_a_hostname_with_spaces_still_produces_a_legal_label():
    advertiser = discovery.ServiceAdvertiser(port=8700, instance="Mete's MacBook Pro")

    # The instance keeps its punctuation for display; the host label must not.
    assert advertiser.instance == "Mete's MacBook Pro"
    assert advertiser.hostname.endswith(".local.")
    label = advertiser.hostname.split(".")[0]
    assert all(char.isalnum() or char == "-" for char in label)
    assert len(label) <= 63


# -- visual panels -----------------------------------------------------------


def test_panels_are_on_by_default(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch)

    assert gw.panels_enabled is True
    assert "Visual workspace" in gw._composed_suffix()
    assert gw.get_settings()["panels"] is True


def test_turning_panels_off_takes_the_instructions_out_of_the_prompt(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch, cfg={"gateway": {"panels": False}})

    assert gw.panels_enabled is False
    # The model should stop producing panels rather than produce them for nobody.
    assert "Visual workspace" not in gw._composed_suffix()
    assert gw.get_settings()["panels"] is False


def test_the_panel_tool_is_withheld_when_panels_are_off(tmp_path, monkeypatch):
    gw = _gateway(tmp_path, monkeypatch, cfg={"gateway": {"panels": False}})
    gw._rebuild_suffix()

    names = {(s.get("function") or {}).get("name") for s in gw.engine._tool_schemas()}
    # Offering a tool whose output nothing renders wastes both a call and the
    # context its schema occupies.
    assert "show_widget" not in names

    gw.agent.state.update(panels_enabled=True)
    assert "show_widget" in {
        (s.get("function") or {}).get("name") for s in gw.engine._tool_schemas()
    }
