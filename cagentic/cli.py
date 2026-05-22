"""Command-line entry point for Cagentic."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, config, notes as _notes, reminders as _reminders, sessions, ui
from . import diff as _diff
from .agent import Agent
from .ollama_client import OllamaClient, OllamaError, _is_apple_silicon
from .prompt import Prompt


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cagentic",
        description="Your local personal AI assistant, powered by Ollama.",
    )
    p.add_argument("-p", "--prompt", help="One-shot prompt; print response and exit.")
    p.add_argument("-m", "--model", help="Ollama model (overrides saved config).")
    p.add_argument("--host", help="Ollama host (overrides saved config).")
    p.add_argument("-C", "--cwd", default=".", help="Working dir (default: cwd).")
    p.add_argument("--yolo", action="store_true", help="Auto-approve all tool calls.")
    p.add_argument("-t", "--temperature", type=float)
    p.add_argument("--name", help="What the assistant should call you (e.g. --name Alex).")
    p.add_argument("--reset-config", action="store_true")
    p.add_argument("-V", "--version", action="version", version=f"cagentic {__version__}")
    return p.parse_args(argv)


HELP_TEXT = """\
Slash commands:
  /help                  show this help
  /tools                 list tools the model can call
  /groups [en/disable G] show or change which tool groups are sent
  /cd [path]             show or change the working dir
  /notes                 list saved notes
  /note <name>           show one note
  /remind [add <text>]   list reminders or add one
  /mcp [server]          list MCP servers, or list tools on one
  /browser               Chrome extension status + setup steps
  /gateway [off]         start (or stop) the Cagentic web UI
  /plan on|off           toggle plan mode (read-only)
  /todo [add|done|clear] session todo list
  /stream on|off         toggle token streaming
  /diag                  print model / workspace / tools / mcp status
  /model [name]          show or switch model (saved)
  /models                list installed Ollama models
  /host [url]            show or change Ollama host
  /config                show current config (tokens redacted)
  /set <key> <value>     set a config value (e.g. user_name Alex)
  /name <your name>      tell the assistant what to call you
  /login github <token>  save a GitHub PAT
  /logout github         remove the saved token
  /whoami                show authenticated GitHub user
  /clear                 reset conversation history
  /diff [N]              show file edits this session
  /undo                  revert the most recent file edit
  /retry                 re-run your last message
  /new [title]           start a new conversation
  /resume [id|num]       list/resume saved conversations
  /sessions              list saved conversations
  /save [title]          force-save current conversation
  /rename <new title>    rename current conversation
  /delete <id|num>       delete a saved conversation
  /yolo [on|off]         toggle auto-approve
  /exit, /quit           leave
