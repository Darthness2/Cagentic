"""Model Context Protocol (MCP) client — stdio JSON-RPC.

MCP servers are launched as subprocesses; we speak JSON-RPC 2.0 over their
stdin/stdout. The protocol is documented at https://modelcontextprotocol.io
— here we implement just the surface area a personal assistant needs:

    initialize          handshake
    tools/list          discover what the server exposes
    tools/call          run one
    resources/list      list URI-addressable resources
    resources/read      fetch one

Each server is configured in config.json under `mcp.servers.<name>`:

    {
      "mcp": {
        "servers": {
          "notion": {
            "command": ["npx", "-y", "@notionhq/notion-mcp-server"],
            "env": {"NOTION_TOKEN": "secret_xxx"},
            "enabled": true
          },
          "gdrive": {
            "command": ["npx", "-y", "@modelcontextprotocol/server-gdrive"],
            "env": {},
            "enabled": true
          }
        }
      }
    }

The MCPManager starts servers on first use, keeps a single connection
per server for the life of the Cagentic process, and shuts them down on
exit.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any


PROTOCOL_VERSION = "2024-11-05"   # mcp protocol revision string
CLIENT_INFO = {"name": "cagentic", "version": "0.1.0"}


class MCPError(RuntimeError):
    pass


@dataclass
class MCPServer:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    proc: subprocess.Popen | None = None
    _next_id: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _initialized: bool = False
    _tools_cache: list[dict] | None = None

    def start(self, timeout: float = 10.0) -> None:
        """Launch the server subprocess and run the MCP initialize handshake."""
        if self.proc and self.proc.poll() is None:
            return
        env = {**os.environ, **self.env}
        # MCP servers are line-delimited JSON-RPC over stdio.
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,                  # line-buffered
        )
        # Handshake: initialize → initialized notification.
        try:
            self._send_request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            }, timeout=timeout)
            self._send_notification("notifications/initialized", {})
            self._initialized = True
        except Exception as e:
            self.stop()
            raise MCPError(f"server '{self.name}' init failed: {e}") from e

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        self._initialized = False
        self._tools_cache = None

    # ---- JSON-RPC plumbing ------------------------------------------------

    def _send_request(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        if not self.proc or self.proc.poll() is not None:
            raise MCPError(f"server '{self.name}' is not running")
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            payload = json.dumps({
                "jsonrpc": "2.0", "id": req_id, "method": method, "params": params,
            })
            self.proc.stdin.write(payload + "\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()                # type: ignore[union-attr]
            # Read until we find a matching response. MCP servers emit
            # notifications too, which we drain and ignore for now.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = self.proc.stdout.readline()  # type: ignore[union-attr]
                if not line:
                    if self.proc.poll() is not None:
                        err = (self.proc.stderr.read() or "")[:500]  # type: ignore[union-attr]
                        raise MCPError(f"server '{self.name}' exited (stderr: {err})")
                    time.sleep(0.05)
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == req_id:
                    if "error" in msg:
                        err = msg["error"]
                        raise MCPError(f"{method}: {err.get('message', err)}")
                    return msg.get("result")
            raise MCPError(f"timeout waiting for '{method}' response")

    def _send_notification(self, method: str, params: dict) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        try:
            self.proc.stdin.write(payload + "\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()                # type: ignore[union-attr]
        except Exception:
            pass

    # ---- public surface ---------------------------------------------------

    def list_tools(self, *, force: bool = False) -> list[dict]:
        if self._tools_cache is not None and not force:
            return self._tools_cache
        res = self._send_request("tools/list", {}) or {}
        tools = res.get("tools") or []
        self._tools_cache = tools
        return tools

    def call_tool(self, name: str, arguments: dict, *, timeout: float = 60.0) -> dict:
        return self._send_request("tools/call", {
            "name": name, "arguments": arguments,
        }, timeout=timeout) or {}

    def list_resources(self) -> list[dict]:
        res = self._send_request("resources/list", {}) or {}
        return res.get("resources") or []

    def read_resource(self, uri: str) -> dict:
        return self._send_request("resources/read", {"uri": uri}) or {}


class MCPManager:
    """Holds one MCPServer per configured server. Lazy-starts on first use."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.servers: dict[str, MCPServer] = {}
        self._load_from_config()

    def _load_from_config(self) -> None:
        servers = ((self.config.get("mcp") or {}).get("servers") or {})
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            if not spec.get("enabled", True):
                continue
            command = spec.get("command")
            if not command:
                continue
            self.servers[name] = MCPServer(
                name=name,
                command=list(command),
                env=dict(spec.get("env") or {}),
            )

    def reload(self, config: dict) -> None:
        """Rebuild the server map from a new config. Stops any servers that
        were removed or changed."""
        self.shutdown()
        self.config = config
        self.servers = {}
        self._load_from_config()

    def names(self) -> list[str]:
        return sorted(self.servers.keys())

    def get(self, name: str, *, start: bool = True) -> MCPServer:
        srv = self.servers.get(name)
        if srv is None:
            raise MCPError(
                f"no MCP server named '{name}' — configured: {self.names() or '(none)'}"
            )
        if start:
            srv.start()
        return srv

    def list_tools(self, server: str) -> list[dict]:
        return self.get(server).list_tools()

    def call_tool(self, server: str, name: str, arguments: dict, timeout: float = 60.0) -> dict:
        return self.get(server).call_tool(name, arguments, timeout=timeout)

    def list_resources(self, server: str) -> list[dict]:
        return self.get(server).list_resources()

    def read_resource(self, server: str, uri: str) -> dict:
        return self.get(server).read_resource(uri)

    def shutdown(self) -> None:
        for srv in self.servers.values():
            try:
                srv.stop()
            except Exception:
                pass


def format_tool_result(result: dict) -> str:
    """Turn an MCP tool result envelope into a readable string.

    Tool results are {content: [{type, text|data, ...}], isError?: bool}.
    """
    if not isinstance(result, dict):
        return str(result)
    is_err = bool(result.get("isError"))
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text":
            parts.append(item.get("text", ""))
        elif t == "image":
            parts.append(f"[image: {item.get('mimeType', '?')}, {len(item.get('data', ''))} bytes b64]")
        elif t == "resource":
            res = item.get("resource") or {}
            parts.append(f"[resource: {res.get('uri', '?')}  {res.get('mimeType', '')}]")
            if "text" in res:
                parts.append(res["text"])
        else:
            parts.append(json.dumps(item)[:400])
    body = "\n".join(parts) if parts else "(empty result)"
    return ("ERROR: " + body) if is_err else body
