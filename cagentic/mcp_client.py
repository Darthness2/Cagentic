"""Model Context Protocol (MCP) client — stdio and Streamable HTTP.

Two transports, chosen by what the server config declares:

    command: [...]   launch a subprocess, JSON-RPC over its stdin/stdout
    url: "https://…"  Streamable HTTP — POST JSON-RPC, read back either a
                      JSON body or an SSE stream, carrying the session id
                      the server hands out at initialize

HTTP was the larger of the two gaps: hosted MCP servers do not ship a
command line to run, so a stdio-only client simply cannot reach them.
Both speak the same JSON-RPC above the transport, so everything from
`MCPServer._send_request` up is shared.

The protocol is documented at https://modelcontextprotocol.io — here we
implement the surface area a personal assistant needs:

    initialize          handshake
    tools/list          discover what the server exposes
    tools/call          run one
    resources/list      list URI-addressable resources
    resources/read      fetch one
    prompts/list        discover server-authored prompt templates
    prompts/get         render one

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
          },
          "hosted": {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer ..."},
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
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import __version__

logger = logging.getLogger(__name__)


# The revision we ask for. Servers answer `initialize` with the revision they
# actually speak, and that answer is what we use from then on — so asking for a
# newer one costs nothing against an older server. FALLBACK exists for the
# stricter servers that error on an unknown revision rather than negotiating.
PROTOCOL_VERSION = "2025-06-18"
FALLBACK_PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "cagentic", "version": __version__}


class MCPError(RuntimeError):
    pass


class _HttpTransport:
    """Streamable HTTP: one POST per JSON-RPC message.

    The server may answer a POST with either a JSON body (one response) or an
    SSE stream (a run of messages, of which one carries our id). Both are
    legal for the same endpoint and the server picks per request, so we read
    the Content-Type and branch rather than assuming.

    A session id, if the server issues one at initialize, comes back in the
    `Mcp-Session-Id` header and has to be echoed on everything afterwards —
    without it a stateful server treats each call as a new, uninitialised
    client and rejects it.
    """

    def __init__(self, name: str, url: str, headers: dict[str, str]) -> None:
        self.name = name
        self.url = url
        self.extra_headers = dict(headers or {})
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._session: Any = None

    def start(self) -> None:
        import requests

        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Both are advertised because the server chooses which to send.
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        headers.update(self.extra_headers)
        return headers

    def send(self, payload: dict, timeout: float, *, expect_reply: bool) -> dict | None:
        import requests

        if self._session is None:
            raise MCPError(f"server '{self.name}' is not connected")
        try:
            response = self._session.post(
                self.url,
                data=json.dumps(payload),
                headers=self._headers(),
                timeout=timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise MCPError(f"server '{self.name}' request failed: {exc}") from exc

        # Handed out at initialize; required on every later call.
        issued = response.headers.get("Mcp-Session-Id")
        if issued:
            self.session_id = issued

        if response.status_code == 404 and self.session_id:
            self.session_id = None
            raise MCPError(f"server '{self.name}' session expired — reconnect and retry")
        if response.status_code >= 400:
            detail = (response.text or "")[:300]
            raise MCPError(f"server '{self.name}' HTTP {response.status_code}: {detail}")
        if not expect_reply or response.status_code == 202:
            # A notification is accepted with no body.
            response.close()
            return None

        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        want_id = payload.get("id")
        if content_type == "text/event-stream":
            return self._read_sse(response, want_id)
        try:
            body = response.json()
        except ValueError as exc:
            raise MCPError(f"server '{self.name}' sent a non-JSON reply: {exc}") from exc
        finally:
            response.close()
        # A batch is legal; pick the message that answers us.
        if isinstance(body, list):
            for message in body:
                if isinstance(message, dict) and message.get("id") == want_id:
                    return message
            return None
        return body if isinstance(body, dict) else None

    def _read_sse(self, response: Any, want_id: Any) -> dict | None:
        """Pull `data:` frames off the stream until one answers our id.

        Anything else on the stream is a server-initiated message we have no
        handler for yet (logging, progress, sampling); skipping is the correct
        behaviour for a client that did not advertise those capabilities.
        """
        try:
            for raw in response.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                chunk = raw[len("data:") :].strip()
                if not chunk:
                    continue
                try:
                    message = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict) and message.get("id") == want_id:
                    return message
        except Exception as exc:
            raise MCPError(f"server '{self.name}' stream failed: {exc}") from exc
        finally:
            response.close()
        raise MCPError(f"server '{self.name}' stream ended without answering")

    def stop(self) -> None:
        session, self._session = self._session, None
        self.session_id = None
        if session is not None:
            try:
                session.close()
            except Exception:
                logger.warning("mcp '%s' session close failed", self.name, exc_info=True)

    def is_running(self) -> bool:
        return self._session is not None


@dataclass
class MCPServer:
    name: str
    # Exactly one of these decides the transport: a command to launch, or a URL
    # to POST to. A hosted server has no command line to run, which is why
    # stdio-only could not reach one at all.
    command: list[str] = field(default_factory=list)
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    proc: subprocess.Popen | None = None
    _http: "_HttpTransport | None" = None
    _protocol_version: str = PROTOCOL_VERSION
    _next_id: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _initialized: bool = False
    _tools_cache: list[dict] | None = None
    # What the server said it can do at initialize; empty for older ones.
    _capabilities: dict = field(default_factory=dict)
    # stdout lines pushed by a background reader thread; consumed (with a
    # timeout) by _send_request so a stalled server can't block forever.
    _stdout_q: "queue.Queue[str | None]" = field(default_factory=queue.Queue)
    _reader: threading.Thread | None = None
    _stderr_thread: threading.Thread | None = None
    # Tail of recent stderr lines, kept for diagnostics on exit.
    _stderr_tail: list[str] = field(default_factory=list)
    _stderr_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def start(self, timeout: float = 10.0) -> None:
        """Connect (launch or open a session) and run the initialize handshake."""
        if self.url:
            self._start_http(timeout)
            return
        if self.proc and self.proc.poll() is None:
            return
        env = {**os.environ, **self.env}
        # start_new_session puts the child in its own process group so we can
        # kill the whole tree on stop — npx spawns a `node` grandchild that
        # would otherwise leak. (No-op / harmless on platforms without it.)
        popen_kwargs: dict[str, Any] = {}
        if hasattr(os, "setsid"):
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":  # Windows: own process group for killing the tree
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # MCP servers are line-delimited JSON-RPC over stdio.
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,  # line-buffered
            **popen_kwargs,
        )
        # Drain stdout and stderr on daemon threads. stderr MUST be drained
        # continuously or a chatty server fills its ~64KB pipe and deadlocks.
        self._stdout_q = queue.Queue()
        self._stderr_tail = []
        self._reader = threading.Thread(
            target=self._read_stdout,
            args=(self.proc,),
            daemon=True,
            name=f"mcp-{self.name}-stdout",
        )
        self._reader.start()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self.proc,),
            daemon=True,
            name=f"mcp-{self.name}-stderr",
        )
        self._stderr_thread.start()
        try:
            self._handshake(timeout)
        except Exception as e:
            self.stop()
            raise MCPError(f"server '{self.name}' init failed: {e}") from e

    def _start_http(self, timeout: float) -> None:
        if self._http is not None and self._http.is_running():
            return
        self._http = _HttpTransport(self.name, self.url, self.headers)
        self._http.start()
        try:
            self._handshake(timeout)
        except Exception as exc:
            self.stop()
            raise MCPError(f"server '{self.name}' init failed: {exc}") from exc

    def _handshake(self, timeout: float) -> None:
        """initialize → initialized, agreeing a protocol revision on the way.

        A server that refuses the revision we asked for gets one more try at
        the older one. That is cheaper than pinning ourselves to 2024-11-05
        forever, which is what the client did before and what kept it from
        speaking to anything newer.
        """
        for version in (PROTOCOL_VERSION, FALLBACK_PROTOCOL_VERSION):
            try:
                result = (
                    self._send_request(
                        "initialize",
                        {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": CLIENT_INFO,
                        },
                        timeout=timeout,
                    )
                    or {}
                )
            except MCPError:
                if version == FALLBACK_PROTOCOL_VERSION:
                    raise
                continue
            # The server's answer wins: it names the revision it will speak.
            agreed = result.get("protocolVersion") or version
            self._protocol_version = str(agreed)
            if self._http is not None:
                self._http.protocol_version = self._protocol_version
            self._capabilities = result.get("capabilities") or {}
            self._send_notification("notifications/initialized", {})
            self._initialized = True
            return

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        """Feed every stdout line into the queue; sentinel None on EOF."""
        try:
            for line in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
                self._stdout_q.put(line)
        except Exception:
            logger.warning("mcp '%s' stdout reader crashed", self.name, exc_info=True)
        finally:
            self._stdout_q.put(None)  # signal EOF / process end

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Continuously drain stderr so the pipe buffer never fills and
        deadlocks the server. Keep a short tail for error reporting."""
        try:
            for line in iter(proc.stderr.readline, ""):  # type: ignore[union-attr]
                # append + trim isn't atomic across threads; guard so a
                # concurrent _stderr_snapshot can't join a half-truncated view.
                with self._stderr_lock:
                    self._stderr_tail.append(line)
                    del self._stderr_tail[:-50]  # cap the tail
        except Exception:
            logger.warning("mcp '%s' stderr drainer crashed", self.name, exc_info=True)

    def _stderr_snapshot(self, limit: int = 500) -> str:
        with self._stderr_lock:
            return ("".join(self._stderr_tail))[-limit:]

    def stop(self) -> None:
        if self._http is not None:
            self._http.stop()
            self._http = None
            self._initialized = False
            self._tools_cache = None
            return
        proc = self.proc
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            logger.warning("mcp '%s' stdin close failed", self.name, exc_info=True)
        try:
            self._terminate_tree(proc)
            proc.wait(timeout=3)
        except Exception:
            # Escalate to SIGKILL on the whole group, then reap the zombie.
            try:
                self._kill_tree(proc)
            except Exception:
                logger.warning("mcp '%s' kill failed", self.name, exc_info=True)
            try:
                proc.wait(timeout=3)
            except Exception:
                logger.warning("mcp '%s' wait-after-kill failed", self.name, exc_info=True)
        self.proc = None
        self._initialized = False
        self._tools_cache = None

    def _terminate_tree(self, proc: subprocess.Popen) -> None:
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                return
            except (ProcessLookupError, PermissionError, OSError):
                logger.warning(
                    "mcp '%s' killpg(SIGTERM) failed, falling back", self.name, exc_info=True
                )
        if os.name == "nt":
            # On Windows, proc.terminate()/proc.kill() call TerminateProcess
            # on the direct child only — the `node` grandchild that npx spawns
            # would be orphaned. taskkill /T walks and kills the whole tree.
            self._win_kill_tree(proc.pid, force=False)
            return
        proc.terminate()

    def _kill_tree(self, proc: subprocess.Popen) -> None:
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                logger.warning(
                    "mcp '%s' killpg(SIGKILL) failed, falling back", self.name, exc_info=True
                )
        if os.name == "nt":
            self._win_kill_tree(proc.pid, force=True)
            return
        proc.kill()

    @staticmethod
    def _win_kill_tree(pid: int, *, force: bool) -> None:
        """Kill a process and all its descendants on Windows via taskkill."""
        args = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            args.insert(1, "/F")
        try:
            subprocess.call(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            # taskkill missing or pid already gone — nothing useful to do.
            pass

    # ---- JSON-RPC plumbing ------------------------------------------------

    def _send_request(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        if self.url:
            return self._http_request(method, params, timeout)
        if not self.proc or self.proc.poll() is not None:
            raise MCPError(f"server '{self.name}' is not running")
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": method,
                    "params": params,
                }
            )
            # Guard the write: a server that died after the poll() check above
            # raises BrokenPipeError on write — turn it into a clean MCPError.
            try:
                self.proc.stdin.write(payload + "\n")  # type: ignore[union-attr]
                self.proc.stdin.flush()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError, ValueError) as e:
                raise MCPError(f"server '{self.name}' write failed for '{method}': {e}") from e
            # Read until we find a matching response, but bound every read by
            # the remaining deadline via the reader-thread queue — a server
            # that stalls mid-line can't hang us (or hold the lock) forever.
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(f"timeout waiting for '{method}' response")
                try:
                    line = self._stdout_q.get(timeout=remaining)
                except queue.Empty:
                    raise MCPError(f"timeout waiting for '{method}' response")
                if line is None:
                    # EOF sentinel — the process ended.
                    err = self._stderr_snapshot()
                    raise MCPError(f"server '{self.name}' exited (stderr: {err})")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == req_id:
                    if "error" in msg:
                        err = msg["error"]
                        raise MCPError(f"{method}: {err.get('message', err)}")
                    return msg.get("result")

    def _http_request(self, method: str, params: dict, timeout: float) -> Any:
        """Same JSON-RPC contract as the stdio path, over one POST."""
        if self._http is None:
            raise MCPError(f"server '{self.name}' is not connected")
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        message = self._http.send(payload, timeout, expect_reply=True)
        if not isinstance(message, dict):
            raise MCPError(f"{method}: no response from '{self.name}'")
        if "error" in message:
            err = message["error"]
            raise MCPError(f"{method}: {err.get('message', err) if isinstance(err, dict) else err}")
        return message.get("result")

    def _send_notification(self, method: str, params: dict) -> None:
        if self.url:
            if self._http is None:
                return
            try:
                self._http.send(
                    {"jsonrpc": "2.0", "method": method, "params": params},
                    timeout=10.0,
                    expect_reply=False,
                )
            except MCPError:
                logger.warning(
                    "mcp '%s' notification '%s' failed", self.name, method, exc_info=True
                )
            return
        if not self.proc or self.proc.poll() is not None:
            return
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        # Take the same lock as _send_request so a notification can't interleave
        # its bytes with an in-flight request and corrupt the JSON-RPC stream.
        with self._lock:
            try:
                self.proc.stdin.write(payload + "\n")  # type: ignore[union-attr]
                self.proc.stdin.flush()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError, ValueError):
                logger.warning(
                    "mcp '%s' notification '%s' write failed", self.name, method, exc_info=True
                )

    # ---- public surface ---------------------------------------------------

    def list_tools(self, *, force: bool = False) -> list[dict]:
        if self._tools_cache is not None and not force:
            return self._tools_cache
        res = self._send_request("tools/list", {}) or {}
        tools = res.get("tools") or []
        self._tools_cache = tools
        return tools

    def call_tool(self, name: str, arguments: dict, *, timeout: float = 60.0) -> dict:
        return (
            self._send_request(
                "tools/call",
                {
                    "name": name,
                    "arguments": arguments,
                },
                timeout=timeout,
            )
            or {}
        )

    def list_resources(self) -> list[dict]:
        res = self._send_request("resources/list", {}) or {}
        return res.get("resources") or []

    def read_resource(self, uri: str) -> dict:
        return self._send_request("resources/read", {"uri": uri}) or {}

    def list_prompts(self) -> list[dict]:
        """Server-authored prompt templates. Empty when the server has none.

        Servers that do not implement prompts answer with a method-not-found
        error rather than an empty list, which is not worth surfacing as a
        failure — the honest answer to "what prompts does this have" is none.
        """
        if not self._supports("prompts"):
            return []
        try:
            res = self._send_request("prompts/list", {}) or {}
        except MCPError:
            return []
        return res.get("prompts") or []

    def get_prompt(self, name: str, arguments: dict | None = None) -> dict:
        return self._send_request("prompts/get", {"name": name, "arguments": arguments or {}}) or {}

    def _supports(self, capability: str) -> bool:
        """Whether the server advertised a capability at initialize.

        Absent capabilities means an older server that did not report any, so
        the honest default is to try rather than to refuse.
        """
        caps = getattr(self, "_capabilities", None)
        if not isinstance(caps, dict) or not caps:
            return True
        return capability in caps