"""


def _pick_model_interactive(client: OllamaClient) -> str | None:
    try:
        models = client.list_models()
    except OllamaError as e:
        ui.error(str(e))
        ui.warn("Is `ollama serve` running?")
        return None

    print()
    ui.info("Welcome to Cagentic. Pick the Ollama model to use.")
    if models:
        print()
        for i, m in enumerate(models, 1):
            print(f"  {i:>2}. {m}")
        print()
        prompt = "Choose a number, or type a model name: "
    else:
        ui.warn("No models installed locally.")
        ui.warn("Suggested for general assistant use: llama3.1:8b, qwen2.5:7b, mistral-nemo")
        prompt = "Type a model name: "

    try:
        ans = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not ans:
        return None
    if ans.isdigit() and models:
        idx = int(ans) - 1
        if 0 <= idx < len(models):
            return models[idx]
    return ans


def _redact(cfg: dict) -> dict:
    out = {**cfg, "github": dict(cfg.get("github", {}))}
    tok = out["github"].get("token")
    if tok:
        out["github"]["token"] = tok[:4] + "…" + tok[-4:] if len(tok) > 8 else "••••"
    # Also redact any tokens in MCP server env blocks.
    mcp = dict(out.get("mcp") or {})
    servers = dict(mcp.get("servers") or {})
    for name, spec in list(servers.items()):
        if not isinstance(spec, dict):
            continue
        env = dict(spec.get("env") or {})
        for k, v in list(env.items()):
            if any(s in k.lower() for s in ("token", "secret", "key", "password")):
                env[k] = "••••" if not v else (str(v)[:4] + "…")
        servers[name] = {**spec, "env": env}
    mcp["servers"] = servers
    out["mcp"] = mcp
    return out


def _apply_setting_live(agent: Agent, key: str, value) -> bool:
    if key == "temperature":
        try:
            agent.engine.temperature = float(value); return True
        except (TypeError, ValueError):
            return False
    if key == "ollama.num_ctx":
        try:
            agent.client.num_ctx = int(value); return True
        except (TypeError, ValueError):
            return False
    if key == "ollama.stream":
        agent.engine.stream = bool(value); return True
    if key == "ollama.keep_alive":
        agent.client.keep_alive = value; return True
    if key == "yolo":
        agent.state.update(yolo=bool(value)); return True
    if key == "user_name":
        agent.state.update(user_name=str(value) if value else None)
        agent.engine.refresh_system_prompt()
        return True
    return False


def _apply_to_agent(agent: Agent, cfg: dict) -> None:
    agent.state.github_token = config.get_value(cfg, "github.token")
    agent.state.yolo = bool(cfg.get("yolo", agent.state.yolo))
    agent.state.insecure_ssl = bool(config.get_value(cfg, "insecure_ssl", False))


def _autosave(session: dict, agent: Agent) -> None:
    session["model"] = agent.model
    session["messages"] = [m for m in agent.messages if m.get("role") != "system"]
    sessions.save(session)


def _print_sessions(active_id: str | None = None) -> list[dict]:
    listed = sessions.list_all()
    if not listed:
        ui.info("(no saved conversations)")
        return []
    print()
    print(ui.color(f"  {'#':<3} {'id':<14} {'updated':<10} {'turns':<6} {'model':<20} title", ui.GRAY))
    for i, s in enumerate(listed, 1):
        marker = ui.color(" ✦", ui.GLOW) if s["id"] == active_id else "  "
        print(f"{marker}{i:<3} {s['id']:<14} {sessions.fmt_time(s['updated_at']):<10} "
              f"{s['turns']:<6} {s['model'][:19]:<20} {s['title'][:60]}")
    return listed


def _resolve_session_arg(arg: str, listed: list[dict]) -> dict | None:
    if not arg:
        return None
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(listed):
            return listed[idx]
        return None
    for s in listed:
        if s["id"] == arg or s["id"].startswith(arg):
            return s
    return None


def _replay_conversation(messages: list[dict], max_turns: int = 12) -> None:
    convo = [m for m in messages if m.get("role") != "system"]
    if not convo:
        ui.info("(empty conversation)")
        return
    user_idxs = [i for i, m in enumerate(convo) if m.get("role") == "user"]
    start = 0
    if len(user_idxs) > max_turns:
        start = user_idxs[-max_turns]
        ui.info(f"… {user_idxs.index(user_idxs[-max_turns])} earlier turn(s) hidden")
    ui.hr()
    for m in convo[start:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "user":
            if content.startswith((
                "Tool result for ", "[background] ",
                "STOP. You have called", "STOP. Tool outputs",
            )):
                continue
            print(ui.color("✦ ", ui.GLOW) + ui.color(content, ui.SURFACE))
        elif role == "assistant":
            if content:
                ui.assistant(content)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                print(ui.color("  ↳ ", ui.DUSK) + ui.color(fn.get("name", "?"), ui.DUSK))
        elif role == "tool":
            first = content.splitlines()[0][:120] if content else ""
            ui.tool_result(first, ok=not first.startswith("ERROR"))
    ui.hr()


def _settle_in(agent: Agent) -> None:
    """First thing Cagentic says — a short, warm orientation rather than a
    log dump. Mentions overdue reminders and how much it remembers, so the
    user knows it's picked up where they left off."""
    name = agent.state.user_name
    try:
        rems = _reminders.list_all()
        notes_n = len(_notes.list_all())
    except Exception:
        rems, notes_n = [], 0
    import time as _t
    overdue = [r for r in rems if r.due_at and r.due_at < _t.time() and r.status == "pending"]

    bits = []
    if rems:
        bits.append(f"{len(rems)} reminder{'s' if len(rems) != 1 else ''} on your list")
    if notes_n:
        bits.append(f"{notes_n} note{'s' if notes_n != 1 else ''} I remember")
    if bits:
        ui.info("I've got " + " and ".join(bits) + ".")
    else:
        opener = f"I'm here, {name}." if name else "I'm here."
        ui.info(opener + " Tell me what you need — or ask me to remember something.")

    if overdue:
        print()
        ui.warn(f"a heads-up — {len(overdue)} reminder"
                f"{'s are' if len(overdue) != 1 else ' is'} overdue:")
        for r in overdue[:5]:
            print("    " + r.short().strip())
        if len(overdue) > 5:
            print(f"    …and {len(overdue) - 5} more — type /remind to see them all")


