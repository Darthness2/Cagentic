"""/gateway — a local web UI for Cagentic.

Starts an HTTP server (default port 8700) that serves a chat page and runs
the full agent behind it: the same tools, notes, reminders, MCP servers,
browser control — everything the terminal REPL can do, in a browser tab.

The web turn streams back token-by-token. Tools that need approval surface
an Approve / Deny prompt right in the page, so "do everything" stays safe
without a terminal in the loop. Bound to localhost only.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _ClientGone(Exception):
    """Raised when the browser hangs up mid-stream."""


class Gateway:
    def __init__(self, agent, config: dict, port: int = 8700) -> None:
        self.agent = agent
        self.config = config
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None

        # One turn at a time — the engine isn't concurrent-safe.
        self._turn_lock = threading.Lock()
        self._active_emit = None                     # SSE writer of the live turn

        # Web-driven permission prompt.
        self._perm_cv = threading.Condition()
        self._perm_answer: str | None = None

        # The gateway's own engine: a separate conversation, but the SAME
        # shared state (notes, reminders, browser bridge, MCP, workspace).
        from .engine import QueryEngine
        self.engine = QueryEngine(
            client=agent.client,
            state=agent.state,
            model=agent.model,
            temperature=agent.engine.temperature,
            config=config,
            permission_resolver=self._resolve,
            stream=True,
        )

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if self._server is not None:
            return True
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as e:
            self.error = f"could not bind 127.0.0.1:{self.port} ({e})"
            return False
        server.gateway = self           # type: ignore[attr-defined]
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def url(self) -> str:
        return f"http://localhost:{self.port}"

    # -- permission bridge --------------------------------------------------

    def _resolve(self, name: str, args: dict, state) -> str:
        """Permission resolver — surfaces an Approve/Deny prompt in the page."""
        emit = self._active_emit
        if emit is None:
            return "no"
        from .engine import _summarize_args
        with self._perm_cv:
            self._perm_answer = None
        emit("permission", {"tool": name, "summary": _summarize_args(name, args)})
        with self._perm_cv:
            deadline = time.monotonic() + 300        # 5 min to decide
            while self._perm_answer is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "no"
                self._perm_cv.wait(remaining)
            return self._perm_answer

    def deliver_permission(self, answer: str) -> None:
        with self._perm_cv:
            self._perm_answer = answer if answer in ("yes", "no", "always", "never") else "no"
            self._perm_cv.notify_all()

    # -- a chat turn --------------------------------------------------------

    def run_turn(self, message: str, emit) -> None:
        """Drive one turn, calling emit(kind, data) for every engine event."""
        if not self._turn_lock.acquire(blocking=False):
            emit("error", {"text": "Cagentic is still working on the previous message."})
            return
        self._active_emit = emit
        # Track the model the REPL may have switched to.
        self.engine.model = self.agent.model
        try:
            for ev in self.engine.submit_message(message):
                emit(ev.kind, ev.data)
        except _ClientGone:
            raise
        except Exception as e:
            emit("error", {"text": f"{type(e).__name__}: {e}"})
        finally:
            self._active_emit = None
            self._turn_lock.release()


# ---------------------------------------------------------------- handler ---

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silence
        pass

    def _gw(self) -> Gateway:
        return self.server.gateway  # type: ignore[attr-defined]

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/":
            self._send(_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/info":
            gw = self._gw()
            info = {
                "model": gw.agent.model,
                "user_name": gw.agent.state.user_name,
                "workspace": str(gw.agent.state.workspace),
            }
            self._send(json.dumps(info).encode("utf-8"), "application/json")
        else:
            self._send(b"not found", "text/plain", status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(b'{"error":"bad json"}', "application/json", status=400)
            return

        if path == "/permission":
            self._gw().deliver_permission(str(data.get("answer", "no")))
            self._send(b'{"ok":true}', "application/json")
            return

        if path == "/chat":
            self._stream_chat(str(data.get("message", "")).strip())
            return

        self._send(b"not found", "text/plain", status=404)

    def _stream_chat(self, message: str) -> None:
        # Stream the turn as Server-Sent Events; close the connection at end.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(kind: str, data: dict) -> None:
            payload = json.dumps({"kind": kind, "data": data})
            try:
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise _ClientGone()

        if not message:
            emit("error", {"text": "empty message"})
            return
        try:
            self._gw().run_turn(message, emit)
        except _ClientGone:
            return
        try:
            emit("end", {})
        except _ClientGone:
            pass


# ---------------------------------------------------------------- the page --

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Cagentic</title>
<style>
  :root {
    --bg: #17121d; --panel: #221a2b; --panel2: #2b2136;
    --text: #ece4f2; --muted: #a795b3;
    --peach: #ffd0a8; --copper: #d39a6a; --plum: #b98fc9; --gold: #e8c07a;
    --ok: #8fce8f; --err: #e09090;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.6 -apple-system, "Segoe UI", system-ui, sans-serif;
    height: 100vh; display: flex; flex-direction: column;
  }
  header {
    padding: 12px 20px; border-bottom: 1px solid #342941;
    display: flex; align-items: baseline; gap: 12px;
  }
  header .mark { color: var(--gold); font-size: 18px; }
  header .name { color: var(--peach); font-weight: 600; letter-spacing: .12em; }
  header .meta { color: var(--muted); font-size: 12px; }
  #log { flex: 1; overflow-y: auto; padding: 20px; }
  .msg { max-width: 760px; margin: 0 auto 16px; }
  .who { font-size: 11px; letter-spacing: .08em; text-transform: uppercase;
         color: var(--muted); margin-bottom: 4px; }
  .you .bubble { background: var(--panel2); border-left: 2px solid var(--copper); }
  .bot .bubble { background: var(--panel); border-left: 2px solid var(--plum); }
  .bubble { padding: 10px 14px; border-radius: 8px; white-space: pre-wrap;
            word-wrap: break-word; }
  .tool { max-width: 760px; margin: 0 auto 8px; font-size: 13px;
          color: var(--muted); }
  .tool .nm { color: var(--plum); }
  .tool.ok .res { color: var(--ok); }
  .tool.bad .res { color: var(--err); }
  .note { max-width: 760px; margin: 0 auto 8px; font-size: 12px;
          color: var(--muted); font-style: italic; }
  .plan { max-width: 760px; margin: 0 auto 12px; padding: 10px 14px;
          background: var(--panel); border-radius: 8px; border-left: 2px solid var(--gold); }
  .plan b { color: var(--gold); }
  .perm { max-width: 760px; margin: 0 auto 12px; padding: 12px 14px;
          background: #312338; border: 1px solid var(--copper); border-radius: 8px; }
  .perm .q { margin-bottom: 8px; }
  .perm button { border: 0; border-radius: 6px; padding: 6px 12px; margin-right: 6px;
                 font: 600 13px inherit; cursor: pointer; }
  .perm .yes { background: var(--ok); color: #14201a; }
  .perm .always { background: var(--gold); color: #241c10; }
  .perm .no { background: var(--err); color: #2a1414; }
  footer { padding: 14px 20px; border-top: 1px solid #342941; }
  form { max-width: 760px; margin: 0 auto; display: flex; gap: 8px; }
  textarea {
    flex: 1; resize: none; background: var(--panel); color: var(--text);
    border: 1px solid #3a2e47; border-radius: 8px; padding: 10px 12px;
    font: inherit; min-height: 44px; max-height: 160px;
  }
  button.send {
    border: 0; border-radius: 8px; padding: 0 18px; cursor: pointer;
    background: var(--copper); color: #1c1622; font: 600 14px inherit;
  }
  button.send:disabled { opacity: .5; cursor: default; }
  code { background: #2b2136; padding: 1px 5px; border-radius: 4px; color: var(--gold); }
</style>
</head>
<body>
  <header>
    <span class="mark">&#10022;</span>
    <span class="name">CAGENTIC</span>
    <span class="meta" id="meta">connecting…</span>
  </header>
  <div id="log"></div>
  <footer>
    <form id="form">
      <textarea id="input" placeholder="Ask Cagentic anything…  (Enter to send)" autofocus></textarea>
      <button class="send" id="send" type="submit">Send</button>
    </form>
  </footer>
<script>
const log = document.getElementById('log');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const form = document.getElementById('form');

fetch('/info').then(r => r.json()).then(i => {
  document.getElementById('meta').textContent =
    i.model + (i.user_name ? '  ·  for ' + i.user_name : '');
}).catch(() => {});

function el(cls, html) {
  const d = document.createElement('div');
  d.className = cls;
  if (html !== undefined) d.innerHTML = html;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}
function esc(s) {
  return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
function addMsg(who, label) {
  const wrap = el('msg ' + who);
  wrap.innerHTML = '<div class="who">' + label + '</div>';
  const b = document.createElement('div');
  b.className = 'bubble';
  wrap.appendChild(b);
  log.scrollTop = log.scrollHeight;
  return b;
}

let botBubble = null;

function handle(ev) {
  const k = ev.kind, d = ev.data || {};
  if (k === 'delta') {
    if (!botBubble) botBubble = addMsg('bot', 'Cagentic');
    botBubble.textContent += d.text || '';
  } else if (k === 'assistant') {
    if (!botBubble && (d.text || '').trim()) {
      botBubble = addMsg('bot', 'Cagentic');
      botBubble.textContent = d.text;
    }
  } else if (k === 'plan') {
    const p = el('plan');
    p.innerHTML = '<b>&#10047; plan</b><br>' +
      (d.steps || []).map((s, i) => (i + 1) + '. ' + esc(s)).join('<br>');
  } else if (k === 'tool_call') {
    el('tool', '<span class="nm">&#8627; ' + esc(d.name) + '</span> ' +
       esc(d.summary || '')).dataset.pending = '1';
  } else if (k === 'tool_result') {
    const rows = log.querySelectorAll('.tool');
    const last = rows[rows.length - 1];
    if (last) {
      last.className = 'tool ' + (d.ok ? 'ok' : 'bad');
      last.innerHTML += ' <span class="res">' + (d.ok ? '&#10003; ' : '&#10007; ') +
        esc((d.first_line || '').slice(0, 140)) + '</span>';
    }
  } else if (k === 'permission') {
    showPermission(d);
  } else if (k === 'info' || k === 'warn') {
    el('note', esc(d.text || ''));
  } else if (k === 'error') {
    const e = el('note'); e.style.color = 'var(--err)';
    e.textContent = d.text || 'error';
  } else if (k === 'done' || k === 'end') {
    botBubble = null;
    if (k === 'end') finish();
  }
  log.scrollTop = log.scrollHeight;
}

function showPermission(d) {
  const box = el('perm');
  box.innerHTML = '<div class="q">Cagentic wants to run <code>' + esc(d.tool) +
    '</code>' + (d.summary ? ' &mdash; ' + esc(d.summary) : '') + '</div>';
  const answer = (a) => {
    box.querySelectorAll('button').forEach(b => b.disabled = true);
    box.innerHTML += '<div class="q" style="margin-top:6px">&rarr; ' + a + '</div>';
    fetch('/permission', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({answer: a})
    });
  };
  for (const [cls, lbl, a] of [['yes','Approve','yes'],
                               ['always','Always','always'],
                               ['no','Deny','no']]) {
    const btn = document.createElement('button');
    btn.className = cls; btn.textContent = lbl;
    btn.onclick = () => answer(a);
    box.appendChild(btn);
  }
}

function finish() {
  sendBtn.disabled = false;
  input.disabled = false;
  input.focus();
}

async function send(text) {
  addMsg('you', 'You').textContent = text;
  botBubble = null;
  sendBtn.disabled = true;
  input.disabled = true;
  let res;
  try {
    res = await fetch('/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
  } catch (e) {
    el('note').textContent = 'connection failed'; finish(); return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    let chunk;
    try { chunk = await reader.read(); } catch (e) { break; }
    if (chunk.done) break;
    buf += dec.decode(chunk.value, {stream: true});
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 2);
      if (line.startsWith('data: ')) {
        try { handle(JSON.parse(line.slice(6))); } catch (e) {}
      }
    }
  }
  finish();
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || sendBtn.disabled) return;
  input.value = '';
  send(text);
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});
</script>
</body>
</html>
"""