class MCPManager:
    """Holds one MCPServer per configured server. Lazy-starts on first use."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.servers: dict[str, MCPServer] = {}
        self._load_from_config()

    def _load_from_config(self) -> None:
        mcp = self.config.get("mcp")
        servers = mcp.get("servers") if isinstance(mcp, dict) else {}
        if not isinstance(servers, dict):
            return
        for name, spec in servers.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                continue
            if not spec.get("enabled", True):
                continue
            url = spec.get("url")
            if isinstance(url, str) and url.strip():
                # A hosted server is addressed, not launched. Only http(s) —
                # anything else here is a config mistake, and guessing at it
                # would mean handing an arbitrary scheme to the HTTP stack.
                if not url.strip().lower().startswith(("http://", "https://")):
                    logger.warning(
                        "mcp server '%s': url must be http:// or https://, got %r", name, url
                    )
                    continue
                raw_headers = spec.get("headers")
                self.servers[name] = MCPServer(
                    name=name,
                    url=url.strip(),
                    headers={
                        str(k): str(v)
                        for k, v in (raw_headers or {}).items()
                        if isinstance(raw_headers, dict)
                    },
                )
                continue
            command = spec.get("command")
            if not command:
                continue
            # `command` may be a list (preferred) or a shell-style string.
            # `list("npx -y …")` would split it into single characters and
            # then Popen tries to exec a program named "n" — so tokenize
            # strings with shlex instead. Use POSIX rules off Windows so
            # backslashes in paths aren't eaten as escapes.
            if isinstance(command, str):
                import shlex

                command = shlex.split(command, posix=os.name != "nt")
            elif isinstance(command, (list, tuple)):
                command = list(command)
            else:
                continue
            if not command:
                continue
            env = spec.get("env")
            self.servers[name] = MCPServer(
                name=name,
                command=command,
                env=dict(env) if isinstance(env, dict) else {},
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
            raise MCPError(f"no MCP server named '{name}' — configured: {self.names() or '(none)'}")
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

    def list_prompts(self, server: str) -> list[dict]:
        return self.get(server).list_prompts()

    def get_prompt(self, server: str, name: str, arguments: dict | None = None) -> dict:
        return self.get(server).get_prompt(name, arguments)

    def shutdown(self) -> None:
        for srv in self.servers.values():
            try:
                srv.stop()
            except Exception:
                logger.warning("mcp server '%s' stop failed", srv.name, exc_info=True)


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
            parts.append(
                f"[image: {item.get('mimeType', '?')}, {len(item.get('data', ''))} chars b64]"
            )
        elif t == "resource":
            res = item.get("resource") or {}
            parts.append(f"[resource: {res.get('uri', '?')}  {res.get('mimeType', '')}]")
            if "text" in res:
                parts.append(res["text"])
        else:
            parts.append(json.dumps(item)[:400])
    body = "\n".join(parts) if parts else "(empty result)"
    return ("ERROR: " + body) if is_err else body