def repl(agent: Agent, cfg: dict, gateway_holder: dict | None = None) -> int:
    gateway_holder = gateway_holder if gateway_holder is not None else {"server": None}
    ui.banner(agent.model, str(agent.state.workspace),
              tools_enabled=agent.tools_enabled,
              user_name=agent.state.user_name)

    session = sessions.make(agent.model)

    def _on_turn(a):
        _autosave(session, a)

    agent.on_turn_complete = _on_turn
    agent.engine.session_id = session["id"]

    _settle_in(agent)

    prompt = Prompt()
    if prompt.status_note:
        ui.warn(prompt.status_note)
    if not agent.engine.stream:
        ui.warn("streaming is OFF — use /stream on to see tokens live.")

    last_user_input = ""
    while True:
        ui.prepare_for_input()
        print()
        try:
            line = prompt.ask(ui.color("✦ ", ui.GLOW)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue

        if line.startswith("/"):
            parts = line.split(maxsplit=2)
            cmd = parts[0][1:].lower()
            arg1 = parts[1] if len(parts) > 1 else ""
            arg2 = parts[2] if len(parts) > 2 else ""

            if cmd in ("exit", "quit"):
                return 0
            if cmd == "help":
                print(HELP_TEXT)
                continue
            if cmd == "tools":
                from .tools import _all_tools
                mode = "native" if agent.tools_enabled else "text-protocol fallback"
                ui.info(f"mode: {mode}")
                for n in _all_tools():
                    print(f"  - {n}")
                continue
            if cmd == "groups":
                from .tools import TOOL_GROUPS, DEFAULT_GROUPS
                active = agent.state.tool_groups if agent.state.tool_groups is not None else DEFAULT_GROUPS
                if not arg1:
                    ui.info("tool groups (✓ = sent to the model):")
                    for g, names in TOOL_GROUPS.items():
                        mark = ui.color("✓", ui.OK) if g in active else ui.color("·", ui.SOFT)
                        print(f"  {mark} {g:<12} ({len(names)} tools)")
                    continue
                if arg1 in ("enable", "disable") and arg2:
                    if arg2 not in TOOL_GROUPS:
                        ui.warn(f"unknown group '{arg2}' — see /groups")
                        continue
                    groups = set(active)
                    if arg1 == "enable":
                        groups.add(arg2)
                    else:
                        groups.discard(arg2)
                    agent.state.tool_groups = groups
                    agent.engine.refresh_system_prompt()
                    config.set_value(cfg, "tool_groups", sorted(groups))
                    config.save(cfg)
                    ui.info(f"{arg1}d '{arg2}' — {len(groups)} group(s) active")
                else:
                    ui.warn("usage: /groups  |  /groups enable <name>  |  /groups disable <name>")
                continue
            if cmd == "cd":
                if not arg1:
                    ui.info(f"workspace: {agent.state.workspace}")
                    continue
                raw = os.path.expanduser(os.path.expandvars(arg1))
                target = Path(raw)
                if not target.is_absolute():
                    target = agent.state.workspace / target
                target = target.resolve()
                if not target.is_dir():
                    ui.error(f"not a directory: {target}")
                    continue
                agent.state.update(workspace=target)
                agent.engine.refresh_system_prompt()
                ui.info(f"workspace → {target}")
                continue

            # ---- notes ----
            if cmd == "notes":
                items = _notes.list_all()
                if not items:
                    ui.info("(no notes yet — ask Cagentic to remember something)")
                else:
                    for n in items[:40]:
                        print("  " + n.short())
                continue
            if cmd == "note":
                if not arg1:
                    ui.warn("usage: /note <name>")
                    continue
                n = _notes.get(arg1)
                if not n:
                    ui.warn(f"no note named '{arg1}'")
                else:
                    print()
                    print(ui.color(f"  ❀ {n.name}", ui.DUSK + ui.BOLD))
                    print(ui.color("  " + "─" * (len(n.name) + 4), ui.PLUM))
                    print(n.body)
                continue

            # ---- reminders ----
            if cmd in ("remind", "reminders"):
                if arg1 == "add":
                    if not arg2:
                        ui.warn("usage: /remind add <text>  (or include time: 'call mom @ tomorrow')")
                        continue
                    # Crude '@ when' splitter
                    text, when = arg2, None
                    if " @ " in arg2:
                        text, when = arg2.rsplit(" @ ", 1)
                    due = _reminders.parse_when(when) if when else None
                    r = _reminders.add(text.strip(), due_at=due)
                    ui.info(f"added: {r.short().strip()}")
                    continue
                if arg1 == "done":
                    if not arg2:
                        ui.warn("usage: /remind done <id>")
                        continue
                    r = _reminders.update(arg2, status="done")
                    ui.info(f"marked done: {r.short().strip()}" if r else f"no reminder {arg2}")
                    continue
                if arg1 == "delete":
                    if not arg2:
                        ui.warn("usage: /remind delete <id>")
                        continue
                    ok = _reminders.delete(arg2)
                    ui.info("deleted" if ok else f"no reminder {arg2}")
                    continue
                if arg1 == "clear":
                    # Don't actually delete — just mark all done. Safer.
                    count = 0
                    for r in _reminders.list_all():
                        _reminders.update(r.id, status="done")
                        count += 1
                    ui.info(f"marked {count} reminder(s) done")
                    continue
                # bare /remind — list
                rems = _reminders.list_all(include_done=(arg1 == "all"))
                if not rems:
                    ui.info("(no reminders)")
                for r in rems[:40]:
                    print(r.short())
                continue

            # ---- mcp ----
            if cmd == "mcp":
                # Lazy-init the MCP manager on the state
                from .mcp_client import MCPManager
                if agent.state.mcp is None:
                    agent.state.mcp = MCPManager(cfg)
                mgr = agent.state.mcp
                if not arg1:
                    names = mgr.names()
                    if not names:
                        ui.info("no MCP servers configured.")
                        ui.info("add one under mcp.servers in ~/.config/cagentic/config.json, e.g.:")
                        print('  {"mcp": {"servers": {')
                        print('    "notion": {"command": ["npx", "-y", "@notionhq/notion-mcp-server"],')
                        print('               "env": {"NOTION_TOKEN": "secret_xxx"}, "enabled": true}')
                        print('  }}}')
                    else:
                        ui.info(f"{len(names)} MCP server(s) configured:")
                        for n in names:
                            print(f"  - {n}")
                        ui.info("use /mcp <server> to list its tools.")
                else:
                    try:
                        tools = mgr.list_tools(arg1)
                    except Exception as e:
                        ui.error(str(e))
                        continue
                    if not tools:
                        ui.info(f"(server '{arg1}' exposes no tools)")
                    else:
                        ui.info(f"{arg1}: {len(tools)} tool(s)")
                        for t in tools[:40]:
                            n = t.get("name", "?")
                            d = (t.get("description") or "").splitlines()[0][:140]
                            print(f"  - {n}  —  {d}")
                continue
            if cmd == "browser":
                from .browser import BrowserBridge
                if agent.state.browser is None:
                    port = int((cfg.get("browser") or {}).get("port", 8765))
                    b = BrowserBridge(port=port)
                    b.start()
                    agent.state.browser = b
                b = agent.state.browser
                ext_dir = Path(__file__).resolve().parent.parent / "extension"
                if b.error:
                    ui.error(f"browser bridge couldn't start: {b.error}")
                elif b.is_connected():
                    ui.info(f"Chrome extension is connected — bridge on port {b.port}.")
                    ui.info("Cagentic can read pages, open tabs, click, and fill forms.")
                else:
                    ui.warn(f"bridge running on port {b.port}, but the Chrome extension "
                            f"isn't connected yet.")
                    ui.info("To connect it:")
                    print(f"  1. Open  chrome://extensions")
                    print(f"  2. Turn on 'Developer mode' (top-right)")
                    print(f"  3. Click 'Load unpacked' and pick this folder:")
                    print(ui.color(f"       {ext_dir}", ui.GLOW))
                    print(f"  4. The extension connects automatically; re-run /browser.")
                continue
            if cmd == "gateway":
                from .gateway import Gateway
                gw = gateway_holder.get("server")
                if arg1.lower() in ("off", "stop"):
                    if gw is None or not gw.running:
                        ui.info("the gateway isn't running.")
                    else:
                        gw.stop()
                        gateway_holder["server"] = None
                        ui.info("gateway stopped.")
                    continue
                if gw is not None and gw.running:
                    ui.info(f"gateway is already live at {gw.url()}")
                    continue
                port = int((cfg.get("gateway") or {}).get("port", 8700))
                gw = Gateway(agent, cfg, port=port)
                if gw.start():
                    gateway_holder["server"] = gw
                    ui.info(f"gateway is live — open {gw.url()} in your browser.")
                    ui.info("it's the full assistant on the web; tool approvals pop "
                            "up right in the page. /gateway off to stop it.")
                else:
                    ui.error(f"gateway couldn't start: {gw.error}")
                continue

            if cmd == "plan":
                want = arg1.lower() if arg1 else ("off" if agent.state.plan_mode else "on")
                if want not in ("on", "off"):
                    ui.warn("usage: /plan on|off")
                    continue
                agent.state.update(plan_mode=(want == "on"))
                agent.engine.refresh_system_prompt()
                ui.info(f"plan mode: {'ON (read-only)' if agent.state.plan_mode else 'off'}")
                continue
            if cmd == "todo":
                todos = list(agent.state.todos or [])
                if not arg1:
                    if not todos:
                        ui.info("(no todos)")
                    for i, t in enumerate(todos, 1):
                        mark = {"done": "✓", "pending": " ", "active": "→", "blocked": "✗"}.get(t.get("status", "pending"), "?")
                        print(f"  [{mark}] {i}. {t.get('text', '')}")
                    continue
                if arg1 == "add":
                    text = arg2.strip()
                    if not text:
                        ui.warn("usage: /todo add <text>")
                        continue
                    todos.append({"text": text, "status": "pending"})
                    agent.state.update(todos=todos)
                    ui.info(f"added: {text}")
                    continue
                if arg1 == "done" and arg2.isdigit():
                    i = int(arg2) - 1
                    if 0 <= i < len(todos):
                        todos[i]["status"] = "done"
                        agent.state.update(todos=todos)
                        ui.info(f"done: {todos[i]['text']}")
                    continue
                if arg1 == "clear":
                    agent.state.update(todos=[])
                    ui.info("cleared todos")
                    continue
                ui.warn("usage: /todo  |  /todo add <text>  |  /todo done <n>  |  /todo clear")
                continue
            if cmd == "diag":
                from .tools import DEFAULT_GROUPS
                groups = agent.state.tool_groups if agent.state.tool_groups is not None else DEFAULT_GROUPS
                ui.info(f"model:    {agent.model}")
                ui.info(f"name:     {agent.state.user_name or '(not set — /name <your name>)'}")
                ui.info(f"workspace: {agent.state.workspace}")
                ui.info(f"home:     {Path.home()}")
                ui.info(f"tools:    {'native' if agent.tools_enabled else 'text-protocol fallback'}")
                ui.info(f"groups:   {', '.join(sorted(groups))}")
                ui.info(f"stream:   {'on' if agent.engine.stream else 'off'}")
                ui.info(f"num_ctx:  {agent.client.num_ctx}")
                status = agent.client.model_vram_status(agent.model)
                mac = _is_apple_silicon()
                label = "memory" if mac else "vram"
                if status is None:
                    ui.info(f"{label}:    model not currently loaded")
                elif status["fully_gpu"]:
                    place = "in Metal buffer (unified)" if mac else "fully on GPU ✓"
                    ui.info(f"{label}:    {status['size_vram'] / (1024**3):.1f} GB · {place}")
                else:
                    size_gb = status["size"] / (1024**3)
                    cpu_gb = status["cpu_bytes"] / (1024**3)
                    pct = status["cpu_percent"]
                    ui.warn(f"{label}:    {cpu_gb:.1f}/{size_gb:.1f} GB on CPU ({pct:.0f}% offloaded — slow)")
                mcp_servers = list(((cfg.get("mcp") or {}).get("servers") or {}).keys())
                ui.info(f"mcp:      {len(mcp_servers)} configured ({', '.join(mcp_servers) or 'none'})")
                notes_n = len(_notes.list_all())
                rems_n = len(_reminders.list_all())
                ui.info(f"data:     {notes_n} notes · {rems_n} active reminders")
                ui.info(f"github:   {'logged in' if agent.state.github_token else 'no token'}")
                ui.info(f"input:    {prompt.backend}")
                continue
            if cmd == "stream":
                want = arg1.lower() if arg1 else ("off" if agent.engine.stream else "on")
                if want not in ("on", "off"):
                    ui.warn("usage: /stream on|off")
                    continue
                agent.engine.stream = (want == "on")
                config.set_value(cfg, "ollama.stream", agent.engine.stream)
                config.save(cfg)
                ui.info(f"streaming: {'on' if agent.engine.stream else 'off'} (saved)")
                continue
            if cmd == "model":
                if not arg1:
                    ui.info(f"current model: {agent.model}")
                else:
                    agent.model = arg1
                    cfg["model"] = arg1
                    config.save(cfg)
                    supported = config.get_value(cfg, f"models.{arg1}.tools_supported", True)
                    agent.tools_enabled = bool(supported)
                    agent.engine.refresh_system_prompt()
                    ui.info(f"switched to {arg1} (saved)")
                continue
            if cmd == "models":
                try:
                    for m in agent.client.list_models():
                        marker = " *" if m == agent.model else ""
                        print(f"  - {m}{marker}")
                except OllamaError as e:
                    ui.error(str(e))
                continue
            if cmd == "host":
                if not arg1:
                    ui.info(f"current host: {agent.client.host}")
                else:
                    # The client's host setter normalizes (adds scheme/port,
                    # rewrites bind-all 0.0.0.0 to a routable loopback).
                    agent.client.host = arg1
                    cfg["host"] = agent.client.host
                    config.save(cfg)
                    if "0.0.0.0" in arg1 or arg1.strip() in ("::", "[::]", "0"):
                        ui.info(f"{arg1!r} is a bind-all address — "
                                f"using {agent.client.host} (a client can't dial 0.0.0.0).")
                    else:
                        ui.info(f"host set to {agent.client.host}")
                continue
            if cmd == "config":
                import json as _json
                print(_json.dumps(_redact(cfg), indent=2))
                ui.info(f"file: {config.config_path()}")
                continue
            if cmd == "set":
                if not arg1 or not arg2:
                    ui.warn("usage: /set <key> <value>")
                    continue
                v: object = arg2
                if arg2.lower() in ("true", "false"):
                    v = arg2.lower() == "true"
                else:
                    try:
                        v = float(arg2) if "." in arg2 else int(arg2)
                    except ValueError:
                        pass
                config.set_value(cfg, arg1, v)
                config.save(cfg)
                applied = _apply_setting_live(agent, arg1, v)
                ui.info(f"set {arg1} = {v}" + ("  → applied live" if applied else "  → config only"))
                continue
            if cmd == "name":
                if not arg1:
                    ui.info(f"I'm calling you: {agent.state.user_name or '(no name set)'}")
                    continue
                full = (arg1 + (" " + arg2 if arg2 else "")).strip()
                agent.state.update(user_name=full)
                agent.engine.refresh_system_prompt()
                config.set_value(cfg, "user_name", full)
                config.save(cfg)
                ui.info(f"got it — I'll call you {full}.")
                continue
            if cmd == "login":
                if arg1.lower() != "github" or not arg2:
                    ui.warn("usage: /login github <token>")
                    continue
                config.set_value(cfg, "github.token", arg2)
                config.save(cfg)
                _apply_to_agent(agent, cfg)
                ui.info("GitHub token saved.")
                continue
            if cmd == "logout":
                if arg1.lower() != "github":
                    ui.warn("usage: /logout github")
                    continue
                config.set_value(cfg, "github.token", None)
                config.save(cfg)
                _apply_to_agent(agent, cfg)
                ui.info("GitHub token removed.")
                continue
            if cmd == "whoami":
                from .github import t_gh_whoami
                print(t_gh_whoami({}, agent.ctx))
                continue
            if cmd == "clear":
                agent.reset()
                ui.info("history cleared")
                continue
            if cmd == "diff":
                hist = list(agent.state.edit_history or [])
                if not hist:
                    ui.info("(no edits this session)")
                    continue
                limit = int(arg1) if arg1.isdigit() else len(hist)
                for entry in hist[-limit:]:
                    path = entry.get("path", "?")
                    op = entry.get("op", "edit")
                    before = entry.get("before", "")
                    after = entry.get("after", "")
                    adds, dels = _diff.stats(before, after)
                    print(ui.color(f"  {op}  {path}  ", ui.DUSK) +
                          ui.color(f"(+{adds} -{dels})", ui.MUTED))
                    rendered = _diff.render(before, after, path, max_lines=20)
                    if rendered:
                        print(rendered)
                continue
            if cmd == "undo":
                hist = list(agent.state.edit_history or [])
                if not hist:
                    ui.info("(no edits to undo)")
                    continue
                entry = hist.pop()
                p = Path(entry["path"])
                try:
                    p.write_text(entry.get("before", ""))
                except OSError as e:
                    ui.error(f"undo failed: {e}")
                    continue
                agent.state.update(edit_history=hist)
                ui.info(f"reverted {entry.get('op', 'edit')} on {p}")
                continue
            if cmd == "new":
                title = (arg1 + (" " + arg2 if arg2 else "")).strip() or None
                if agent.messages and len(agent.messages) > 1:
                    if title:
                        session["title"] = title
                    _autosave(session, agent)
                    ui.info(f"saved {session['id']}")
                session.clear()
                session.update(sessions.make(agent.model, title=title))
                agent.reset()
                agent.on_turn_complete = _on_turn
                agent.engine.session_id = session["id"]
                ui.info("fresh start — clean slate. (Notes and reminders are still with me.)")
                continue
            if cmd == "resume":
                listed = _print_sessions(active_id=session.get("id"))
                if not arg1:
                    if listed:
                        ui.info("usage: /resume <id|number>")
                    continue
                target = _resolve_session_arg(arg1, listed)
                if not target:
                    ui.warn(f"no session matching '{arg1}'")
                    continue
                data = sessions.load(target["id"])
                if not data:
                    ui.error(f"could not load {target['id']}")
                    continue
                if len(agent.messages) > 1:
                    _autosave(session, agent)
                session.clear()
                session.update(data)
                agent.model = data.get("model") or agent.model
                saved_messages = data.get("messages", [])
                agent.load_messages(saved_messages)
                agent.on_turn_complete = _on_turn
                agent.engine.session_id = session["id"]
                ui.info(f"resumed {session['id']} — {session.get('title', '')}")
                _replay_conversation(saved_messages)
                continue
            if cmd == "sessions":
                _print_sessions(active_id=session.get("id"))
                continue
            if cmd == "save":
                if arg1 or arg2:
                    session["title"] = (arg1 + (" " + arg2 if arg2 else "")).strip()
                _autosave(session, agent)
                ui.info(f"saved {session['id']} — {session.get('title', '')}")
                continue
            if cmd == "rename":
                new_title = (arg1 + (" " + arg2 if arg2 else "")).strip()
                if not new_title:
                    ui.warn("usage: /rename <new title>")
                    continue
                session["title"] = new_title
                _autosave(session, agent)
                ui.info(f"renamed to '{new_title}'")
                continue
            if cmd == "delete":
                listed = sessions.list_all()
                target = _resolve_session_arg(arg1, listed)
                if not target:
                    ui.warn(f"no session matching '{arg1}'")
                    continue
                if target["id"] == session.get("id"):
                    ui.warn("can't delete the active session — use /new first")
                    continue
                if sessions.delete(target["id"]):
                    ui.info(f"deleted {target['id']}")
                else:
                    ui.warn("delete failed")
                continue
            if cmd == "yolo":
                sub = arg1.lower() if arg1 else ""
                if sub in ("on", "true", "1", "yes"):
                    want = True
                elif sub in ("off", "false", "0", "no"):
                    want = False
                elif sub == "":
                    want = not agent.state.yolo
                else:
                    ui.warn("usage: /yolo on|off")
                    continue
                agent.state.update(yolo=want)
                cfg["yolo"] = want
                config.save(cfg)
                ui.info(f"yolo: {'ON' if want else 'OFF'} (saved)")
                continue
            if cmd == "retry":
                if not last_user_input:
                    ui.warn("nothing to retry yet")
                    continue
                ui.info(f"retrying: {last_user_input[:80]}")
                line = last_user_input
            else:
                ui.warn(f"unknown command: /{cmd}")
                continue

        last_user_input = line
        try:
            agent.turn(line)
        except KeyboardInterrupt:
            ui.warn("interrupted")
            continue


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.reset_config:
        try:
            config.config_path().unlink()
            ui.info(f"removed {config.config_path()}")
        except FileNotFoundError:
            ui.info("no config to reset")

    cfg = config.load()

    if args.name:
        config.set_value(cfg, "user_name", args.name)
        config.save(cfg)

    raw_host = args.host or os.environ.get("OLLAMA_HOST") or cfg.get("host", "http://localhost:11434")
    client = OllamaClient(
        host=raw_host,
        connect_timeout=float(config.get_value(cfg, "ollama.connect_timeout", 15.0)),
        read_timeout=float(config.get_value(cfg, "ollama.read_timeout", 1800.0)),
        nonstream_read_timeout=float(config.get_value(cfg, "ollama.nonstream_read_timeout", 1800.0)),
        keep_alive=config.get_value(cfg, "ollama.keep_alive", "30m"),
        num_ctx=config.get_value(cfg, "ollama.num_ctx", 8192),
        num_predict=config.get_value(cfg, "ollama.num_predict", -1),
    )
    # Store the normalized host the client will actually use — so /config and
    # the next launch show the routable address, not a bind-only 0.0.0.0.
    cfg["host"] = client.host
    if "0.0.0.0" in raw_host or raw_host.strip() in ("::", "[::]", "0"):
        ui.info(f"Ollama host {raw_host!r} is a bind-all address — "
                f"connecting to {client.host} instead.")

    model = args.model or os.environ.get("CAGENTIC_MODEL") or cfg.get("model")
    if not model:
        chosen = _pick_model_interactive(client)
        if not chosen:
            ui.error("No model selected. Exiting.")
            return 1
        model = chosen
        cfg["model"] = model
        config.save(cfg)
        ui.info(f"saved model '{model}' to {config.config_path()}")

    root = Path(args.cwd).resolve()
    if not root.is_dir():
        ui.error(f"workspace not a directory: {root}")
        return 2

    # If launched from inside the Cagentic install dir itself, default to home.
    if args.cwd == ".":
        cagentic_root = Path(__file__).resolve().parent.parent
        inside_install = root == cagentic_root or cagentic_root in root.parents
        if inside_install:
            root = Path.home()

    try:
        models = client.list_models()
    except OllamaError as e:
        ui.error(str(e))
        ui.warn("Is `ollama serve` running?")
        return 1
    if model not in models and models:
        ui.warn(f"model '{model}' not installed locally. Available: {', '.join(models[:8])}")
        ui.warn(f"Pull it with:  ollama pull {model}")

    temperature = args.temperature if args.temperature is not None else float(cfg.get("temperature", 0.4))
    yolo = args.yolo or bool(cfg.get("yolo", False))
    user_name = cfg.get("user_name")

    tools_supported = config.get_value(cfg, f"models.{model}.tools_supported", True)
    if tools_supported is False:
        ui.warn(f"note: '{model}' is known not to support tool calls — running tool-less.")

    def _remember_no_tools(_a):
        config.set_value(cfg, f"models.{model}.tools_supported", False)
        config.save(cfg)

    agent = Agent(
        client=client,
        model=model,
        root=root,
        yolo=yolo,
        temperature=temperature,
        tools_enabled=bool(tools_supported),
        on_tools_disabled=_remember_no_tools,
        stream=bool(config.get_value(cfg, "ollama.stream", True)),
        config=cfg,
        user_name=user_name,
    )
    agent.state.github_token = config.get_value(cfg, "github.token")
    agent.state.insecure_ssl = bool(config.get_value(cfg, "insecure_ssl", False))

    saved_groups = config.get_value(cfg, "tool_groups", None)
    if isinstance(saved_groups, list) and saved_groups:
        agent.state.tool_groups = set(saved_groups)

    # Wire MCP manager onto state, but lazy-start servers
    if cfg.get("mcp", {}).get("servers"):
        from .mcp_client import MCPManager
        agent.state.mcp = MCPManager(cfg)

    # Start the browser bridge so the Chrome extension can connect.
    br_cfg = cfg.get("browser") or {}
    if br_cfg.get("enabled", True):
        from .browser import BrowserBridge
        bridge = BrowserBridge(port=int(br_cfg.get("port", 8765)))
        if bridge.start():
            bridge.set_status(model=model, activity="idle")
            agent.state.browser = bridge
        else:
            ui.warn(f"browser bridge: {bridge.error}")

    # The /gateway web UI, started on demand. Held here so /gateway can toggle it.
    gateway_holder: dict = {"server": None}

    # Shutdown handlers: unload model + shut down MCP / browser / gateway.
    import atexit
    import signal
    _shutdown_done = {"flag": False}

    def _shutdown(*_):
        if _shutdown_done["flag"]:
            return
        _shutdown_done["flag"] = True
        try:
            client.unload(agent.model)
        except Exception:
            pass
        try:
            if agent.state.mcp is not None:
                agent.state.mcp.shutdown()
        except Exception:
            pass
        try:
            if agent.state.browser is not None:
                agent.state.browser.stop()
        except Exception:
            pass
        try:
            if gateway_holder["server"] is not None:
                gateway_holder["server"].stop()
        except Exception:
            pass

    atexit.register(_shutdown)
    for sig_name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_: (_shutdown(), sys.exit(0)))
        except (ValueError, OSError):
            pass

    if args.prompt:
        agent.turn(args.prompt)
        return 0
    return repl(agent, cfg, gateway_holder)


if __name__ == "__main__":
    sys.exit(main())
