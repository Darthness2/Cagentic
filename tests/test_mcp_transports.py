"""MCP over stdio and Streamable HTTP.

The client spoke stdio only and pinned itself to protocol revision 2024-11-05,
which ruled out every hosted MCP server: a hosted server has no command line to
launch, so there was nothing for a subprocess transport to do.

These run real servers — a subprocess for stdio, a threaded HTTPServer for HTTP
— because the things worth testing are the wire details a mock would simply
assert back at itself: that the session id is echoed on later calls, that a
JSON body and an SSE stream are both accepted for the same request, and that a
server which refuses the newer revision gets offered the older one.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cagentic.mcp_client import (
    FALLBACK_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    MCPError,
    MCPManager,
    MCPServer,
    format_tool_result,
)

RESULTS = {
    "tools/list": {"tools": [{"name": "echo", "description": "echo it"}]},
    "tools/call": {"content": [{"type": "text", "text": "echoed!"}]},
    "resources/list": {"resources": [{"uri": "file://x", "name": "x"}]},
    "prompts/list": {"prompts": [{"name": "review", "description": "review code"}]},
    "prompts/get": {"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]},
}


class FakeServer:
    """A Streamable HTTP MCP server, configurable per test."""

    def __init__(self, *, sse: bool = False, oldest_only: bool = False, session: str = "sess-1"):
        self.sse = sse
        self.oldest_only = oldest_only
        self.session = session
        self.seen: list[dict] = []
        self.versions_asked: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _body(self, payload: str, content_type: str, extra: dict | None = None):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                if content_type != "text/event-stream":
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload.encode())
                self.wfile.flush()

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
                request = json.loads(raw or b"{}")
                outer.seen.append(dict(self.headers))
                req_id = request.get("id")
                if req_id is None:
                    self.send_response(202)
                    self.end_headers()
                    return

                method = request["method"]
                if method == "initialize":
                    asked = request["params"]["protocolVersion"]
                    outer.versions_asked.append(asked)
                    if outer.oldest_only and asked != FALLBACK_PROTOCOL_VERSION:
                        self._body(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": req_id,
                                    "error": {"code": -32602, "message": "unsupported version"},
                                }
                            ),
                            "application/json",
                        )
                        return
                    agreed = FALLBACK_PROTOCOL_VERSION if outer.oldest_only else asked
                    result = {
                        "protocolVersion": agreed,
                        "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
                    }
                    self._body(
                        json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}),
                        "application/json",
                        {"Mcp-Session-Id": outer.session} if outer.session else None,
                    )
                    return

                message = json.dumps(
                    {"jsonrpc": "2.0", "id": req_id, "result": RESULTS.get(method, {})}
                )
                if outer.sse:
                    # A server-initiated frame first: the client must skip past
                    # anything that is not the answer to its own id.
                    self._body(
                        'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                        f"data: {message}\n\n",
                        "text/event-stream",
                    )
                else:
                    self._body(message, "application/json")

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def http_server():
    made: list[FakeServer] = []

    def make(**kw) -> FakeServer:
        server = FakeServer(**kw)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


STDIO_SCRIPT = """
import json, sys
for line in sys.stdin:
    try:
        req = json.loads(line)
    except Exception:
        continue
    if req.get("id") is None:
        continue
    results = {
        "initialize": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}},
        "tools/list": {"tools": [{"name": "ping"}]},
        "tools/call": {"content": [{"type": "text", "text": "pong"}]},
    }
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": results.get(req["method"], {})})
        + "\\n"
    )
    sys.stdout.flush()
"""


@pytest.fixture()
def stdio_server(tmp_path):
    path = tmp_path / "server.py"
    path.write_text(STDIO_SCRIPT)
    server = MCPServer(name="local", command=[sys.executable, str(path)])
    yield server
    server.stop()


# ------------------------------------------------------------------ stdio --


def test_stdio_still_works(stdio_server):
    """The transport split must not have cost the one that already worked."""
    stdio_server.start()
    assert stdio_server.transport == "stdio"
    assert [t["name"] for t in stdio_server.list_tools()] == ["ping"]
    assert format_tool_result(stdio_server.call_tool("ping", {})) == "pong"


def test_stdio_negotiates_down_to_what_the_server_speaks(stdio_server):
    stdio_server.start()
    assert stdio_server._protocol_version == "2024-11-05"


# ------------------------------------------------------------------- http --


def test_http_completes_the_handshake(http_server):
    server = MCPServer(name="hosted", url=http_server().url)
    server.start()
    try:
        assert server.transport == "http"
        assert server._protocol_version == PROTOCOL_VERSION
    finally:
        server.stop()


def test_the_session_id_is_captured_and_echoed(http_server):
    """A stateful server treats a call without its session id as a new,
    uninitialised client and rejects it."""
    fake = http_server()
    server = MCPServer(name="hosted", url=fake.url)
    server.start()
    try:
        server.list_tools()
        assert fake.seen[-1].get("Mcp-Session-Id") == "sess-1"
        assert fake.seen[-1].get("MCP-Protocol-Version") == PROTOCOL_VERSION
    finally:
        server.stop()


@pytest.mark.parametrize("sse", [False, True], ids=["json-body", "sse-stream"])
def test_both_response_shapes_are_accepted(http_server, sse):
    """The server picks per request, so the client reads Content-Type rather
    than assuming one."""
    server = MCPServer(name="hosted", url=http_server(sse=sse).url)
    server.start()
    try:
        assert [t["name"] for t in server.list_tools()] == ["echo"]
        assert format_tool_result(server.call_tool("echo", {"m": "hi"})) == "echoed!"
        assert [r["uri"] for r in server.list_resources()] == ["file://x"]
    finally:
        server.stop()


def test_an_sse_stream_skips_server_initiated_messages(http_server):
    # The fake emits a notifications/progress frame before the real answer.
    server = MCPServer(name="hosted", url=http_server(sse=True).url)
    server.start()
    try:
        assert [t["name"] for t in server.list_tools()] == ["echo"]
    finally:
        server.stop()


def test_a_server_without_a_session_id_still_works(http_server):
    server = MCPServer(name="hosted", url=http_server(session="").url)
    server.start()
    try:
        assert server._http.session_id is None
        assert server.list_tools()
    finally:
        server.stop()


def test_an_old_server_gets_offered_the_old_revision(http_server):
    """Asking for a newer revision must not lock out servers that predate it."""
    fake = http_server(oldest_only=True)
    server = MCPServer(name="old", url=fake.url)
    server.start()
    try:
        assert fake.versions_asked == [PROTOCOL_VERSION, FALLBACK_PROTOCOL_VERSION]
        assert server._protocol_version == FALLBACK_PROTOCOL_VERSION
        assert server.list_tools()
    finally:
        server.stop()


def test_an_unreachable_server_fails_cleanly():
    server = MCPServer(name="dead", url="http://127.0.0.1:9/mcp")
    with pytest.raises(MCPError):
        server.start()


def test_stopping_releases_the_session(http_server):
    server = MCPServer(name="hosted", url=http_server().url)
    server.start()
    assert server._http is not None
    server.stop()
    assert server._http is None


# ---------------------------------------------------------------- prompts --


def test_prompts_are_reachable(http_server):
    server = MCPServer(name="hosted", url=http_server().url)
    server.start()
    try:
        assert [p["name"] for p in server.list_prompts()] == ["review"]
        assert server.get_prompt("review")["messages"]
    finally:
        server.stop()


def test_a_server_without_prompts_reports_none_rather_than_failing(stdio_server):
    """Its capabilities name tools only, so "what prompts does this have" has
    an honest answer, and it is not an error."""
    stdio_server.start()
    assert stdio_server.list_prompts() == []


# ----------------------------------------------------------------- config --


def test_a_url_server_is_configured_without_a_command():
    manager = MCPManager({"mcp": {"servers": {"hosted": {"url": "https://example.com/mcp"}}}})
    assert manager.servers["hosted"].transport == "http"


def test_headers_are_carried_through():
    manager = MCPManager(
        {
            "mcp": {
                "servers": {
                    "hosted": {
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer x"},
                    }
                }
            }
        }
    )
    assert manager.servers["hosted"].headers["Authorization"] == "Bearer x"


@pytest.mark.parametrize("url", ["ftp://nope", "file:///etc/passwd", "notaurl"])
def test_a_non_http_url_is_refused(url):
    """Handing an arbitrary scheme to the HTTP stack is not a guess worth
    making — it is a config mistake, and a silent one is worse."""
    manager = MCPManager({"mcp": {"servers": {"bad": {"url": url}}}})
    assert "bad" not in manager.servers


def test_stdio_and_http_servers_coexist():
    manager = MCPManager(
        {
            "mcp": {
                "servers": {
                    "local": {"command": ["some-server"]},
                    "hosted": {"url": "https://example.com/mcp"},
                }
            }
        }
    )
    assert manager.servers["local"].transport == "stdio"
    assert manager.servers["hosted"].transport == "http"


def test_a_disabled_server_is_skipped():
    manager = MCPManager(
        {"mcp": {"servers": {"off": {"url": "https://example.com/mcp", "enabled": False}}}}
    )
    assert manager.servers == {}
