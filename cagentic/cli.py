"""Command-line entry point for Cagentic."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import logging
import os
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

import click

logger = logging.getLogger(__name__)

from . import (
    __version__,
    command_utils,
    config,
    sessions,
    ui,
)
from . import diff as _diff
from . import logs as _logs
from . import (
    notes as _notes,
)
from . import (
    reminders as _reminders,
)
from .agent import Agent
from .ollama_client import OllamaClient, OllamaError, _is_apple_silicon, _normalize_host
from .prompt import ALL_COMMANDS, Prompt
from .providers import (
    build_client as _build_client,
)
from .providers import (
    parse_model as _parse_model_provider,
)
from .providers import resolve_model_selector as _resolve_model_selector
from .services.compact import SUMMARY_MARKER

OUTPUT_FORMAT = click.Choice(("text", "json", "stream-json"), case_sensitive=False)
SERVICE = click.Choice(("github", "openai", "anthropic"), case_sensitive=False)
SHELL = click.Choice(("bash", "zsh", "fish"), case_sensitive=False)
PERMISSION_MODE = click.Choice(("ask", "accept-edits", "plan", "yolo"), case_sensitive=False)


def _nonempty(_ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise click.BadParameter("must not be empty", param=param)
    return cleaned


def _model_value(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    value = _nonempty(ctx, param, value)
    if value is None:
        return None
    _provider, model_name = _parse_model_provider(value)
    if not model_name:
        raise click.BadParameter(
            "must include a model name after the provider prefix",
            param=param,
        )
    return value


def _host_value(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    value = _nonempty(ctx, param, value)
    if value is None:
        return None
    try:
        from urllib.parse import urlparse

        normalized = _normalize_host(value)
        parsed = urlparse(normalized)
        _ = parsed.port
    except ValueError as exc:
        raise click.BadParameter(f"must be a valid HTTP(S) host: {exc}", param=param) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or any(char.isspace() for char in normalized)
    ):
        raise click.BadParameter("must be a valid HTTP(S) host", param=param)
    return value


@dataclass
class RuntimeOptions:
    mode: str
    model: str | None = None
    host: str | None = None
    cwd: Path | None = None
    temperature: float | None = None
    name: str | None = None
    yolo: bool | None = None
    prompt: str | None = None
    port: int | None = None
    dry_run: bool = False
    result: dict[str, Any] | None = None
    # Automation surface — what lets Cagentic be driven from a script or CI
    # rather than only from a person's terminal.
    continue_last: bool = False
    resume_id: str | None = None
    allowed_tools: str | None = None
    disallowed_tools: str | None = None
    permission_mode: str | None = None
    append_system_prompt: str | None = None
    # Emit newline-delimited JSON events instead of rendering to a terminal.
    stream_json: bool = False
    # The real stdout, held while human-facing output is redirected to stderr.
    stream_sink: Any = None


def _search_sessions(query: str) -> list[dict]:
    needle = query.casefold().strip()
    if not needle:
        return []
    results = []
    for data in sessions.search(needle):
        results.append(
            {
                "id": data.get("id", ""),
                "title": data.get("title", "untitled"),
                "model": data.get("model", "?"),
                "updated_at": data.get("updated_at", 0),
                "turns": sum(
                    1 for message in data.get("messages", []) if message.get("role") == "user"
                ),
            }
        )
    return results


def _setup_wizard(cfg: dict, *, dry_run: bool = False) -> bool:
    print()
    ui.heading("Setup")
    ui.info("Press Enter to keep the current value.")
    current_model = str(cfg.get("model") or "")
    model = ui.input_prompt("Model", current_model or "none").strip() or current_model
    if model:
        cfg["model"] = model
    name = ui.input_prompt("Your name", str(cfg.get("user_name") or "")).strip()
    if name:
        cfg["user_name"] = name
    roots = ui.input_prompt("Allowed workspace roots (separate with OS path separator)").strip()
    if roots:
        config.set_value(cfg, "gateway.workspace_roots", roots.split(os.pathsep))
    lan = ui.input_prompt("Enable LAN gateway access?", "y/N").strip().lower()
    if lan in ("y", "yes"):
        config.set_value(cfg, "gateway.lan", True)
        if not config.get_value(cfg, "gateway.token"):
            import secrets

            config.set_value(cfg, "gateway.token", secrets.token_urlsafe(32))
    if dry_run:
        ui.info(f"dry run: would save setup to {config.config_path()} · no settings changed")
    else:
        try:
            config.save(cfg)
        except (OSError, TypeError, ValueError) as exc:
            ui.error(f"could not save setup: {exc}; no settings were changed")
            return False
        ui.info(f"saved setup to {config.config_path()}")
    return True


def _print_context(
    session_ref: str,
    threshold: float,
    *,
    compact: bool = False,
    dry_run: bool = False,
    as_json: bool = False,
    context_limit: int = 8192,
) -> int:
    from .services.compact import SUMMARY_MARKER, auto_compact
    from .token_count import count_messages

    listed = sessions.list_all()
    target = _resolve_session_arg(session_ref, listed)
    if target is None:
        message = _session_miss(session_ref, listed)
        if as_json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            ui.error(message)
        return 1
    session_id = target["id"]
    loaded = sessions.load(session_id)
    if loaded is None:
        message = f"could not load session: {session_id}"
        if as_json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            ui.error(message)
        return 1
    # Compaction mutates nested message dictionaries. Work on an isolated copy
    # so --dry-run cannot alter an object supplied by an in-memory store.
    data = copy.deepcopy(loaded)
    messages = [dict(message) for message in data.get("messages", [])]
    session_model = str(data.get("model") or "")
    before = count_messages(messages, session_model)
    # The session records the model it ran under, which beats the caller's
    # config-derived default: inspecting a Claude session from a machine
    # configured for a local model must not report an 8k window.
    if session_model:
        context_limit = command_utils.context_window(
            config.load(), default=context_limit, model_spec=session_model
        )
    limit = max(1, int(context_limit * threshold))
    if compact:
        auto_compact(messages, max_tokens=limit, keep_recent=6)
        data["messages"] = [
            message
            for message in messages
            if message.get("role") != "system"
            or SUMMARY_MARKER in str(message.get("content") or "")
        ]
        if not dry_run:
            try:
                sessions.save(data)
            except Exception as exc:
                logger.warning("could not save compacted session", exc_info=True)
                message = (
                    f"could not finish saving compacted session: {exc}; "
                    "the session file and index may differ, so reload before retrying"
                )
                if as_json:
                    print(json.dumps({"ok": False, "error": message}))
                else:
                    ui.error(message)
                return 1
    after = count_messages(messages, str(data.get("model") or ""))
    report = {
        "ok": True,
        "id": session_id,
        "before": before,
        "after": after,
        "dry_run": bool(compact and dry_run),
    }
    if as_json:
        print(json.dumps(report, indent=2))
    elif compact:
        verb = "would compact" if dry_run else "compacted"
        suffix = " · no changes saved" if dry_run else ""
        ui.info(f"{verb} {session_id}: {before:,} → {after:,} estimated tokens{suffix}")
    else:
        percent = before / max(1, limit)
        ui.info(
            f"{session_id}: {before:,} estimated tokens "
            f"({percent:.0%} of the {limit:,}-token compaction target)"
        )
    return 0


def _pick_model_interactive(client: OllamaClient) -> str | None:
    try:
        with ui.Spinner("connecting to Ollama"):
            models = _list_models_with_retry(client)
    except OllamaError as e:
        ui.error(str(e))
        ui.warn("Is `ollama serve` running?")
        return None

    print()
    ui.heading("Choose a model")
    ui.info("Select the Ollama model Cagentic should use.")
    if models:
        print()
        for i, m in enumerate(models, 1):
            prefix = f"  {i:>2}  "
            print(
                ui.color(prefix, ui.DUSK)
                + ui.color(ui.truncate(ui.single_line(m), ui.width() - 6), ui.SURFACE)
            )
        print()
        prompt = "Model number or name"
    else:
        ui.warn("No models installed locally.")
        ui.warn("Suggested for general assistant use: llama3.1:8b, qwen2.5:7b, mistral-nemo")
        prompt = "Model name"

    try:
        ans = ui.input_prompt(prompt).strip()
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


def print_help(workspace: Path | None = None) -> None:
    """Render the shared command catalog as a responsive reference.

    Project commands (.cagentic/commands/*.md) are appended as their own
    section, so a repo's custom commands are discoverable the same way the
    built-ins are rather than being a thing you have to already know about.
    """
    from .prompt import COMMAND_GROUPS

    groups = list(COMMAND_GROUPS)
    if workspace is not None:
        from .project_scope import command_summary, discover_commands

        found = discover_commands(workspace)
        if found:
            groups.append(
                (
                    "this project",
                    [
                        (f"/{name}", "[args]", command_summary(body))
                        for name, body in sorted(found.items())
                    ],
                )
            )

    usage = {
        name: (f"{name} {args}" if args else name)
        for _s, entries in groups
        for name, args, _h in entries
    }
    columns = ui.width()
    max_usage = max(len(value) for value in usage.values())
    usage_col = min(max_usage + 2, max(18, min(34, columns // 2)))

    print()
    ui.heading("Commands")
    for section, entries in groups:
        print()
        print("  " + ui.color(section.title(), ui.SURFACE + ui.BOLD))
        for name, _args, hint in entries:
            command = usage[name]
            hint_width = columns - 4 - usage_col
            if columns >= 44 and len(command) < usage_col and hint_width >= 16:
                hint_lines = textwrap.wrap(
                    hint,
                    width=hint_width,
                    break_long_words=True,
                    break_on_hyphens=False,
                ) or [""]
                for index, hint_line in enumerate(hint_lines):
                    if index == 0:
                        lead = "    " + ui.color(command, ui.DUSK)
                        lead += " " * (usage_col - len(command))
                    else:
                        lead = " " * (4 + usage_col)
                    print(lead + ui.color(hint_line, ui.MUTED))
                continue

            command_width = max(1, columns - 4)
            print("    " + ui.color(ui.truncate(command, command_width), ui.DUSK))
            for hint_line in textwrap.wrap(
                hint,
                width=max(1, columns - 6),
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""]:
                print("      " + ui.color(hint_line, ui.MUTED))

    print()
    ui.info("Type / for completions · use @path to attach a file")
    print()


def print_tools(agent: Agent) -> None:
    """List the model's tools by group, marking which groups are actually sent.

    A flat 60-line column told you nothing about why a tool wasn't being used;
    grouping shows the off switches (/groups) right next to what they control.
    """
    from .tools import DEFAULT_GROUPS, TOOL_GROUPS, _all_tools

    active = agent.state.tool_groups if agent.state.tool_groups is not None else DEFAULT_GROUPS
    known = set(_all_tools())

    print()
    ui.heading("Tools")
    print()
    for group, names in TOOL_GROUPS.items():
        on = group in active
        mark = ui.color("✓", ui.OK) if on else ui.color("·", ui.SOFT)
        head = ui.color(group, (ui.DUSK + ui.BOLD) if on else ui.SOFT)
        print(f"  {mark} {head}")
        if not on:
            for line in textwrap.wrap(
                f"off · enable with /groups enable {group}",
                width=max(1, ui.width() - 6),
            ):
                print("      " + ui.color(line, ui.SOFT))
        body = ", ".join(n for n in names if n in known)
        for line in textwrap.wrap(body, max(1, ui.width() - 6)):
            print("      " + ui.color(line, ui.MUTED if on else ui.SOFT))
    print()
    mode = "native" if agent.tools_enabled else "text-protocol fallback"
    sent = len({n for g in active for n in TOOL_GROUPS.get(g, ()) if n in known})
    ui.info(f"{sent} of {len(known)} tools sent to the model · mode: {mode}")


def _reminder_miss(rid: str) -> str:
    """Why an id didn't resolve — genuinely absent, or an ambiguous prefix."""
    matches = [r for r in _reminders.list_all(include_done=True) if r.id.startswith(rid)]
    if len(matches) > 1:
        return (
            f"'{rid}' matches {len(matches)} reminders "
            f"({', '.join(r.id for r in matches[:5])}) — use the full id"
        )
    return f"no reminder {rid}"


def _client_provider(client) -> str:
    """Which provider an already-built client speaks to."""
    name = type(client).__name__
    if name.startswith("OpenAI"):
        return "openai"
    if name.startswith("Anthropic"):
        return "anthropic"
    return "ollama"


def _set_live_provider_key(agent: Agent, service: str, secret: str | None) -> bool:
    """Update or clear credentials on the active cloud client immediately."""
    if _client_provider(agent.client) != service:
        return False
    value = secret or ""
    setattr(agent.client, "api_key", value)
    session = getattr(agent.client, "_session", None)
    headers = getattr(session, "headers", None)
    if headers is None:
        return True
    header = "Authorization" if service == "openai" else "x-api-key"
    if secret:
        headers[header] = f"Bearer {secret}" if service == "openai" else secret
    else:
        headers.pop(header, None)
    return True


_CREDENTIAL_KEYS = {
    "github": "github.token",
    "openai": "providers.openai.api_key",
    "anthropic": "providers.anthropic.api_key",
}
_CREDENTIAL_ENV = {
    "github": ("GITHUB_TOKEN", "GH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
}


def _warn_credential_environment(service: str, *, login: bool) -> None:
    active = _credential_environment(service)
    if not active:
        return
    joined = " and ".join(active)
    if login:
        ui.warn(f"{joined} is set and takes precedence over the saved key on new clients.")
    else:
        ui.warn(f"{joined} is still set in the environment and remains active.")


def _credential_environment(service: str) -> list[str]:
    return [name for name in _CREDENTIAL_ENV[service] if os.environ.get(name)]


def _credential_mode(
    cfg: dict,
    service: str,
    *,
    login: bool,
    secret: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
) -> int:
    """Persist or preview one credential change without starting a provider."""
    if login and not (secret or "").strip():
        message = "credential was empty; no settings were changed"
        if as_json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            ui.error(message)
        return 1

    working = copy.deepcopy(cfg)
    config.set_value(
        working, _CREDENTIAL_KEYS[service], secret.strip() if login and secret else None
    )
    action = "save" if login else "remove"
    environment = _credential_environment(service)
    payload: dict[str, Any] = {
        "ok": True,
        "action": "login" if login else "logout",
        "service": service,
        "dry_run": dry_run,
        "config_key": _CREDENTIAL_KEYS[service],
        "environment_overrides": environment,
    }

    if dry_run:
        if as_json:
            print(json.dumps(payload, indent=2))
        else:
            ui.info(
                f"dry run: would {action} the {service} key at {_CREDENTIAL_KEYS[service]}"
                " · no settings changed"
            )
        return 0

    try:
        config.save(working)
    except (OSError, TypeError, ValueError) as exc:
        message = f"could not {action} {service} key: {exc}; no settings were changed"
        if as_json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            ui.error(message)
        return 1
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        ui.info(f"{service} key {'saved' if login else 'removed'}.")
        _warn_credential_environment(service, login=login)
    return 0


def _configured_port(cfg: dict, section: str, default: int) -> int:
    section_cfg = cfg.get(section)
    if section_cfg is not None and not isinstance(section_cfg, dict):
        ui.warn(f"ignoring invalid {section} config; using port {default}")
        return default
    raw = (section_cfg or {}).get("port", default)
    try:
        port = 0 if isinstance(raw, bool) else int(raw)
    except (TypeError, ValueError):
        port = 0
    if 1 <= port <= 65535:
        return port
    ui.warn(f"ignoring invalid {section}.port={raw!r}; using {default}")
    return default


def _configured_bool(cfg: dict, key: str, default: bool) -> bool:
    raw = config.get_value(cfg, key, default)
    if isinstance(raw, bool):
        return raw
    ui.warn(f"ignoring invalid {key}={raw!r}; using {str(default).lower()}")
    return default


def _tools_supported(cfg: dict, model_spec: str) -> bool:
    raw = config.get_model_capability(cfg, model_spec, "tools_supported", True)
    if isinstance(raw, bool):
        return raw
    ui.warn(f"ignoring invalid tools_supported metadata for {model_spec!r}; enabling tools")
    return True


def _remember_tools_unsupported(cfg: dict, model_spec: str) -> None:
    config.set_model_capability(cfg, model_spec, "tools_supported", False)


def _redact(cfg: dict) -> dict:
    # Delegate to the single source of truth so every secret-bearing key
    # (github token, provider api_keys, smtp/email password, mcp env secrets)
    # is masked consistently — the gateway reuses config.redact_secrets too.
    return config.redact_secrets(cfg)


def _report_cost(agent: Agent) -> None:
    """`/cost` — session token spend and, where the rate is known, dollars.

    Deliberately says "no published rate" rather than printing $0.00 for an
    unpriced model: a fabricated zero is worse than an honest gap, and the
    override hint turns the gap into something the user can close.
    """
    from .fmt import fmt_cost, fmt_tokens

    usage = dict(agent.engine._usage)
    if not any(usage.values()):
        ui.info("no model calls yet this session")
        return

    report = agent.engine.cost_report(usage)
    spec = report.get("model") or agent.model
    ui.info(f"session usage · {spec}")
    ui.list_item("input", detail=f"{int(usage.get('input', 0)):,} tokens")
    ui.list_item("output", detail=f"{int(usage.get('output', 0)):,} tokens")
    cache_read = int(usage.get("cache_read", 0) or 0)
    cache_write = int(usage.get("cache_write", 0) or 0)
    if cache_read or cache_write:
        ui.list_item(
            "cached",
            detail=f"{cache_read:,} read · {cache_write:,} written",
        )

    spent = report.get("spent")
    if spent is None:
        ui.list_item("cost", detail="no published rate for this model")
        ui.meta(f"add one with:  /set models.{spec}.pricing <in_per_1M>,<out_per_1M>")
        return
    ui.list_item("cost", detail=fmt_cost(spent))
    saved = report.get("saved") or 0.0
    if saved >= 0.005:
        ui.list_item("saved by caching", detail=fmt_cost(saved))
    total_tokens = sum(int(usage.get(k, 0) or 0) for k in ("input", "output", "cache_read"))
    if total_tokens:
        ui.meta(f"{fmt_tokens(total_tokens)} tokens billed across the session")


def _retry_snapshot(agent: Agent) -> list[dict]:
    """Copy conversation state without freezing the live system prompt."""
    messages = agent.messages
    start = 1 if messages and messages[0].get("role") == "system" else 0
    return copy.deepcopy(messages[start:])


def _apply_to_agent(agent: Agent, cfg: dict) -> None:
    # Go through state.update() so subscribers (autosave, tool-support
    # detection) actually see the change — direct assignment skips them.
    agent.state.update(
        github_token=config.get_value(cfg, "github.token"),
        yolo=_configured_bool(cfg, "yolo", agent.state.yolo),
        insecure_ssl=_configured_bool(cfg, "insecure_ssl", False),
    )


def _activate_model(agent: Agent, cfg: dict, model_spec: str) -> None:
    """Switch both the provider client and model, preserving the full spec."""
    model_spec = str(model_spec or "").strip()
    provider, model_name = _parse_model_provider(model_spec)
    if not model_name:
        raise RuntimeError("model name is required")
    new_client = _build_client(cfg, provider)
    agent.client = new_client
    agent.engine.client = new_client
    agent.model = model_name
    agent.state.update(active_model_spec=model_spec if provider != "ollama" else model_name)


def _autosave(session: dict, agent: Agent) -> None:
    session["model"] = agent.state.active_model_spec or agent.model
    session["messages"] = [
        m
        for m in agent.messages
        if m.get("role") != "system" or SUMMARY_MARKER in str(m.get("content") or "")
    ]
    sessions.save(session)


def _print_sessions(active_id: str | None = None) -> list[dict]:
    listed = sessions.list_all()
    if not listed:
        ui.info("(no saved conversations)")
        return []
    print()
    ui.heading("Conversations")
    print()
    for i, s in enumerate(listed, 1):
        active = s["id"] == active_id
        marker = ui.color("●", ui.GLOW) if active else ui.color("·", ui.SOFT)
        prefix = f"  {i:>2} {marker} "
        title = ui.single_line(s.get("title") or "untitled")
        print(prefix + ui.color(ui.truncate(title, max(1, ui.width() - 7)), ui.SURFACE))
        turns = int(s.get("turns") or 0)
        metadata = ui.sanitize(
            f"{s.get('id', '')} · {sessions.fmt_time(s.get('updated_at') or 0)} · "
            f"{turns} {'turn' if turns == 1 else 'turns'} · {s.get('model', '?')}"
        )
        for line in textwrap.wrap(
            metadata,
            width=max(1, ui.width() - 7),
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]:
            print("       " + ui.color(line, ui.MUTED))
    return listed


def _resolve_session_arg(arg: str, listed: list[dict]) -> dict | None:
    if not arg:
        return None
    exact = [s for s in listed if s.get("id") == arg]
    if exact:
        return exact[0]
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(listed):
            return listed[idx]
        return None
    matches = [s for s in listed if str(s.get("id", "")).startswith(arg)]
    return matches[0] if len(matches) == 1 else None


def _session_miss(arg: str, listed: list[dict]) -> str:
    matches = [s for s in listed if str(s.get("id", "")).startswith(arg)] if arg else []
    if len(matches) > 1:
        ids = ", ".join(str(s.get("id", "")) for s in matches[:5])
        return f"'{arg}' matches {len(matches)} sessions ({ids}) — use more of the id"
    return f"no session matching '{arg}'"


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
            if content.startswith(
                (
                    "Tool result for ",
                    "[background] ",
                    "STOP. You have called",
                    "STOP. Tool outputs",
                )
            ):
                continue
            prefix = ui.color("  › ", ui.GLOW)
            content = ui.sanitize(content)
            for index, line in enumerate(
                textwrap.wrap(content, width=max(1, ui.width() - 4)) or [""]
            ):
                print((prefix if index == 0 else "    ") + ui.color(line, ui.SURFACE))
        elif role == "assistant":
            if content:
                ui.assistant(content)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                ui.tool_call(fn.get("name", "?"), "")
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
        ui.warn(
            f"a heads-up — {len(overdue)} reminder"
            f"{'s are' if len(overdue) != 1 else ' is'} overdue:"
        )
        for r in overdue[:5]:
            ui.list_item(r.short().strip(), marker="!")
        if len(overdue) > 5:
            ui.meta(f"{len(overdue) - 5} more · use /remind to see all")


def repl(
    agent: Agent,
    cfg: dict,
    gateway_holder: dict | None = None,
    resumed: dict | None = None,
) -> int:
    gateway_holder = gateway_holder if gateway_holder is not None else {"server": None}

    def _live_gateway():
        gateway = gateway_holder.get("server")
        return gateway if gateway is not None and gateway.running else None

    def _refresh_live_prompts() -> None:
        agent.engine.refresh_system_prompt()
        gateway = _live_gateway()
        if gateway is not None and gateway.engine is not agent.engine:
            gateway.engine.refresh_system_prompt()

    def _apply_live_setting(key: str, value) -> bool:
        gateway = _live_gateway()
        if gateway is not None:
            return gateway._apply_shared_runtime_setting(key, value)
        return command_utils.apply_runtime_setting(agent.state, agent.engine, key, value)

    def _activate_live_model(model_spec: str) -> None:
        gateway = _live_gateway()
        if gateway is None:
            _activate_model(agent, cfg, model_spec)
            return
        error = gateway._activate_model(model_spec)
        if error:
            raise RuntimeError(error)

    def _persist_config() -> bool:
        if agent.state.dry_run:
            ui.warn("dry run: configuration was not saved")
            return False
        try:
            config.save(cfg)
            return True
        except (OSError, TypeError, ValueError) as exc:
            ui.error(f"could not save config: {exc}; any live change lasts only until exit")
            return False

    def _persist_session() -> bool:
        if agent.state.dry_run:
            return True
        try:
            _autosave(session, agent)
            return True
        except Exception as exc:
            logger.warning("slash command could not save session", exc_info=True)
            ui.error(
                f"could not finish saving conversation: {exc}; "
                "the session file and index may differ, so reload before retrying"
            )
            return False

    ui.banner(
        agent.model,
        str(agent.state.workspace),
        tools_enabled=agent.tools_enabled,
        user_name=agent.state.user_name,
        version=__version__,
        plan_mode=agent.state.plan_mode,
        yolo=agent.state.yolo,
        dry_run=agent.state.dry_run,
    )

    # A resumed conversation keeps its own record, so autosave appends to it
    # rather than forking a second session with the same content.
    session = resumed or sessions.make(agent.state.active_model_spec or agent.model)

    def _on_turn(a):
        if not a.state.dry_run:
            _autosave(session, a)

    agent.on_turn_complete = _on_turn
    agent.engine.session_id = session["id"]

    if not agent.state.dry_run:
        _settle_in(agent)

    prompt = Prompt(persist_history=False) if agent.state.dry_run else Prompt()
    set_workspace_provider = getattr(prompt, "set_workspace_provider", None)
    if callable(set_workspace_provider):
        set_workspace_provider(lambda: agent.state.workspace)
    set_context_provider = getattr(prompt, "set_context_provider", None)
    if callable(set_context_provider):
        set_context_provider(
            lambda: {
                "model": agent.state.active_model_spec or agent.model,
                "workspace": agent.state.workspace,
                "mode": (
                    "dry run" if agent.state.dry_run else "plan" if agent.state.plan_mode else "act"
                ),
                "approval": (
                    "auto approve"
                    if agent.state.yolo
                    else "accept edits"
                    if agent.state.approval_mode == "accept_edits"
                    else "ask changes"
                ),
                "tools": "tools on" if agent.tools_enabled else "tools off",
                # Cached for a few seconds inside branch_label — the toolbar
                # repaints per keystroke and must not shell out to git that often.
                "branch": _branch_label(agent.state.workspace),
            }
        )
    if prompt.status_note:
        ui.warn(prompt.status_note)
    if not agent.engine.stream:
        ui.warn("streaming is OFF — use /stream on to see tokens live.")
    if agent.state.dry_run:
        ui.warn("dry run is ON — mutating tools and persistent slash commands are blocked")

    last_user_input = ""
    retry_messages: list[dict] | None = None
    # Type-ahead: lets the user compose while the model streams. Constructed
    # once and reused; a no-op when the terminal can't support it.
    from .typeahead import TypeAhead

    typeahead = TypeAhead()
    queued: str | None = None
    # A line the user was mid-typing when a turn ended. Held across any
    # type-ahead-queued turns that run first, and cleared once a prompt has
    # actually pre-filled with it.
    carried: str = ""
    while True:
        if queued is not None:
            # Submitted mid-turn via type-ahead — run it straight away rather
            # than throwing away what the user already typed and re-prompting.
            line, queued = queued.strip(), None
            if not line:
                continue
            ui.prepare_for_input()
            print()
            print(ui.prompt_prefix() + line)
        else:
            ui.prepare_for_input()
            print()
            try:
                line = prompt.ask(ui.prompt_prefix(), default=carried).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            finally:
                # Consumed either way: on Ctrl-C the user is discarding it, and
                # re-offering it at the next prompt would be a resurrection.
                carried = ""
            if not line:
                continue

        if line.startswith("/"):
            parts = line.split(maxsplit=2)
            cmd = parts[0][1:].lower()
            arg1 = parts[1] if len(parts) > 1 else ""
            arg2 = parts[2] if len(parts) > 2 else ""
            full_arg = command_utils.full_argument(arg1, arg2)

            # Project commands (.cagentic/commands/*.md) expand into a prompt
            # and run as an ordinary turn. Looked up per invocation rather than
            # cached at start-up so adding a command file takes effect
            # immediately, and checked AFTER the built-ins so a project can't
            # shadow /quit or /yolo.
            if cmd not in _BUILTIN_COMMAND_NAMES:
                from .project_scope import discover_commands, render_command

                project_commands = discover_commands(agent.state.workspace)
                if cmd in project_commands:
                    rendered = render_command(project_commands[cmd], full_arg)
                    if not rendered:
                        ui.warn(f"/{cmd} is empty — nothing to run")
                        continue
                    ui.meta(f"running project command /{cmd}")
                    line = rendered
                    # Fall through to the normal turn path below.
                    if not (line.startswith("/") and line.split(maxsplit=1)[0].lower() == "/retry"):
                        retry_messages = _retry_snapshot(agent)
                    last_user_input = line
                    try:
                        agent.turn(line, typeahead=typeahead)
                    except KeyboardInterrupt:
                        ui.warn("interrupted")
                        continue
                    queued = agent.pending_input
                    carried = agent.pending_partial or carried
                    continue

            no_argument_commands = {
                "browser",
                "clear",
                "compact",
                "config",
                "context",
                "cost",
                "diag",
                "help",
                "models",
                "notes",
                "quit",
                "retry",
                "sessions",
                "tools",
                "undo",
                "whoami",
            }
            if cmd in no_argument_commands and full_arg:
                ui.warn(f"usage: /{cmd}")
                continue
            single_argument_usage = {
                "delete": "/delete <id|number>",
                "diff": "/diff [N]",
                "effort": "/effort [low|medium|high]",
                "host": "/host [url]",
                "logout": "/logout github|openai|anthropic",
                "plan": "/plan [on|off]",
                "resume": "/resume [id|number]",
                "stream": "/stream [on|off]",
                "yolo": "/yolo [on|off]",
            }
            if cmd in single_argument_usage and arg2:
                ui.warn(f"usage: {single_argument_usage[cmd]}")
                continue

            dry_run_mutation = (
                cmd
                in {
                    "browser",
                    "clear",
                    "compact",
                    "delete",
                    "gateway",
                    "login",
                    "logout",
                    "new",
                    "rename",
                    "save",
                    "set",
                    "undo",
                    "yolo",
                }
                or (cmd == "groups" and bool(arg1))
                or (
                    cmd in {"remind", "reminders"}
                    and arg1.lower() in {"add", "done", "delete", "clear"}
                )
                or (cmd in {"effort", "host", "model", "name", "stream"} and bool(full_arg))
            )
            if agent.state.dry_run and dry_run_mutation:
                ui.warn(f"dry run: would execute {line!r} · no changes made")
                continue

            if cmd == "quit":
                return 0
            if cmd == "init":
                from .project_scope import PROJECT_DIR

                workspace = agent.state.workspace
                target = workspace / "AGENTS.md"
                if target.exists() and arg1.lower() != "force":
                    ui.warn(f"{target} already exists — /init force overwrites it")
                    ui.meta("it's already loaded into every turn; edit it directly instead")
                    continue
                if agent.state.dry_run:
                    ui.warn("dry run is ON — /init would write but is blocked")
                    continue
                facts = _project_facts(workspace)
                ui.info("asking the model to describe this project…")
                # The model writes it, because a template full of blanks is
                # worse than nothing — but ground it in what's actually on disk
                # so it describes this repo rather than a generic one.
                draft = agent.turn(
                    "Write the contents of an AGENTS.md for this project. It is loaded "
                    "into your context at the start of every future session, so include "
                    "only what a competent newcomer could not work out quickly: how to "
                    "run and test it, the conventions that aren't obvious from the code, "
                    "and any traps. Be concise and concrete — no filler, no restating "
                    "the directory listing. Reply with the file contents only, no "
                    "commentary and no code fence.\n\n"
                    f"What's on disk:\n{facts}",
                    typeahead=typeahead,
                )
                body = (draft or "").strip()
                if body.startswith("```"):
                    body = body.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                if not body:
                    ui.error("the model returned nothing; AGENTS.md was not written")
                    continue
                try:
                    from .tools import _write_text_raw

                    _write_text_raw(target, body + "\n")
                except OSError as exc:
                    ui.error(f"could not write {target}: {exc}")
                    continue
                ui.info(f"wrote {target} ({len(body)} chars)")
                ui.meta("it is read back into every turn — edit it freely")
                ui.meta(f"project settings/commands/skills live in {workspace / PROJECT_DIR}/")
                continue
            if cmd == "help":
                print_help(agent.state.workspace)
                continue
            if cmd == "tools":
                print_tools(agent)
                continue
            if cmd == "groups":
                from .tools import DEFAULT_GROUPS, TOOL_GROUPS

                active = (
                    agent.state.tool_groups
                    if agent.state.tool_groups is not None
                    else DEFAULT_GROUPS
                )
                if not arg1:
                    print()
                    ui.heading("Tool groups")
                    print()
                    for g, names in TOOL_GROUPS.items():
                        ui.list_item(
                            g,
                            detail=f"{len(names)} tools · {'enabled' if g in active else 'disabled'}",
                            marker="✓" if g in active else "·",
                            active=g in active,
                        )
                    continue
                action = arg1.lower()
                group = arg2.lower()
                if action in ("enable", "disable") and group:
                    if group not in TOOL_GROUPS:
                        ui.warn(f"unknown group '{arg2}' — see /groups")
                        continue
                    groups = set(active)
                    if action == "enable":
                        groups.add(group)
                    else:
                        groups.discard(group)
                    agent.state.update(tool_groups=groups)
                    _refresh_live_prompts()
                    config.set_value(cfg, "tool_groups", sorted(groups))
                    if not _persist_config():
                        continue
                    verb = "enabled" if action == "enable" else "disabled"
                    ui.info(f"{verb} '{group}' — {len(groups)} group(s) active")
                else:
                    ui.warn("usage: /groups  |  /groups enable <name>  |  /groups disable <name>")
                continue
            if cmd == "cd":
                if not full_arg:
                    ui.info(f"workspace: {agent.state.workspace}")
                    continue
                raw = os.path.expanduser(os.path.expandvars(full_arg))
                workspace_target = Path(raw)
                if not workspace_target.is_absolute():
                    workspace_target = agent.state.workspace / workspace_target
                workspace_target = workspace_target.resolve()
                if not workspace_target.is_dir():
                    ui.error(f"not a directory: {workspace_target}")
                    continue
                agent.state.update(workspace=workspace_target)
                _refresh_live_prompts()
                ui.info(f"workspace → {workspace_target}")
                continue

            # ---- notes ----
            if cmd == "notes":
                items = _notes.list_all()
                if not items:
                    ui.info("(no notes yet — ask Cagentic to remember something)")
                else:
                    print()
                    ui.heading("Notes")
                    print()
                    for note in items[:40]:
                        ui.list_item(note.short())
                    if len(items) > 40:
                        ui.meta(f"{len(items) - 40} more notes")
                continue
            if cmd == "note":
                if not full_arg:
                    ui.warn("usage: /note <name>")
                    continue
                selected_note = _notes.get(full_arg)
                if not selected_note:
                    ui.warn(f"no note named '{full_arg}'")
                else:
                    print()
                    ui.panel(selected_note.body, title=selected_note.name, markdown=True)
                continue

            # ---- reminders ----
            if cmd in ("remind", "reminders"):
                reminder_action = arg1.lower()
                if reminder_action == "add":
                    if not arg2:
                        ui.warn(
                            "usage: /remind add <text>  (or include time: 'call mom @ tomorrow')"
                        )
                        continue
                    # Crude '@ when' splitter
                    text, when = arg2, None
                    if " @ " in arg2:
                        text, when = arg2.rsplit(" @ ", 1)
                    if not text.strip():
                        ui.warn("reminder text cannot be empty")
                        continue
                    due = _reminders.parse_when(when) if when else None
                    if when and due is None:
                        ui.warn(
                            f"couldn't understand due time {when!r}; "
                            "nothing was saved — try 'tomorrow', 'in 2h', or YYYY-MM-DD"
                        )
                        continue
                    added_reminder = _reminders.add(text.strip(), due_at=due)
                    ui.info(f"added: {added_reminder.short().strip()}")
                    continue
                if reminder_action == "done":
                    if not arg2:
                        ui.warn("usage: /remind done <id>")
                        continue
                    updated_reminder = _reminders.update(arg2, status="done")
                    if updated_reminder:
                        ui.info(f"marked done: {updated_reminder.short().strip()}")
                    else:
                        ui.warn(_reminder_miss(arg2))
                    continue
                if reminder_action == "delete":
                    if not arg2:
                        ui.warn("usage: /remind delete <id>")
                        continue
                    if _reminders.delete(arg2):
                        ui.info("deleted")
                    else:
                        ui.warn(_reminder_miss(arg2))
                    continue
                if reminder_action == "clear":
                    # Don't actually delete — just mark all done. Safer.
                    count = 0
                    for pending_reminder in _reminders.list_all():
                        _reminders.update(pending_reminder.id, status="done")
                        count += 1
                    ui.info(f"marked {count} reminder(s) done")
                    continue
                if reminder_action not in ("", "all"):
                    ui.warn(
                        "usage: /remind [all]  |  /remind add <text>  |  "
                        "/remind done|delete <id>  |  /remind clear"
                    )
                    continue
                # bare /remind — list
                rems = _reminders.list_all(include_done=(reminder_action == "all"))
                if not rems:
                    ui.info("(no reminders)")
                else:
                    print()
                    ui.heading("Reminders")
                    print()
                    for listed_reminder in rems[:40]:
                        ui.list_item(
                            listed_reminder.short().strip(),
                            marker="✓" if listed_reminder.status == "done" else "•",
                            active=listed_reminder.status != "done",
                        )
                    if len(rems) > 40:
                        ui.meta(f"{len(rems) - 40} more reminders")
                continue

            # ---- mcp ----
            if cmd == "mcp":
                # Lazy-init the MCP manager on the state
                from .mcp_client import MCPManager

                if agent.state.mcp is None:
                    agent.state.update(mcp=MCPManager(cfg))
                mgr = cast(MCPManager, agent.state.mcp)
                if not full_arg:
                    names = mgr.names()
                    if not names:
                        ui.info("no MCP servers configured.")
                        ui.info(
                            "add one under mcp.servers in ~/.config/cagentic/config.json, e.g.:"
                        )
                        ui.code_block(
                            "{\n"
                            '  "mcp": {\n'
                            '    "servers": {\n'
                            '      "notion": {\n'
                            '        "command": ["npx", "-y", "@notionhq/notion-mcp-server"],\n'
                            '        "env": {"NOTION_TOKEN": "secret_xxx"},\n'
                            '        "enabled": true\n'
                            "      }\n"
                            "    }\n"
                            "  }\n"
                            "}"
                        )
                    else:
                        print()
                        ui.heading("MCP servers")
                        print()
                        for n in names:
                            ui.list_item(n)
                        ui.meta(f"{len(names)} configured · use /mcp <server> to inspect tools")
                else:
                    try:
                        tools = mgr.list_tools(full_arg)
                    except Exception as e:
                        ui.error(str(e))
                        continue
                    if not tools:
                        ui.info(f"(server '{full_arg}' exposes no tools)")
                    else:
                        print()
                        ui.heading(full_arg)
                        print()
                        for t in tools[:40]:
                            n = t.get("name", "?")
                            d = (t.get("description") or "").splitlines()[0][:140]
                            ui.list_item(n, detail=d)
                        if len(tools) > 40:
                            ui.meta(f"{len(tools) - 40} more tools")
                continue
            if cmd == "browser":
                from .browser import BrowserBridge

                if agent.state.browser is None:
                    port = _configured_port(cfg, "browser", 8765)
                    b = BrowserBridge(port=port, site_rules=config.get_value(cfg, "browser.sites"))
                    if b.start():
                        agent.state.update(browser=b)
                    else:
                        ui.error(f"browser bridge couldn't start: {b.error}")
                        continue
                b = cast(BrowserBridge, agent.state.browser)
                ext_dir = Path(__file__).resolve().parent.parent / "extension"
                if b.error:
                    ui.error(f"browser bridge couldn't start: {b.error}")
                elif b.is_connected():
                    ui.info(f"Chrome extension is connected — bridge on port {b.port}.")
                    ui.info("Cagentic can read pages, open tabs, click, and fill forms.")
                elif b.auth_failing():
                    # The extension IS installed and polling — it just has the
                    # wrong secret. Showing the install steps here (which is what
                    # this used to do) sends the user to fix a problem they don't
                    # have, while the real one keeps failing silently in the log.
                    from .browser import _token_path

                    ui.warn(
                        "the Chrome extension is running but its bridge token is "
                        "wrong — every request is being rejected."
                    )
                    print()
                    ui.heading("Re-pair the extension")
                    print()
                    ui.list_item("Click the Cagentic icon in Chrome's toolbar", marker="1")
                    ui.list_item("Paste this token into 'Bridge token', then Save", marker="2")
                    print()
                    ui.code_block(b.token or "(token unavailable)")
                    ui.field("also stored at", _token_path())
                    ui.field("bridge port", str(b.port))
                else:
                    ui.warn(
                        f"bridge running on port {b.port}, but the Chrome extension "
                        f"isn't connected yet."
                    )
                    print()
                    ui.heading("Connect Chrome")
                    print()
                    ui.list_item("Open chrome://extensions", marker="1")
                    ui.list_item("Turn on Developer mode", marker="2")
                    ui.list_item("Choose Load unpacked", detail=ext_dir, marker="3")
                    ui.list_item(
                        "Wait for the extension to connect, then run /browser again",
                        marker="4",
                    )
                continue
            if cmd == "gateway":
                from .gateway import Gateway

                gw = gateway_holder.get("server")
                gateway_action = arg1.lower()
                if arg2:
                    ui.warn("usage: /gateway [on|off]")
                    continue
                if gateway_action in ("off", "stop"):
                    if gw is None or not gw.running:
                        ui.info("the gateway isn't running.")
                    else:
                        gw.stop()
                        gateway_holder["server"] = None
                        ui.info("gateway stopped.")
                    continue
                if gateway_action not in ("", "on", "start"):
                    ui.warn("usage: /gateway [on|off]")
                    continue
                if gw is not None and gw.running:
                    ui.info(f"gateway is already live at {gw.url()}")
                    continue
                port = _configured_port(cfg, "gateway", 8700)
                gw = Gateway(agent, cfg, port=port)
                if gw.start():
                    gateway_holder["server"] = gw
                    if gw.start_notice:
                        ui.warn(gw.start_notice)
                    ui.info(f"gateway is live — open {gw.url()} in your browser.")
                    ui.info(
                        "it's the full assistant on the web; tool approvals pop "
                        "up right in the page. /gateway off to stop it."
                    )
                else:
                    ui.error(f"gateway couldn't start: {gw.error}")
                continue

            if cmd == "plan":
                plan_enabled = command_utils.switch_value(arg1, agent.state.plan_mode)
                if plan_enabled is None:
                    ui.warn("usage: /plan [on|off]")
                    continue
                agent.state.update(plan_mode=plan_enabled)
                _refresh_live_prompts()
                ui.info(f"plan mode: {'ON (read-only)' if agent.state.plan_mode else 'off'}")
                continue
            if cmd == "effort":
                from .engine import EFFORT_LEVELS

                if not arg1:
                    ui.info(
                        f"effort: {getattr(agent.state, 'effort', 'medium')}  "
                        f"(usage: /effort [{'|'.join(EFFORT_LEVELS)}])"
                    )
                    continue
                level = arg1.lower()
                if level not in EFFORT_LEVELS:
                    ui.warn(f"usage: /effort [{'|'.join(EFFORT_LEVELS)}]")
                    continue
                agent.state.update(effort=level)
                _refresh_live_prompts()
                cfg["effort"] = level
                if not _persist_config():
                    continue
                ui.info(f"effort: {level}")
                continue
            if cmd == "todo":
                todos = list(agent.state.todos or [])
                todo_action = arg1.lower()
                if not todo_action:
                    if not todos:
                        ui.info("(no todos)")
                    else:
                        print()
                        ui.heading("Todo")
                        print()
                        for i, t in enumerate(todos, 1):
                            status = t.get("status", "pending")
                            mark = {
                                "done": "✓",
                                "pending": "•",
                                "active": "→",
                                "blocked": "×",
                            }.get(status, "?")
                            ui.list_item(
                                f"{i}. {t.get('text', '')}",
                                detail=status,
                                marker=mark,
                                active=status != "done",
                            )
                    continue
                if todo_action == "add":
                    text = arg2.strip()
                    if not text:
                        ui.warn("usage: /todo add <text>")
                        continue
                    todos.append({"text": text, "status": "pending"})
                    agent.state.update(todos=todos)
                    ui.info(f"added: {text}")
                    continue
                if todo_action == "done" and arg2.isdigit():
                    i = int(arg2) - 1
                    if 0 <= i < len(todos):
                        todos[i]["status"] = "done"
                        agent.state.update(todos=todos)
                        ui.info(f"done: {todos[i]['text']}")
                    else:
                        ui.warn(f"no todo {arg2}; choose a number from /todo")
                    continue
                if todo_action == "clear":
                    agent.state.update(todos=[])
                    ui.info("cleared todos")
                    continue
                ui.warn("usage: /todo  |  /todo add <text>  |  /todo done <n>  |  /todo clear")
                continue
            if cmd == "diag":
                from .tools import DEFAULT_GROUPS

                groups = (
                    agent.state.tool_groups
                    if agent.state.tool_groups is not None
                    else DEFAULT_GROUPS
                )

                def _row(label: str, value: str, warn: bool = False) -> None:
                    ui.field(label, value, warning=warn)

                print()
                ui.heading("Diagnostics")
                print()
                _row("model", agent.model)
                _row("name", agent.state.user_name or "(not set — /name <your name>)")
                _row("workspace", str(agent.state.workspace))
                _row("home", str(Path.home()))
                _row("tools", "native" if agent.tools_enabled else "text-protocol fallback")
                _row("groups", ", ".join(sorted(groups)))
                _row("stream", "on" if agent.engine.stream else "off")
                if isinstance(agent.client, OllamaClient):
                    _row("host", agent.client.host)
                    _row("num_ctx", str(agent.client.num_ctx))
                    memory_error = ""
                    try:
                        status = agent.client.model_vram_status(agent.model)
                    except Exception as exc:
                        logger.warning("could not read model memory status", exc_info=True)
                        status = None
                        memory_error = type(exc).__name__
                    mac = _is_apple_silicon()
                    label = "memory" if mac else "vram"
                    if memory_error:
                        _row(label, f"unavailable ({memory_error})", warn=True)
                    elif status is None:
                        _row(label, "model not currently loaded")
                    elif status["fully_gpu"]:
                        place = "in Metal buffer (unified)" if mac else "fully on GPU ✓"
                        _row(label, f"{status['size_vram'] / (1024**3):.1f} GB · {place}")
                    else:
                        size_gb = status["size"] / (1024**3)
                        cpu_gb = status["cpu_bytes"] / (1024**3)
                        pct = status["cpu_percent"]
                        _row(
                            label,
                            f"{cpu_gb:.1f}/{size_gb:.1f} GB on CPU ({pct:.0f}% offloaded — slow)",
                            warn=True,
                        )
                else:
                    _row(
                        "provider", f"{_client_provider(agent.client)} (cloud — no local VRAM info)"
                    )
                mcp_servers = list(command_utils.mcp_server_config(cfg))
                _row("mcp", f"{len(mcp_servers)} configured ({', '.join(mcp_servers) or 'none'})")
                notes_n = len(_notes.list_all())
                rems_n = len(_reminders.list_all())
                _row("data", f"{notes_n} notes · {rems_n} active reminders")
                _row("github", "logged in" if agent.state.github_token else "no token")
                from . import sandbox as _sandbox

                if str(config.get_value(cfg, "shell.sandbox", "auto")).lower() == "off":
                    _row("shell", "UNCONFINED (shell.sandbox=off)")
                else:
                    _row(
                        "shell",
                        f"{_sandbox.describe()} · network "
                        f"{config.get_value(cfg, 'shell.network', 'deny')}",
                    )
                _row("input", prompt.backend)
                gw = gateway_holder.get("server")
                _row("gateway", gw.url() if gw is not None and gw.running else "off")
                print()
                continue
            if cmd == "stream":
                stream_enabled = command_utils.switch_value(arg1, agent.engine.stream)
                if stream_enabled is None:
                    ui.warn("usage: /stream [on|off]")
                    continue
                _apply_live_setting("ollama.stream", stream_enabled)
                config.set_value(cfg, "ollama.stream", agent.engine.stream)
                if not _persist_config():
                    continue
                ui.info(f"streaming: {'on' if agent.engine.stream else 'off'} (saved)")
                continue
            if cmd == "model":
                if not full_arg:
                    ui.info(f"current model: {agent.state.active_model_spec or agent.model}")
                    ui.meta("use /models, then /model <number|unique words>")
                else:
                    try:
                        available_models = agent.client.list_models()
                    except Exception as exc:
                        logger.warning(
                            "could not list models while resolving /model", exc_info=True
                        )
                        available_models = []
                        ui.warn(f"could not look up short model names: {exc}")

                    resolved, matches = _resolve_model_selector(full_arg, available_models)
                    if resolved is None and matches:
                        ui.warn(f"'{full_arg}' matches more than one model:")
                        for index, model_name in enumerate(available_models, 1):
                            if model_name in matches:
                                ui.list_item(f"{index}. {model_name}")
                        ui.meta("add another word, or use /model <number>")
                        continue
                    if resolved is None and available_models:
                        provider, _ = _parse_model_provider(full_arg)
                        if provider == "ollama":
                            ui.warn(f"no installed model matches '{full_arg}' — use /models")
                            continue
                    selected_model = resolved or full_arg
                    try:
                        _activate_live_model(selected_model)
                    except RuntimeError as _e:
                        ui.error(str(_e))
                        continue
                    cfg["model"] = selected_model  # save the canonical provider:model
                    if not _persist_config():
                        continue
                    supported = _tools_supported(cfg, agent.state.active_model_spec or agent.model)
                    agent.tools_enabled = bool(supported)
                    _refresh_live_prompts()
                    suffix = f" · matched '{full_arg}'" if selected_model != full_arg else ""
                    ui.info(f"switched to {selected_model} (saved){suffix}")
                continue
            if cmd == "models":
                try:
                    models = agent.client.list_models()
                    if not models:
                        ui.info("no models reported by the current provider")
                    else:
                        print()
                        ui.heading("Models")
                        print()
                        for index, m in enumerate(models, 1):
                            _, listed_name = _parse_model_provider(m)
                            current = listed_name == agent.model
                            ui.list_item(
                                f"{index}. {m}",
                                detail="current" if current else None,
                                marker="●" if current else "•",
                            )
                except OllamaError as e:
                    ui.error(str(e))
                continue
            if cmd == "host":
                # Only the Ollama client has a host; the cloud clients don't,
                # so reading/setting .host on them raised AttributeError (bare
                # /host) or silently set a dead attribute.
                if not isinstance(agent.client, OllamaClient):
                    ui.warn(
                        f"/host applies to Ollama only — currently on "
                        f"{_client_provider(agent.client)}. "
                        f"Switch back with /model <ollama-model> first."
                    )
                elif not arg1:
                    ui.info(f"current host: {agent.client.host}")
                else:
                    # The client's host setter normalizes (adds scheme/port,
                    # rewrites bind-all 0.0.0.0 to a routable loopback).
                    try:
                        agent.client.host = arg1
                    except (TypeError, ValueError) as exc:
                        ui.error(f"invalid Ollama host: {exc}")
                        continue
                    cfg["host"] = agent.client.host
                    if not _persist_config():
                        continue
                    if "0.0.0.0" in arg1 or arg1.strip() in ("::", "[::]", "0"):
                        ui.info(
                            f"{arg1!r} is a bind-all address — "
                            f"using {agent.client.host} (a client can't dial 0.0.0.0)."
                        )
                    else:
                        ui.info(f"host set to {agent.client.host}")
                continue
            if cmd == "config":
                import json as _json

                print()
                ui.heading("Configuration")
                print()
                ui.code_block(_json.dumps(_redact(cfg), indent=2))
                ui.meta(f"file · {config.config_path()}")
                continue
            if cmd == "set":
                if not arg1 or not arg2:
                    ui.warn("usage: /set <key> <value>")
                    continue
                key_error = command_utils.validate_config_key(arg1)
                if key_error:
                    ui.warn(key_error)
                    continue
                v = command_utils.parse_config_value(arg2)
                value_error = command_utils.validate_config_value(arg1, v)
                if value_error:
                    ui.warn(value_error)
                    continue
                if arg1 == "tool_groups" and v is not None:
                    from .tools import TOOL_GROUPS

                    unknown_groups = sorted(set(v) - set(TOOL_GROUPS))
                    if unknown_groups:
                        ui.warn(f"unknown tool group(s): {', '.join(unknown_groups)}")
                        continue
                config.set_value(cfg, arg1, v)
                if not _persist_config():
                    continue
                applied = _apply_live_setting(arg1, v)
                shown = "••••" if config.is_secret_key(arg1) else v
                ui.info(
                    f"set {arg1} = {shown}" + ("  → applied live" if applied else "  → config only")
                )
                continue
            if cmd == "name":
                if not arg1:
                    ui.info(f"I'm calling you: {agent.state.user_name or '(no name set)'}")
                    continue
                full = (arg1 + (" " + arg2 if arg2 else "")).strip()
                _apply_live_setting("user_name", full)
                config.set_value(cfg, "user_name", full)
                if not _persist_config():
                    continue
                ui.info(f"got it — I'll call you {full}.")
                continue
            if cmd == "login":
                svc = arg1.lower() if arg1 else ""
                if svc not in {"github", "openai", "anthropic"}:
                    ui.warn("usage: /login github|openai|anthropic")
                    continue
                if arg2:
                    ui.warn(
                        "for security, don't put keys on the command line; "
                        f"run /login {svc} and enter it at the hidden prompt"
                    )
                    continue
                try:
                    import getpass

                    secret = getpass.getpass(f"{svc} key: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    ui.warn("login cancelled")
                    continue
                if not secret:
                    ui.warn("login cancelled: key was empty")
                    continue
                if svc == "github":
                    config.set_value(cfg, "github.token", secret)
                    if not _persist_config():
                        continue
                    _apply_to_agent(agent, cfg)
                    ui.info("GitHub token saved.")
                elif svc == "openai":
                    config.set_value(cfg, "providers.openai.api_key", secret)
                    if not _persist_config():
                        continue
                    live = _set_live_provider_key(agent, svc, secret)
                    ui.info(
                        "OpenAI API key saved"
                        + (" and applied to the current model." if live else ".")
                        + " Use 'openai:<model>' to switch."
                    )
                else:
                    config.set_value(cfg, "providers.anthropic.api_key", secret)
                    if not _persist_config():
                        continue
                    live = _set_live_provider_key(agent, svc, secret)
                    ui.info(
                        "Anthropic API key saved"
                        + (" and applied to the current model." if live else ".")
                        + " Use 'anthropic:<model>' to switch."
                    )
                _warn_credential_environment(svc, login=True)
                continue
            if cmd == "logout":
                svc = arg1.lower() if arg1 else ""
                if svc == "github":
                    config.set_value(cfg, "github.token", None)
                    if not _persist_config():
                        continue
                    _apply_to_agent(agent, cfg)
                    ui.info("GitHub token removed.")
                elif svc == "openai":
                    config.set_value(cfg, "providers.openai.api_key", None)
                    if not _persist_config():
                        continue
                    live = _set_live_provider_key(agent, svc, None)
                    ui.info(
                        "OpenAI API key removed"
                        + ("; the current client is signed out." if live else ".")
                    )
                elif svc == "anthropic":
                    config.set_value(cfg, "providers.anthropic.api_key", None)
                    if not _persist_config():
                        continue
                    live = _set_live_provider_key(agent, svc, None)
                    ui.info(
                        "Anthropic API key removed"
                        + ("; the current client is signed out." if live else ".")
                    )
                else:
                    ui.warn("usage: /logout github|openai|anthropic")
                    continue
                _warn_credential_environment(svc, login=False)
                continue
            if cmd == "whoami":
                from .github import t_gh_whoami

                identity = t_gh_whoami({}, agent.ctx)
                if identity.startswith("ERROR:"):
                    ui.error(identity.removeprefix("ERROR:").strip())
                else:
                    print()
                    ui.heading("GitHub")
                    print()
                    ui.code_block(identity)
                continue
            if cmd == "clear":
                agent.reset()
                if not _persist_session():
                    continue
                last_user_input = ""
                retry_messages = None
                ui.info("conversation history cleared and saved")
                continue
            if cmd == "diff":
                hist = list(agent.state.edit_history or [])
                if not hist:
                    ui.info("(no edits this session)")
                    continue
                if arg1 and not arg1.isdigit():
                    ui.warn("usage: /diff [N]")
                    continue
                limit = int(arg1) if arg1 else len(hist)
                for entry in hist[-limit:]:
                    path = entry.get("path", "?")
                    op = entry.get("op", "edit")
                    before = entry.get("before", "")
                    after = entry.get("after", "")
                    adds, dels = _diff.stats(before, after)
                    line = ui.color(
                        f"  {ui.single_line(op)}  {ui.single_line(path)}  ", ui.DUSK
                    ) + ui.color(f"(+{adds} -{dels})", ui.MUTED)
                    print(ui.truncate(line, ui.width()))
                    rendered = _diff.render(before, after, path, max_lines=20)
                    if rendered:
                        print(rendered)
                continue
            if cmd == "undo":
                hist = list(agent.state.edit_history or [])
                if not hist:
                    ui.info("(no edits to undo)")
                    continue
                entry = hist[-1]
                reverted, problems = _revert_edits([entry])
                for problem in problems:
                    ui.error(f"undo refused: {problem}")
                if not reverted:
                    continue
                agent.state.update(edit_history=hist[:-1])
                ui.info(f"reverted {entry.get('op', 'edit')} on {Path(entry['path'])}")
                continue
            if cmd == "rewind":
                hist = list(agent.state.edit_history or [])
                turns = _turn_summary(agent, hist)
                if not arg1:
                    if not turns:
                        ui.info("(nothing to rewind — no turns in this session yet)")
                        continue
                    ui.heading("Rewind points")
                    print()
                    for number, prompt_text, edits in turns:
                        detail = f"{edits} edit{'s' if edits != 1 else ''}" if edits else "no edits"
                        ui.list_item(f"turn {number}: {prompt_text}", detail=detail, marker="·")
                    print()
                    ui.meta("/rewind <n> undoes turn n and everything after it")
                    continue
                try:
                    rewind_to = int(arg1)
                except ValueError:
                    ui.warn("usage: /rewind [n] — n is a turn number from /rewind")
                    continue
                if not any(number == rewind_to for number, _t, _e in turns):
                    ui.warn(f"no turn {rewind_to} in this session; run /rewind to list them")
                    continue
                # Newest first: a later edit to the same file must be undone
                # before an earlier one, or the earlier "before" text gets
                # clobbered by the later restore.
                doomed = [e for e in hist if int(e.get("turn", 0)) >= rewind_to]
                _reverted, problems = _revert_edits(list(reversed(doomed)))
                for problem in problems:
                    ui.error(f"rewind refused: {problem}")
                if problems:
                    ui.warn("no files were reverted; resolve the conflicts above and retry")
                    continue
                agent.state.update(
                    edit_history=[e for e in hist if int(e.get("turn", 0)) < rewind_to]
                )
                dropped = _truncate_to_turn(agent, rewind_to)
                ui.info(
                    f"rewound to before turn {rewind_to} — reverted {len(doomed)} file "
                    f"edit(s), dropped {dropped} message(s)"
                )
                if not _persist_session():
                    continue
                continue
            if cmd == "new":
                title = full_arg or None
                if agent.messages and len(agent.messages) > 1:
                    if not _persist_session():
                        continue
                    ui.info(f"saved {session['id']}")
                session.clear()
                session.update(
                    sessions.make(agent.state.active_model_spec or agent.model, title=title)
                )
                agent.reset()
                agent.state.update(todos=[])
                agent.on_turn_complete = _on_turn
                agent.engine.session_id = session["id"]
                last_user_input = ""
                retry_messages = None
                ui.info("fresh start — clean slate. (Notes and reminders are still with me.)")
                continue
            if cmd == "resume":
                if not arg1:
                    _print_sessions(active_id=session.get("id"))
                    continue
                listed = sessions.list_all()
                session_target = _resolve_session_arg(arg1, listed)
                if not session_target:
                    ui.warn(_session_miss(arg1, listed))
                    continue
                data = sessions.load(session_target["id"])
                if not data:
                    ui.error(f"could not load {session_target['id']}")
                    continue
                # Save the current chat while its own model is still active.
                # Switching first mislabeled the old chat with the resumed
                # chat's model during _autosave().
                if len(agent.messages) > 1:
                    if not _persist_session():
                        continue
                try:
                    _activate_live_model(data.get("model") or agent.model)
                except RuntimeError as e:
                    ui.error(f"could not activate session model: {e}")
                    continue
                session.clear()
                session.update(data)
                saved_messages = data.get("messages", [])
                agent.load_messages(saved_messages)
                agent.state.update(todos=[])
                agent.on_turn_complete = _on_turn
                agent.engine.session_id = session["id"]
                last_user_input = ""
                retry_messages = None
                ui.info(f"resumed {session['id']} — {session.get('title', '')}")
                _replay_conversation(saved_messages)
                continue
            if cmd == "sessions":
                _print_sessions(active_id=session.get("id"))
                continue
            if cmd == "search":
                query = (arg1 + (" " + arg2 if arg2 else "")).strip()
                if not query:
                    ui.warn("usage: /search <text>")
                    continue
                found = _search_sessions(query)
                if not found:
                    ui.info("no matching sessions")
                else:
                    print()
                    ui.heading("Search results")
                    print()
                    for item in found:
                        ui.list_item(item["title"], detail=item["id"])
                continue
            if cmd == "context":
                from .token_count import count_messages

                used = count_messages(agent.messages, agent.state.active_model_spec or agent.model)
                # Ask about the model actually in play — a /model switch that
                # hasn't been persisted still has to report the right window.
                limit = command_utils.context_window(
                    cfg, model_spec=agent.state.active_model_spec or agent.model
                )
                ui.info(f"context: {used:,} / {limit:,} tokens ({used / max(1, limit):.0%})")
                continue
            if cmd == "cost":
                _report_cost(agent)
                continue
            if cmd == "compact":
                from .services.compact import auto_compact
                from .token_count import count_messages

                before = count_messages(
                    agent.messages, agent.state.active_model_spec or agent.model
                )
                report = auto_compact(
                    agent.messages,
                    max_tokens=int(command_utils.context_window(cfg) * 0.9),
                    keep_recent=6,
                    summarize_with_model=agent.engine._summarize_with_model,
                )
                if not _persist_session():
                    continue
                last_user_input = ""
                retry_messages = None
                after = count_messages(agent.messages, agent.state.active_model_spec or agent.model)
                ui.info(
                    f"context compacted: {before:,} → {after:,} tokens"
                    if report.triggered
                    else f"context already compact enough ({after:,} tokens)"
                )
                continue
            if cmd == "save":
                if arg1 or arg2:
                    session["title"] = (arg1 + (" " + arg2 if arg2 else "")).strip()
                if not _persist_session():
                    continue
                ui.info(f"saved {session['id']} — {session.get('title', '')}")
                continue
            if cmd == "rename":
                new_title = (arg1 + (" " + arg2 if arg2 else "")).strip()
                if not new_title:
                    ui.warn("usage: /rename <new title>")
                    continue
                session["title"] = new_title
                if not _persist_session():
                    continue
                ui.info(f"renamed to '{new_title}'")
                continue
            if cmd == "delete":
                if not arg1:
                    ui.warn("usage: /delete <id|number>")
                    continue
                listed = sessions.list_all()
                session_target = _resolve_session_arg(arg1, listed)
                if not session_target:
                    ui.warn(_session_miss(arg1, listed))
                    continue
                if session_target["id"] == session.get("id"):
                    ui.warn("can't delete the active session — use /new first")
                    continue
                if sessions.delete(session_target["id"]):
                    ui.info(f"deleted {session_target['id']}")
                else:
                    ui.warn("delete failed")
                continue
            if cmd == "accept":
                on = command_utils.switch_value(arg1, agent.state.approval_mode == "accept_edits")
                if on is None:
                    ui.warn("usage: /accept [on|off]")
                    continue
                agent.state.update(approval_mode="accept_edits" if on else "ask")
                if on:
                    ui.info("accept-edits: ON — file changes inside the workspace apply")
                    ui.meta("shell, browser and network calls still ask")
                else:
                    ui.info("accept-edits: OFF — every change asks again")
                continue
            if cmd == "rules":
                from .permissions import effective_rules

                parts = arg1.split(maxsplit=1)
                action = parts[0].lower() if parts else ""
                rule = parts[1].strip() if len(parts) > 1 else ""
                cfg_rules = cfg.setdefault("permissions", {"allow": [], "deny": []})
                if action in ("allow", "deny"):
                    if not rule:
                        ui.warn(f"usage: /rules {action} <tool> or <tool(glob)>")
                        continue
                    bucket = cfg_rules.setdefault(action, [])
                    if rule not in bucket:
                        bucket.append(rule)
                    agent.state.update(permission_rules=copy.deepcopy(cfg_rules))
                    if not _persist_config():
                        continue
                    ui.info(f"{action}: {rule} (saved)")
                    continue
                if action == "remove":
                    removed = False
                    for key in ("allow", "deny"):
                        bucket = cfg_rules.get(key) or []
                        if rule in bucket:
                            bucket.remove(rule)
                            removed = True
                    if not removed:
                        ui.warn(f"no such rule: {rule}")
                        continue
                    agent.state.update(permission_rules=copy.deepcopy(cfg_rules))
                    if not _persist_config():
                        continue
                    ui.info(f"removed: {rule} (saved)")
                    continue
                if action:
                    ui.warn("usage: /rules [allow|deny|remove <rule>]")
                    continue
                in_force = effective_rules(agent.state)
                if not in_force["allow"] and not in_force["deny"]:
                    ui.info("no permission rules configured")
                    ui.meta("example: /rules allow run_bash(git status*)")
                    ui.meta("deny beats everything, including yolo")
                    continue
                for key, marker in (("deny", "✗"), ("allow", "✓")):
                    for listed_rule in in_force[key]:
                        ui.list_item(listed_rule, detail=key, marker=marker)
                continue
            if cmd == "yolo":
                yolo_enabled = command_utils.switch_value(arg1, agent.state.yolo)
                if yolo_enabled is None:
                    ui.warn("usage: /yolo [on|off]")
                    continue
                agent.state.update(yolo=yolo_enabled)
                cfg["yolo"] = yolo_enabled
                if not _persist_config():
                    continue
                ui.info(f"yolo: {'ON' if yolo_enabled else 'OFF'} (saved)")
                continue
            if cmd == "retry":
                if not last_user_input or retry_messages is None:
                    ui.warn("nothing to retry yet")
                    continue
                ui.info(f"retrying: {last_user_input[:80]}")
                agent.load_messages(copy.deepcopy(retry_messages))
                line = last_user_input
            else:
                import difflib

                from .prompt import SLASH_COMMANDS

                names = [name.lstrip("/") for name, _hint in SLASH_COMMANDS]
                suggestion = difflib.get_close_matches(cmd, names, n=1, cutoff=0.5)
                hint = f" Did you mean /{suggestion[0]}?" if suggestion else ""
                ui.warn(f"unknown command: /{cmd}.{hint}")
                continue

        if not (line.startswith("/") and line.split(maxsplit=1)[0].lower() == "/retry"):
            retry_messages = _retry_snapshot(agent)
        last_user_input = line
        try:
            agent.turn(line, typeahead=typeahead)
        except KeyboardInterrupt:
            ui.warn("interrupted")
            continue
        queued = agent.pending_input
        carried = agent.pending_partial or carried


# Built-in slash commands, derived from the one catalog so a project command
# can never shadow /quit or /yolo — and so this can't drift as commands are
# added. `reminders` is the documented alias the catalog carries separately.
def _project_facts(workspace: Path, limit: int = 60) -> str:
    """A short, factual sketch of the repo for /init to write against.

    Deliberately shallow and bounded: the point is to anchor the model in this
    project's real shape (build files, entry points, test layout) without
    spending the turn's context on a full tree walk.
    """
    interesting = (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
        "package.json",
        "tsconfig.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "Gemfile",
        "composer.json",
        "Makefile",
        "Justfile",
        "Dockerfile",
        "docker-compose.yml",
        ".pre-commit-config.yaml",
        "README.md",
        "CONTRIBUTING.md",
    )
    lines: list[str] = []
    try:
        present = [n for n in interesting if (workspace / n).is_file()]
        if present:
            lines.append("Build/config files: " + ", ".join(present))
        entries = sorted(p for p in workspace.iterdir() if not p.name.startswith("."))[:limit]
        dirs = [p.name + "/" for p in entries if p.is_dir()]
        files = [p.name for p in entries if p.is_file()]
        if dirs:
            lines.append("Top-level directories: " + ", ".join(dirs))
        if files:
            lines.append("Top-level files: " + ", ".join(files))
        # Test layout is the single most useful thing to get right, and the
        # convention differs enough between projects to be worth stating.
        for candidate in ("tests", "test", "spec", "__tests__"):
            d = workspace / candidate
            if d.is_dir():
                names = sorted(p.name for p in d.iterdir() if p.is_file())[:12]
                lines.append(f"{candidate}/ contains: " + ", ".join(names))
                break
    except OSError as exc:
        lines.append(f"(could not fully inspect the workspace: {exc})")
    return "\n".join(lines) or "(empty workspace)"


_BUILTIN_COMMAND_NAMES = frozenset(name.lstrip("/") for name in ALL_COMMANDS)


def _branch_label(workspace: Path) -> str:
    """Git branch (with `*` when dirty) for the prompt toolbar, or ""."""
    try:
        from .gitignore import branch_label

        return branch_label(workspace)
    except Exception:
        # The toolbar must never be the thing that breaks the prompt.
        return ""


def _revert_edits(entries: list[dict]) -> tuple[int, list[str]]:
    """Restore the given edit-history entries. Returns (reverted, problems).

    Shared by /undo and /rewind so both enforce the same guard: never revert a
    file the user has touched since Cagentic wrote it. Entries must already be
    ordered newest-first, because two edits to one file only unwind correctly
    in reverse.
    """
    from .tools import _read_text_robust, _write_text_raw

    reverted = 0
    problems: list[str] = []
    for entry in entries:
        path = Path(entry["path"])
        op = entry.get("op", "edit")
        try:
            if not path.exists() and op != "create":
                problems.append(f"{path} no longer exists")
                continue
            if path.exists() and _read_text_robust(path) != entry.get("after", ""):
                problems.append(f"{path} changed after Cagentic's edit; review it before reverting")
                continue
            if op == "create":
                path.unlink(missing_ok=True)
            else:
                # Same raw write the edit tools use: Path.write_text would
                # translate "\n" to os.linesep and turn an LF file into CRLF
                # on Windows, so a revert would leave a whole-file diff behind.
                _write_text_raw(path, entry.get("before", ""))
            reverted += 1
        except OSError as exc:
            problems.append(f"{path}: {exc}")
    return reverted, problems


def _user_message_indices(messages: list[dict]) -> list[int]:
    """Positions of the real user turns, skipping tool results and injections.

    Tool results are stored with role "tool", but background notifications are
    injected as plain user messages — those aren't turns the user typed, and
    counting them would make /rewind's numbering drift from what it printed.
    """
    return [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user" and not str(m.get("content") or "").startswith("[background]")
    ]


def _turn_summary(agent: Agent, history: list[dict]) -> list[tuple[int, str, int]]:
    """(turn number, prompt preview, edit count) for each turn this session."""
    edits_per_turn: dict[int, int] = {}
    for entry in history:
        turn = int(entry.get("turn", 0))
        edits_per_turn[turn] = edits_per_turn.get(turn, 0) + 1
    out: list[tuple[int, str, int]] = []
    for number, index in enumerate(_user_message_indices(agent.messages), start=1):
        text = str(agent.messages[index].get("content") or "").strip()
        # Attachments are appended after a blank line; the first line is what
        # the user actually typed.
        preview = ui.single_line(text.split("\n\n")[0])
        out.append((number, ui.truncate(preview, 60), edits_per_turn.get(number, 0)))
    return out


def _truncate_to_turn(agent: Agent, target: int) -> int:
    """Drop the conversation from turn `target` onward. Returns messages removed."""
    indices = _user_message_indices(agent.messages)
    if target < 1 or target > len(indices):
        return 0
    cut = indices[target - 1]
    dropped = len(agent.messages) - cut
    agent.load_messages(agent.messages[:cut])
    return dropped


def _split_rules(text: str | None) -> list[str]:
    """Parse a comma-separated --allowed-tools / --disallowed-tools value."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _apply_automation_options(agent: Agent, args: RuntimeOptions) -> None:
    """Apply the non-interactive flags: permission mode, rules, prompt, resume.

    These exist so Cagentic can be driven from a script or CI, where there is
    nobody to answer a y/n prompt — which is why `--permission-mode` has to be
    able to reach the same states the slash commands set interactively.
    """
    mode = (args.permission_mode or "").lower()
    if mode == "yolo":
        agent.state.update(yolo=True)
    elif mode == "accept-edits":
        agent.state.update(approval_mode="accept_edits")
    elif mode == "plan":
        agent.state.update(plan_mode=True)
    elif mode == "ask":
        agent.state.update(approval_mode="ask", yolo=False)

    allow = _split_rules(args.allowed_tools)
    deny = _split_rules(args.disallowed_tools)
    if allow or deny:
        rules = copy.deepcopy(agent.state.permission_rules or {})
        rules["allow"] = list(rules.get("allow") or []) + allow
        rules["deny"] = list(rules.get("deny") or []) + deny
        agent.state.update(permission_rules=rules)

    if args.append_system_prompt:
        # Appended rather than replacing: the base prompt carries the tool
        # contracts the model needs to function at all.
        suffix = agent.engine.system_suffix or ""
        agent.engine.system_suffix = (
            suffix + "\n\n" + args.append_system_prompt if suffix else args.append_system_prompt
        )
        agent.engine.refresh_system_prompt()


def _resume_session(agent: Agent, args: RuntimeOptions) -> dict | None:
    """Load the session named by --continue/--resume, or None.

    Returns the session record so the caller keeps saving into it rather than
    starting a fresh one — resuming into a new session would silently fork the
    conversation the user asked to continue.
    """
    if not (args.continue_last or args.resume_id):
        return None
    listed = sessions.list_all()
    if not listed:
        ui.warn("no saved conversations to resume")
        return None
    target: dict | None
    if args.continue_last:
        # list_all() sorts by updated_at descending, so [0] is the newest.
        target = listed[0]
    else:
        target = _resolve_session_arg(args.resume_id or "", listed)
    if target is None:
        ui.error(_session_miss(args.resume_id or "", listed))
        return None
    loaded = sessions.load(target["id"])
    if loaded is None:
        ui.error(f"could not load session {target['id']}")
        return None
    agent.load_messages(loaded.get("messages", []))
    agent.engine.session_id = loaded["id"]
    ui.info(
        f"resumed {loaded.get('title') or loaded['id']} ({len(loaded.get('messages', []))} messages)"
    )
    return loaded


def _list_models_with_retry(client, attempts: int = 5, delay: float = 2.0):
    """List models, retrying briefly on *connection* failures only.

    The Ollama desktop app (tray/service) takes a few seconds to bind
    11434 after login. Launching cagentic in that window used to hard-fail
    instantly with "Is `ollama serve` running?" even though Ollama was
    seconds away from being ready. Retry connection-refused / connect-
    timeout a handful of times so a startup race doesn't look like a
    missing install. HTTP errors (404/500) are NOT retried — reconnecting
    won't fix a broken server.
    """
    import requests

    last_err: OllamaError | None = None
    for i in range(attempts):
        try:
            return client.list_models()
        except OllamaError as e:
            last_err = e
            # Only the connection-level failures are worth retrying.
            cause = e.__cause__
            if not isinstance(cause, requests.ConnectionError):
                raise
            if i < attempts - 1:
                time.sleep(delay)
    assert last_err is not None
    raise last_err


def _run_runtime(args: RuntimeOptions) -> int:
    """Run chat, one-shot, or gateway mode after Click has validated syntax."""
    cfg = config.load()
    serve_mode = args.mode == "serve"

    # Runtime overrides are ephemeral. Persistent identity/model changes belong
    # to the explicit `setup` command.
    if args.host:
        cfg["host"] = args.host
    if args.model:
        cfg["model"] = args.model
    if args.name:
        config.set_value(cfg, "user_name", args.name)

    raw_workspace = args.cwd or Path(".")
    root = raw_workspace.expanduser().resolve()
    if not root.is_dir():
        ui.error(f"workspace not a directory: {root}")
        return 2

    # A packaged launch from the source checkout should open in the user's
    # home, but an explicit `-C .` always means exactly what the user requested.
    if args.cwd is None:
        cagentic_root = Path(__file__).resolve().parent.parent
        inside_install = root == cagentic_root or cagentic_root in root.parents
        if inside_install:
            root = Path.home()

    model_raw = args.model or os.environ.get("CAGENTIC_MODEL") or cfg.get("model")
    provider = "ollama"

    if model_raw is not None and not isinstance(model_raw, str):
        ui.warn(f"ignoring invalid configured model={model_raw!r}")
        model_raw = None
    if isinstance(model_raw, str):
        model_raw = model_raw.strip()
        provider, parsed_model = _parse_model_provider(model_raw)
        if not parsed_model:
            ui.warn(f"ignoring invalid configured model={model_raw!r}; model name is missing")
            model_raw = None
            provider = "ollama"
        else:
            model_raw = parsed_model

    # Build initial client (Ollama by default; cloud if model has prefix).
    try:
        client = _build_client(cfg, provider)
    except RuntimeError as e:
        ui.error(str(e))
        return 1
    except ValueError as exc:
        ui.error(f"invalid provider configuration: {exc}")
        ui.warn("Pass a valid --host URL or correct the saved host setting, then retry.")
        return 1

    # Normalize the Ollama host so /config shows the routable address.
    if isinstance(client, OllamaClient):
        raw_host = (
            args.host or os.environ.get("OLLAMA_HOST") or cfg.get("host", "http://localhost:11434")
        )
        if not isinstance(raw_host, str):
            raw_host = client.host
        cfg["host"] = client.host
        if "0.0.0.0" in raw_host or raw_host.strip() in ("::", "[::]", "0"):
            ui.info(
                f"Ollama host {raw_host!r} is a bind-all address — "
                f"connecting to {client.host} instead."
            )

    model = model_raw
    if not model:
        if args.mode != "chat":
            ui.error("no model configured; pass --model MODEL or run `cagentic --setup`")
            return 1
        chosen = _pick_model_interactive(client)
        if not chosen:
            ui.error("No model selected. Exiting.")
            return 1
        model = chosen
        cfg["model"] = model
        if args.dry_run:
            ui.info(f"dry run: would save model '{model}' · no settings changed")
        else:
            try:
                config.save(cfg)
            except (OSError, TypeError, ValueError) as exc:
                ui.error(f"could not save selected model: {exc}; no settings were changed")
                return 1
            ui.info(f"saved model '{model}' to {config.config_path()}")

    try:
        with ui.Spinner("checking available models"):
            models = _list_models_with_retry(client)
    except OllamaError as e:
        if serve_mode:
            # Headless service may start before Ollama does — keep the
            # gateway up; chats will error until the provider is reachable.
            ui.warn(f"model list unavailable at startup: {e}")
            models = []
        else:
            ui.error(str(e))
            if isinstance(client, OllamaClient):
                ui.warn("Is `ollama serve` running?")
            return 1
    available_names = {_parse_model_provider(item)[1] for item in models}
    if model not in available_names and models:
        if isinstance(client, OllamaClient):
            ui.warn(f"model '{model}' not installed locally. Available: {', '.join(models[:8])}")
            ui.warn(f"Pull it with:  ollama pull {model}")
        else:
            ui.warn(
                f"model '{model}' not found in provider list. Available: {', '.join(m.split(':', 1)[-1] for m in models[:8])}"
            )

    if args.temperature is not None:
        temperature = args.temperature
    else:
        configured_temperature = cfg.get("temperature", 0.4)
        if command_utils.validate_config_value("temperature", configured_temperature):
            ui.warn(f"ignoring invalid temperature={configured_temperature!r}; using 0.4")
            temperature = 0.4
        else:
            temperature = float(configured_temperature)
    configured_yolo = cfg.get("yolo", False)
    if not isinstance(configured_yolo, bool):
        ui.warn(f"ignoring invalid yolo={configured_yolo!r}; using false")
        configured_yolo = False
    yolo = configured_yolo if args.yolo is None else args.yolo
    if args.dry_run:
        yolo = False
    configured_name = cfg.get("user_name")
    user_name = configured_name if isinstance(configured_name, str) else None

    active_model_spec = f"{provider}:{model}" if provider != "ollama" else model
    tools_supported = _tools_supported(cfg, active_model_spec)
    if tools_supported is False:
        ui.warn(f"note: '{model}' is known not to support tool calls — running tool-less.")

    def _remember_no_tools(_a):
        # Persist under the agent's CURRENT model, not the startup `model` this
        # closure captured — otherwise switching to a tool-less model B records
        # the flag against model A and penalizes the wrong one next launch.
        if args.dry_run:
            return
        _remember_tools_unsupported(cfg, _a.state.active_model_spec or _a.model)
        try:
            config.save(cfg)
        except (OSError, TypeError, ValueError) as exc:
            ui.warn(f"could not save model capability: {exc}")

    agent = Agent(
        client=client,
        model=model,
        root=root,
        yolo=yolo,
        temperature=temperature,
        tools_enabled=bool(tools_supported),
        on_tools_disabled=_remember_no_tools,
        stream=_configured_bool(cfg, "ollama.stream", True),
        config=cfg,
        user_name=user_name,
        dry_run=args.dry_run,
    )
    agent.state.update(
        active_model_spec=active_model_spec,
        github_token=config.get_value(cfg, "github.token"),
        insecure_ssl=_configured_bool(cfg, "insecure_ssl", False),
        dry_run=args.dry_run,
        plan_mode=args.dry_run,
        # Copy, so /rules edits mutate the config dict and the state together
        # only where we intend to (via an explicit state.update).
        permission_rules=copy.deepcopy(config.get_value(cfg, "permissions", {}) or {}),
    )
    _apply_automation_options(agent, args)
    resumed_session = _resume_session(agent, args)

    saved_groups = config.get_value(cfg, "tool_groups", None)
    if isinstance(saved_groups, list):
        # Ignore group names that no longer exist rather than silently sending
        # the model an empty toolset from a stale config.
        from .tools import TOOL_GROUPS

        known = {g for g in saved_groups if isinstance(g, str) and g in TOOL_GROUPS}
        if known or not saved_groups:
            agent.state.update(tool_groups=known)

    # Wire MCP manager onto state, but lazy-start servers
    if command_utils.mcp_server_config(cfg):
        from .mcp_client import MCPManager

        agent.state.update(mcp=MCPManager(cfg))

    # Start the browser bridge so the Chrome extension can connect.
    configured_browser = cfg.get("browser")
    br_cfg = configured_browser if isinstance(configured_browser, dict) else {}
    if not args.dry_run and command_utils.boolean_value(br_cfg.get("enabled", True), True):
        from .browser import BrowserBridge

        bridge = BrowserBridge(
            port=_configured_port(cfg, "browser", 8765),
            site_rules=config.get_value(cfg, "browser.sites"),
        )
        if bridge.start():
            bridge.set_status(model=model, activity="idle")
            agent.state.update(browser=bridge)
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
        if not args.dry_run:
            try:
                client.unload(agent.model)
            except Exception:
                logger.warning("shutdown: client.unload failed", exc_info=True)
        try:
            if agent.state.mcp is not None:
                shutdown_mcp = getattr(agent.state.mcp, "shutdown", None)
                if callable(shutdown_mcp):
                    shutdown_mcp()
        except Exception:
            logger.warning("shutdown: MCP shutdown failed", exc_info=True)
        try:
            if agent.state.browser is not None:
                stop_browser = getattr(agent.state.browser, "stop", None)
                if callable(stop_browser):
                    stop_browser()
        except Exception:
            logger.warning("shutdown: browser stop failed", exc_info=True)
        try:
            if gateway_holder["server"] is not None:
                gateway_holder["server"].stop()
        except Exception:
            logger.warning("shutdown: gateway stop failed", exc_info=True)

    atexit.register(_shutdown)
    for sig_name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_: (_shutdown(), sys.exit(0)))
        except (ValueError, OSError):
            logger.warning("could not install handler for %s", sig_name, exc_info=True)

    if serve_mode:
        from .gateway import Gateway

        port = args.port or _configured_port(cfg, "gateway", 8700)
        gw = Gateway(agent, cfg, port=port)
        if not gw.start():
            ui.error(f"gateway couldn't start: {gw.error}")
            return 1
        gateway_holder["server"] = gw
        if gw.start_notice:
            ui.warn(gw.start_notice)
        ui.info(f"gateway running at {gw.url()} — press Ctrl-C to stop.")

        # Auto-reload: the daemon installed by --install-service outlives every
        # edit to the source it imported, so without this you change a file and
        # the background gateway keeps serving yesterday's code. Only in serve
        # mode — the REPL's own `/gateway on` shares this process, and
        # re-execing would kill the user's session.
        reloader = None
        if _configured_bool(cfg, "gateway.auto_reload", True):
            from .autoreload import GatewayReloader

            reloader = GatewayReloader(is_busy=gw.is_busy, shutdown=gw.stop)
            reloader.start()
            ui.meta(f"watching {reloader.root} — restarts on code change")

        try:
            import threading

            # Park the main thread with a short-timeout poll rather than
            # Event().wait() with no timeout: on Windows an unbounded wait
            # blocks in a non-interruptible C-level call, so SIGINT (Ctrl-C)
            # never raises KeyboardInterrupt and the gateway can't be stopped
            # with Ctrl-C. Returning to Python every 0.5s lets the default
            # SIGINT handler fire.
            stop = threading.Event()
            while not stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        if reloader is not None:
            reloader.stop()
        _shutdown()
        return 0

    if args.prompt is not None and args.stream_json:
        # One JSON object per engine event, newline-delimited — what lets
        # another program consume the run as it happens. `event_payload` is
        # shared with the gateway's SSE mapping so the two can't drift.
        from .engine import event_payload

        sink = args.stream_sink or sys.stdout
        failed = False
        try:
            for event in agent.engine.submit_message(args.prompt):
                print(json.dumps(event_payload(event), default=str), file=sink, flush=True)
                if event.kind == "error":
                    failed = True
        except (OllamaError, OSError) as exc:
            print(json.dumps({"kind": "error", "text": str(exc)}), file=sink, flush=True)
            return 1
        return 1 if failed else 0

    if args.prompt is not None:
        response = agent.turn(args.prompt)
        if args.result is not None:
            args.result.update(
                {
                    "ok": not agent.last_turn_failed,
                    "response": response,
                    "dry_run": args.dry_run,
                    "model": agent.state.active_model_spec or agent.model,
                    "workspace": str(agent.state.workspace),
                }
            )
        return 1 if agent.last_turn_failed else 0
    return repl(agent, cfg, gateway_holder, resumed=resumed_session)


def _format_option(function: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--format",
        "output_format",
        type=OUTPUT_FORMAT,
        default="text",
        show_default=True,
        help="Output format for humans or automation.",
    )(function)


def _dry_run_option(function: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--dry-run",
        is_flag=True,
        help="Preview the operation without persistent or external mutations.",
    )(function)


def _runtime_options(function: Callable[..., Any]) -> Callable[..., Any]:
    decorators = [
        click.option(
            "-m",
            "--model",
            callback=_model_value,
            metavar="MODEL",
            help="Model name or provider:model override.",
        ),
        click.option(
            "--host",
            callback=_host_value,
            metavar="URL",
            help="Ollama host override.",
        ),
        click.option(
            "-C",
            "--cwd",
            type=click.Path(
                exists=True,
                file_okay=False,
                dir_okay=True,
                readable=True,
                resolve_path=True,
                path_type=Path,
            ),
            metavar="DIR",
            help="Workspace directory.",
        ),
        click.option(
            "-t",
            "--temperature",
            type=click.FloatRange(0.0, 2.0),
            metavar="0..2",
            help="Sampling temperature.",
        ),
        click.option(
            "--name",
            callback=_nonempty,
            metavar="NAME",
            help="Save the name Cagentic should use for you.",
        ),
        click.option(
            "--yolo/--no-yolo",
            default=None,
            help="Enable or disable automatic tool approval for this run.",
        ),
        click.option(
            "-c",
            "--continue",
            "continue_last",
            is_flag=True,
            help="Resume the most recent conversation.",
        ),
        click.option(
            "--resume",
            "resume_id",
            metavar="SESSION",
            callback=_nonempty,
            help="Resume a saved conversation by id or list number.",
        ),
        click.option(
            "--allowed-tools",
            metavar="RULES",
            callback=_nonempty,
            help=(
                "Comma-separated permission rules to allow for this run, e.g. "
                "'run_bash(git status*),read_file'. Same syntax as /rules."
            ),
        ),
        click.option(
            "--disallowed-tools",
            metavar="RULES",
            callback=_nonempty,
            help="Comma-separated rules to deny for this run. Deny beats everything.",
        ),
        click.option(
            "--permission-mode",
            type=PERMISSION_MODE,
            help="ask (default), accept-edits, plan, or yolo.",
        ),
        click.option(
            "--append-system-prompt",
            metavar="TEXT",
            callback=_nonempty,
            help="Extra instructions appended to the system prompt for this run.",
        ),
    ]
    for decorator in reversed(decorators):
        function = decorator(function)
    return function


def _json(data: dict[str, Any]) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True))


def _capture_operation(operation: Callable[[], int]) -> tuple[int, list[str], list[str]]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    previous_no_color = os.environ.get("NO_COLOR")
    os.environ["NO_COLOR"] = "1"
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = operation()
    finally:
        if previous_no_color is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = previous_no_color
    return code, stdout.getvalue().splitlines(), stderr.getvalue().splitlines()


def _run_stream_json(options: RuntimeOptions) -> int:
    """Run one prompt with stdout reserved for newline-delimited JSON events.

    Start-up is chatty — a browser-bridge port clash, a model-capability
    warning — and every one of those lines used to land on stdout, where it
    corrupted the stream the caller was piping into `jq`. Human-facing output
    is redirected to stderr for the whole run; only events reach stdout.
    """
    options.stream_json = True
    options.stream_sink = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        return _run_runtime(options)


def _run_json(options: RuntimeOptions) -> int:
    result: dict[str, Any] = {}
    options.result = result
    code, events, errors = _capture_operation(lambda: _run_runtime(options))
    result.setdefault("ok", code == 0)
    result["exit_code"] = code
    if events:
        result["events"] = events
    if errors:
        result["errors"] = errors
    _json(result)
    return code


def _prepare_setup(
    cfg: dict,
    *,
    model: str | None,
    name: str | None,
    workspace_roots: tuple[Path, ...],
    lan: bool | None,
) -> tuple[dict, list[str]]:
    working = copy.deepcopy(cfg)
    changes: list[str] = []
    if model is not None:
        working["model"] = model
        changes.append("model")
    if name is not None:
        working["user_name"] = name
        changes.append("user_name")
    if workspace_roots:
        config.set_value(
            working,
            "gateway.workspace_roots",
            [str(path.resolve()) for path in workspace_roots],
        )
        changes.append("gateway.workspace_roots")
    if lan is not None:
        config.set_value(working, "gateway.lan", lan)
        changes.append("gateway.lan")
        if lan and not config.get_value(working, "gateway.token"):
            import secrets

            config.set_value(working, "gateway.token", secrets.token_urlsafe(32))
            changes.append("gateway.token (generated)")
    return working, changes


def _save_setup(
    cfg: dict,
    changes: list[str],
    *,
    dry_run: bool,
    output_format: str,
) -> int:
    payload = {
        "ok": True,
        "dry_run": dry_run,
        "config": str(config.config_path()),
        "changes": changes,
    }
    if dry_run:
        if output_format == "json":
            _json(payload)
        else:
            ui.info(
                "dry run: would update "
                + ", ".join(changes)
                + f" in {config.config_path()} · no settings changed"
            )
        return 0
    try:
        config.save(cfg)
    except (OSError, TypeError, ValueError) as exc:
        message = f"could not save setup: {exc}; no settings were changed"
        if output_format == "json":
            _json({"ok": False, "error": message})
        else:
            ui.error(message)
        return 1
    if output_format == "json":
        _json(payload)
    else:
        ui.info(f"saved {', '.join(changes)} to {config.config_path()}")
    return 0


class SuggestingGroup(click.Group):
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        import difflib

        match = difflib.get_close_matches(cmd_name, self.list_commands(ctx), n=1, cutoff=0.5)
        if match:
            ctx.fail(f"No such command '{cmd_name}'. Did you mean '{match[0]}'?")
        return None


_DEBUG_ACTIVE = False


@click.group(
    cls=SuggestingGroup,
    invoke_without_command=True,
    no_args_is_help=False,
    subcommand_metavar="[COMMAND] [ARGS]...",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    epilog=(
        "\b\nExamples:\n"
        "  cagentic\n"
        '  cagentic run "summarize @notes.md"\n'
        "  cagentic doctor --format json\n"
        "  cagentic compact SESSION --dry-run"
    ),
)
@click.option(
    "--debug",
    is_flag=True,
    help="Show Python tracebacks for unexpected internal errors.",
)
@click.version_option(
    __version__,
    "-V",
    "--version",
    prog_name="cagentic",
    message="%(prog)s %(version)s",
)
@click.option("-p", "--prompt", callback=_nonempty, help="Send one prompt and exit.")
@_runtime_options
@click.option("--doctor", is_flag=True, help="Run diagnostics and exit.")
@click.option("--sessions", is_flag=True, help="List saved conversations and exit.")
@click.option("--search", metavar="TEXT", callback=_nonempty, help="Search saved conversations.")
@click.option("--context", "context_ref", metavar="SESSION", help="Show session token usage.")
@click.option("--compact", "compact_ref", metavar="SESSION", help="Compact a saved session.")
@click.option("--setup", is_flag=True, help="Run the guided setup wizard.")
@click.option("--login", "login_service", type=SERVICE, help="Save a service key securely.")
@click.option("--logout", "logout_service", type=SERVICE, help="Remove a saved service key.")
@click.option(
    "--token-stdin",
    is_flag=True,
    help="Read the key for --login from standard input instead of a hidden prompt.",
)
@click.option("--completion", type=SHELL, help="Print a shell completion script.")
@click.option("--serve", is_flag=True, help="Run the local web gateway without the REPL.")
@click.option("--port", type=click.IntRange(1, 65535), metavar="PORT", help="Gateway port.")
@click.option("--install-service", is_flag=True, help="Install the gateway login service.")
@click.option("--uninstall-service", is_flag=True, help="Remove the gateway login service.")
@click.option("--reset-config", is_flag=True, help="Remove saved settings and exit.")
@click.option(
    "--threshold",
    type=click.FloatRange(0.0, 1.0, min_open=True),
    metavar="0..1",
    default=0.9,
    show_default=True,
    help="Compaction target as a fraction of the context window.",
)
@click.option(
    "--workspace-root",
    "workspace_roots",
    multiple=True,
    type=click.Path(
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        path_type=Path,
    ),
    metavar="DIR",
    help="Allowed gateway root for --setup; repeat for multiple roots.",
)
@click.option("--lan/--no-lan", default=None, help="Enable or disable LAN access in --setup.")
@click.option("--dry-run", is_flag=True, help="Preview mutations without saving changes.")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--format",
    "output_format",
    type=OUTPUT_FORMAT,
    default=None,
    help="Choose text or machine-readable JSON output.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    debug: bool,
    prompt: str | None,
    model: str | None,
    host: str | None,
    cwd: Path | None,
    temperature: float | None,
    name: str | None,
    yolo: bool | None,
    continue_last: bool,
    resume_id: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
    permission_mode: str | None,
    append_system_prompt: str | None,
    doctor: bool,
    sessions: bool,
    search: str | None,
    context_ref: str | None,
    compact_ref: str | None,
    setup: bool,
    login_service: str | None,
    logout_service: str | None,
    token_stdin: bool,
    completion: str | None,
    serve: bool,
    port: int | None,
    install_service: bool,
    uninstall_service: bool,
    reset_config: bool,
    threshold: float,
    workspace_roots: tuple[Path, ...],
    lan: bool | None,
    dry_run: bool,
    json_output: bool,
    output_format: str | None,
) -> int | None:
    """Local-first AI assistant for terminal work, memory, and automation."""
    global _DEBUG_ACTIVE
    _DEBUG_ACTIVE = debug
    # Route package logging to a file before anything can log. Without this,
    # Python's lastResort handler prints every warning + traceback to stderr,
    # straight into the middle of the assistant's reply.
    _logs.setup(debug=debug)
    root_mode_requested = any(
        (
            prompt is not None,
            doctor,
            sessions,
            search is not None,
            context_ref is not None,
            compact_ref is not None,
            setup,
            login_service is not None,
            logout_service is not None,
            completion is not None,
            serve,
            install_service,
            uninstall_service,
            reset_config,
        )
    )
    if ctx.invoked_subcommand is not None:
        if root_mode_requested:
            raise click.UsageError("do not combine a compatibility flag with a subcommand")
        return None
    threshold_source = ctx.get_parameter_source("threshold")
    if (
        threshold_source is not None
        and threshold_source.name == "COMMANDLINE"
        and context_ref is None
        and compact_ref is None
    ):
        raise click.UsageError("--threshold requires --context SESSION or --compact SESSION")
    return _run_classic_cli(
        prompt=prompt,
        model=model,
        host=host,
        cwd=cwd,
        temperature=temperature,
        name=name,
        yolo=yolo,
        continue_last=continue_last,
        resume_id=resume_id,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode=permission_mode,
        append_system_prompt=append_system_prompt,
        doctor=doctor,
        list_sessions=sessions,
        search=search,
        context_ref=context_ref,
        compact_ref=compact_ref,
        setup=setup,
        login_service=login_service,
        logout_service=logout_service,
        token_stdin=token_stdin,
        completion=completion,
        serve=serve,
        port=port,
        install_service=install_service,
        uninstall_service=uninstall_service,
        reset_config=reset_config,
        threshold=threshold,
        workspace_roots=workspace_roots,
        lan=lan,
        dry_run=dry_run,
        json_output=json_output,
        output_format=output_format,
    )


@cli.command("chat")
@_runtime_options
@_dry_run_option
def chat_command(
    model: str | None,
    host: str | None,
    cwd: Path | None,
    temperature: float | None,
    name: str | None,
    yolo: bool | None,
    continue_last: bool,
    resume_id: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
    permission_mode: str | None,
    append_system_prompt: str | None,
    dry_run: bool,
) -> int:
    """Start an explicitly interactive terminal session."""
    return _run_runtime(
        RuntimeOptions(
            mode="chat",
            model=model,
            host=host,
            cwd=cwd,
            temperature=temperature,
            name=name,
            yolo=yolo,
            dry_run=dry_run,
            continue_last=continue_last,
            resume_id=resume_id,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            permission_mode=permission_mode,
            append_system_prompt=append_system_prompt,
        )
    )


@cli.command("run")
@click.argument("prompt", callback=_nonempty)
@_runtime_options
@_dry_run_option
@_format_option
def run_command(
    prompt: str,
    model: str | None,
    host: str | None,
    cwd: Path | None,
    temperature: float | None,
    name: str | None,
    yolo: bool | None,
    continue_last: bool,
    resume_id: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
    permission_mode: str | None,
    append_system_prompt: str | None,
    dry_run: bool,
    output_format: str,
) -> int:
    """Send PROMPT once and exit; use -- before prompts beginning with '-'."""
    options = RuntimeOptions(
        mode="run",
        model=model,
        host=host,
        cwd=cwd,
        temperature=temperature,
        name=name,
        yolo=yolo,
        prompt=prompt,
        dry_run=dry_run,
        continue_last=continue_last,
        resume_id=resume_id,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode=permission_mode,
        append_system_prompt=append_system_prompt,
    )
    if output_format == "stream-json":
        return _run_stream_json(options)
    return _run_json(options) if output_format == "json" else _run_runtime(options)


@cli.command("doctor")
@_format_option
def doctor_command(output_format: str) -> int:
    """Run installation and connectivity diagnostics."""
    from .diagnostics import run

    report = run(config.load())
    if output_format == "json":
        _json(report)
    else:
        print()
        ui.heading("Diagnostics")
        print()
        for check in report["checks"]:
            ui.list_item(
                check["name"],
                detail=check["detail"],
                marker="✓" if check["ok"] else "×",
                active=bool(check["ok"]),
            )
    return 0 if report["ok"] else 1


@cli.command("sessions")
@_format_option
def sessions_command(output_format: str) -> int:
    """List saved conversations without starting a model."""
    rows = sessions.list_all()
    if output_format == "json":
        _json({"ok": True, "sessions": rows})
    else:
        _print_sessions()
    return 0


@cli.command("search")
@click.argument("query", callback=_nonempty)
@_format_option
def search_command(query: str, output_format: str) -> int:
    """Search saved conversation titles and messages for QUERY."""
    found = _search_sessions(query)
    if output_format == "json":
        _json({"ok": True, "query": query, "sessions": found})
    elif not found:
        ui.info("no matching conversations")
    else:
        print()
        ui.heading("Search results")
        print()
        for item in found:
            ui.list_item(item["title"], detail=item["id"])
    return 0


@cli.command("context")
@click.argument("session_ref", metavar="SESSION")
@click.option(
    "--threshold",
    type=click.FloatRange(0.0, 1.0, min_open=True),
    default=0.9,
    show_default=True,
    help="Fraction of the context window used as the compaction target.",
)
@_format_option
def context_command(session_ref: str, threshold: float, output_format: str) -> int:
    """Show token usage for SESSION."""
    cfg = config.load()
    return _print_context(
        session_ref,
        threshold,
        as_json=output_format == "json",
        context_limit=command_utils.context_window(cfg),
    )


@cli.command("compact")
@click.argument("session_ref", metavar="SESSION")
@click.option(
    "--threshold",
    type=click.FloatRange(0.0, 1.0, min_open=True),
    default=0.9,
    show_default=True,
    help="Fraction of the context window used as the compaction target.",
)
@_dry_run_option
@_format_option
def compact_command(
    session_ref: str,
    threshold: float,
    dry_run: bool,
    output_format: str,
) -> int:
    """Compact older messages in SESSION while retaining recent turns."""
    cfg = config.load()
    return _print_context(
        session_ref,
        threshold,
        compact=True,
        dry_run=dry_run,
        as_json=output_format == "json",
        context_limit=command_utils.context_window(cfg),
    )


@cli.command("setup")
@click.option("--model", callback=_model_value, metavar="MODEL", help="Model to persist.")
@click.option("--name", callback=_nonempty, metavar="NAME", help="Name to persist.")
@click.option(
    "--workspace-root",
    "workspace_roots",
    multiple=True,
    type=click.Path(
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        path_type=Path,
    ),
    metavar="DIR",
    help="Allowed gateway workspace root; repeat for multiple roots.",
)
@click.option("--lan/--no-lan", default=None, help="Enable or disable LAN gateway access.")
@click.option(
    "--interactive",
    is_flag=True,
    help="Prompt for setup values instead of requiring flags.",
)
@_dry_run_option
@_format_option
def setup_command(
    model: str | None,
    name: str | None,
    workspace_roots: tuple[Path, ...],
    lan: bool | None,
    interactive: bool,
    dry_run: bool,
    output_format: str,
) -> int:
    """Persist configuration; use --interactive to opt into prompts."""
    cfg, changes = _prepare_setup(
        config.load(),
        model=model,
        name=name,
        workspace_roots=workspace_roots,
        lan=lan,
    )
    if interactive:
        if output_format == "json":
            raise click.UsageError("--interactive cannot be combined with --format json")
        try:
            return 0 if _setup_wizard(cfg, dry_run=dry_run) else 1
        except EOFError:
            ui.error("setup input ended; no settings were changed")
            return 1
        except KeyboardInterrupt:
            print()
            ui.warn("setup cancelled; no settings were changed")
            return 1
    if not changes:
        raise click.UsageError(
            "pass at least one setup option, or use --interactive to be prompted"
        )
    return _save_setup(cfg, changes, dry_run=dry_run, output_format=output_format)


@cli.command("login")
@click.argument("service", type=SERVICE)
@click.option(
    "--prompt",
    "prompt_for_key",
    is_flag=True,
    help="Read the key from a hidden interactive prompt.",
)
@click.option(
    "--token-stdin",
    is_flag=True,
    help="Read the key from standard input for automation.",
)
@_dry_run_option
@_format_option
def login_command(
    service: str,
    prompt_for_key: bool,
    token_stdin: bool,
    dry_run: bool,
    output_format: str,
) -> int:
    """Securely save credentials for SERVICE."""
    if prompt_for_key and token_stdin:
        raise click.UsageError("choose exactly one of --prompt or --token-stdin")
    if dry_run:
        return _credential_mode(
            config.load(),
            service,
            login=True,
            secret="dry-run-placeholder",
            dry_run=True,
            as_json=output_format == "json",
        )
    if not prompt_for_key and not token_stdin:
        raise click.UsageError("choose exactly one of --prompt or --token-stdin")
    if prompt_for_key and output_format == "json":
        raise click.UsageError("--prompt cannot be combined with --format json; use --token-stdin")
    if prompt_for_key:
        import getpass

        try:
            secret = getpass.getpass(f"{service} key: ")
        except (EOFError, KeyboardInterrupt):
            ui.error("login cancelled; no settings were changed")
            return 1
    else:
        secret = click.get_text_stream("stdin").read()
    return _credential_mode(
        config.load(),
        service,
        login=True,
        secret=secret,
        dry_run=dry_run,
        as_json=output_format == "json",
    )


@cli.command("logout")
@click.argument("service", type=SERVICE)
@_dry_run_option
@_format_option
def logout_command(service: str, dry_run: bool, output_format: str) -> int:
    """Remove saved credentials for SERVICE."""
    return _credential_mode(
        config.load(),
        service,
        login=False,
        dry_run=dry_run,
        as_json=output_format == "json",
    )


@cli.command("completion")
@click.argument("shell", type=SHELL)
def completion_command(shell: str) -> int:
    """Print a parser-derived completion script for SHELL."""
    from .completion import script

    click.echo(script(shell))
    return 0


def _serve_preview(options: RuntimeOptions, output_format: str) -> int:
    cfg = config.load()
    root = (options.cwd or Path(".")).expanduser().resolve()
    model = options.model or os.environ.get("CAGENTIC_MODEL") or cfg.get("model")
    if not model:
        message = "no model configured; pass --model MODEL or run `cagentic --setup`"
        if output_format == "json":
            _json({"ok": False, "error": message})
        else:
            ui.error(message)
        return 1
    port = options.port or _configured_port(cfg, "gateway", 8700)
    payload: dict[str, Any] = {
        "ok": True,
        "dry_run": True,
        "action": "serve",
        "model": model,
        "workspace": str(root),
        "bind": f"127.0.0.1:{port}",
        "persistent_changes": [],
    }
    if output_format == "json":
        _json(payload)
    else:
        ui.info(
            f"dry run: would start the gateway on {payload['bind']} with {model}"
            f" in {root} · no server started"
        )
    return 0


@cli.command("serve")
@_runtime_options
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    metavar="PORT",
    help="Gateway port; defaults to configuration or 8700.",
)
@_dry_run_option
@_format_option
def serve_command(
    model: str | None,
    host: str | None,
    cwd: Path | None,
    temperature: float | None,
    name: str | None,
    yolo: bool | None,
    continue_last: bool,
    resume_id: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
    permission_mode: str | None,
    append_system_prompt: str | None,
    port: int | None,
    dry_run: bool,
    output_format: str,
) -> int:
    """Run the local web gateway until interrupted."""
    options = RuntimeOptions(
        mode="serve",
        model=model,
        host=host,
        cwd=cwd,
        temperature=temperature,
        name=name,
        yolo=yolo,
        port=port,
        dry_run=dry_run,
        continue_last=continue_last,
        resume_id=resume_id,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode=permission_mode,
        append_system_prompt=append_system_prompt,
    )
    if dry_run:
        return _serve_preview(options, output_format)
    if output_format == "json":
        raise click.UsageError("--format json requires --dry-run for the long-running server")
    return _run_runtime(options)


@cli.group("service", no_args_is_help=True)
def service_group() -> None:
    """Install or remove the gateway login service."""


def _service_preview(action: str, output_format: str) -> int:
    from . import service

    if sys.platform == "darwin":
        target = service._launchd_plist_path()
        manager = "launchd"
    elif sys.platform.startswith("linux"):
        target = service._systemd_unit_path()
        manager = "systemd"
    else:
        message = "background service management is supported on macOS and Linux only"
        if output_format == "json":
            _json({"ok": False, "error": message})
        else:
            ui.error(message)
        return 1
    steps = (
        ["write service definition", "reload service manager", "enable and start service"]
        if action == "install"
        else ["stop and disable service", "remove service definition", "reload service manager"]
    )
    payload: dict[str, Any] = {
        "ok": True,
        "dry_run": True,
        "action": action,
        "manager": manager,
        "target": str(target),
        "steps": steps,
    }
    if output_format == "json":
        _json(payload)
    else:
        ui.info(f"dry run: would {action} the {manager} service at {target} · no changes made")
        for step in steps:
            ui.list_item(step)
    return 0


def _run_service(action: str, dry_run: bool, output_format: str) -> int:
    from . import service

    if dry_run:
        return _service_preview(action, output_format)
    operation = service.install if action == "install" else service.uninstall
    if output_format == "text":
        return 0 if operation() == 0 else 1
    code, events, errors = _capture_operation(operation)
    _json(
        {
            "ok": code == 0,
            "action": action,
            "exit_code": 0 if code == 0 else 1,
            "events": events,
            "errors": errors,
        }
    )
    return 0 if code == 0 else 1


@service_group.command("install")
@_dry_run_option
@_format_option
def service_install_command(dry_run: bool, output_format: str) -> int:
    """Install and start the gateway login service."""
    return _run_service("install", dry_run, output_format)


@service_group.command("uninstall")
@_dry_run_option
@_format_option
def service_uninstall_command(dry_run: bool, output_format: str) -> int:
    """Stop and remove the gateway login service."""
    return _run_service("uninstall", dry_run, output_format)


@cli.group("config", no_args_is_help=True)
def config_group() -> None:
    """Manage persistent CLI configuration."""


@config_group.command("reset")
@_dry_run_option
@_format_option
def config_reset_command(dry_run: bool, output_format: str) -> int:
    """Remove saved settings while preserving conversations and notes."""
    return _reset_config(dry_run, output_format)


def _reset_config(dry_run: bool, output_format: str) -> int:
    path = config.config_path()
    payload = {
        "ok": True,
        "dry_run": dry_run,
        "action": "reset-config",
        "path": str(path),
        "existed": path.exists(),
        "preserved": ["conversations", "notes", "reminders"],
    }
    if dry_run:
        if output_format == "json":
            _json(payload)
        else:
            ui.info(f"dry run: would remove {path} · conversations and notes remain")
        return 0
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        message = f"could not reset config: {exc}; existing settings were preserved"
        if output_format == "json":
            _json({"ok": False, "error": message})
        else:
            ui.error(message)
        return 1
    if output_format == "json":
        _json(payload)
    elif payload["existed"]:
        ui.info(f"removed {path}; conversations and notes were preserved")
    else:
        ui.info("no saved config to reset")
    return 0


def _run_classic_cli(
    *,
    prompt: str | None,
    model: str | None,
    host: str | None,
    cwd: Path | None,
    temperature: float | None,
    name: str | None,
    yolo: bool | None,
    continue_last: bool,
    resume_id: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
    permission_mode: str | None,
    append_system_prompt: str | None,
    doctor: bool,
    list_sessions: bool,
    search: str | None,
    context_ref: str | None,
    compact_ref: str | None,
    setup: bool,
    login_service: str | None,
    logout_service: str | None,
    token_stdin: bool,
    completion: str | None,
    serve: bool,
    port: int | None,
    install_service: bool,
    uninstall_service: bool,
    reset_config: bool,
    threshold: float,
    workspace_roots: tuple[Path, ...],
    lan: bool | None,
    dry_run: bool,
    json_output: bool,
    output_format: str | None,
) -> int:
    """Dispatch the familiar flag-driven interface through validated Click options."""
    if json_output and output_format not in (None, "json"):
        raise click.UsageError("--json cannot be combined with --format text")
    selected_format = "json" if json_output else output_format or "text"

    modes = [
        ("-p/--prompt", prompt is not None),
        ("--doctor", doctor),
        ("--sessions", list_sessions),
        ("--search", search is not None),
        ("--context", context_ref is not None),
        ("--compact", compact_ref is not None),
        ("--setup", setup),
        ("--login", login_service is not None),
        ("--logout", logout_service is not None),
        ("--completion", completion is not None),
        ("--serve", serve),
        ("--install-service", install_service),
        ("--uninstall-service", uninstall_service),
        ("--reset-config", reset_config),
    ]
    selected_modes = [label for label, enabled in modes if enabled]
    if len(selected_modes) > 1:
        raise click.UsageError("choose one mode at a time: " + ", ".join(selected_modes))
    selected_mode = selected_modes[0] if selected_modes else None

    runtime_overrides = [
        ("--model", model is not None),
        ("--host", host is not None),
        ("--cwd", cwd is not None),
        ("--temperature", temperature is not None),
        ("--name", name is not None),
        ("--yolo/--no-yolo", yolo is not None),
    ]
    if selected_mode not in (None, "-p/--prompt", "--serve", "--setup"):
        incompatible = [label for label, supplied in runtime_overrides if supplied]
        if incompatible:
            raise click.UsageError(f"{', '.join(incompatible)} cannot be used with {selected_mode}")
    if setup:
        setup_incompatible = [
            label
            for label, supplied in runtime_overrides
            if supplied and label not in ("--model", "--name")
        ]
        if setup_incompatible:
            raise click.UsageError(f"{', '.join(setup_incompatible)} cannot be used with --setup")
    if dry_run and selected_mode in (
        "--doctor",
        "--sessions",
        "--search",
        "--context",
        "--completion",
    ):
        raise click.UsageError(f"--dry-run has no effect with {selected_mode}")
    if token_stdin and login_service is None:
        raise click.UsageError("--token-stdin requires --login SERVICE")
    if port is not None and not serve:
        raise click.UsageError("--port requires --serve")
    if (workspace_roots or lan is not None) and not setup:
        raise click.UsageError("--workspace-root and --lan/--no-lan require --setup")

    if completion is not None:
        if selected_format == "json":
            raise click.UsageError("--completion emits shell source and cannot use JSON output")
        from .completion import script

        click.echo(script(completion))
        return 0

    if install_service or uninstall_service:
        action = "install" if install_service else "uninstall"
        return _run_service(action, dry_run, selected_format)

    if reset_config:
        return _reset_config(dry_run, selected_format)

    cfg = config.load()

    if setup:
        if selected_format == "json":
            raise click.UsageError("--setup is interactive; omit --json/--format json")
        prepared, _changes = _prepare_setup(
            cfg,
            model=model,
            name=name,
            workspace_roots=workspace_roots,
            lan=lan,
        )
        try:
            return 0 if _setup_wizard(prepared, dry_run=dry_run) else 1
        except EOFError:
            ui.error("setup input ended; no settings were changed")
            return 1
        except KeyboardInterrupt:
            print()
            ui.warn("setup cancelled; no settings were changed")
            return 1

    if doctor:
        from .diagnostics import run

        report = run(cfg)
        if selected_format == "json":
            _json(report)
        else:
            print()
            ui.heading("Diagnostics")
            print()
            for check in report["checks"]:
                ui.list_item(
                    check["name"],
                    detail=check["detail"],
                    marker="✓" if check["ok"] else "×",
                    active=bool(check["ok"]),
                )
        return 0 if report["ok"] else 1

    if list_sessions:
        rows = sessions.list_all()
        if selected_format == "json":
            _json({"ok": True, "sessions": rows})
        else:
            _print_sessions()
        return 0

    if search is not None:
        found = _search_sessions(search)
        if selected_format == "json":
            _json({"ok": True, "query": search, "sessions": found})
        elif not found:
            ui.info("no matching conversations")
        else:
            print()
            ui.heading("Search results")
            print()
            for item in found:
                ui.list_item(item["title"], detail=item["id"])
        return 0

    if context_ref is not None or compact_ref is not None:
        session_ref = compact_ref or context_ref or ""
        return _print_context(
            session_ref,
            threshold,
            compact=compact_ref is not None,
            dry_run=dry_run,
            as_json=selected_format == "json",
            context_limit=command_utils.context_window(cfg),
        )

    if login_service is not None:
        if dry_run:
            secret = "dry-run-placeholder"
        elif token_stdin:
            secret = click.get_text_stream("stdin").read()
        else:
            import getpass

            try:
                secret = getpass.getpass(f"{login_service} key: ")
            except (EOFError, KeyboardInterrupt):
                ui.error("login cancelled; no settings were changed")
                return 1
        return _credential_mode(
            cfg,
            login_service,
            login=True,
            secret=secret,
            dry_run=dry_run,
            as_json=selected_format == "json",
        )

    if logout_service is not None:
        return _credential_mode(
            cfg,
            logout_service,
            login=False,
            dry_run=dry_run,
            as_json=selected_format == "json",
        )

    if name and not dry_run:
        persisted = copy.deepcopy(cfg)
        config.set_value(persisted, "user_name", name)
        try:
            config.save(persisted)
        except (OSError, TypeError, ValueError) as exc:
            message = f"could not save name: {exc}; no settings were changed"
            if selected_format == "json":
                _json({"ok": False, "error": message})
            else:
                ui.error(message)
            return 1

    options = RuntimeOptions(
        mode="serve" if serve else "run" if prompt is not None else "chat",
        model=model,
        host=host,
        cwd=cwd,
        temperature=temperature,
        name=name,
        yolo=yolo,
        prompt=prompt,
        port=port,
        dry_run=dry_run,
        continue_last=continue_last,
        resume_id=resume_id,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode=permission_mode,
        append_system_prompt=append_system_prompt,
    )
    if serve and dry_run:
        return _serve_preview(options, selected_format)
    if serve and selected_format == "json":
        raise click.UsageError("--json requires --dry-run with the long-running gateway")
    if selected_format == "stream-json" and prompt is None:
        raise click.UsageError("--format stream-json needs -p/--prompt")
    if prompt is not None and selected_format == "stream-json":
        return _run_stream_json(options)
    if prompt is not None and selected_format == "json":
        return _run_json(options)
    if prompt is None and not serve and selected_format == "json":
        raise click.UsageError("interactive mode cannot emit JSON; use -p PROMPT")
    return _run_runtime(options)


def _json_requested(argv: list[str] | None) -> bool:
    """Detect an explicit JSON request without trying to parse Click's argv twice."""
    tokens = list(sys.argv[1:] if argv is None else argv)
    for index, token in enumerate(tokens):
        if token == "--":
            break
        if token == "--json":
            return True
        if token == "--format" and index + 1 < len(tokens):
            return tokens[index + 1].casefold() == "json"
        if token.startswith("--format="):
            return token.partition("=")[2].casefold() == "json"
    return False


def _json_error(message: str, exit_code: int, context: click.Context | None = None) -> None:
    payload: dict[str, Any] = {"ok": False, "error": message, "exit_code": exit_code}
    if context is not None:
        payload["usage"] = context.get_usage().strip()
    _json(payload)


def main(argv: list[str] | None = None) -> int:
    """Run the Click app and convert every failure into a stable exit code."""
    global _DEBUG_ACTIVE
    _DEBUG_ACTIVE = False
    # Configure logging up front too: Click can fail before cli() runs, and a
    # crash during option parsing must not dump a traceback into the terminal.
    _logs.setup(debug="--debug" in (argv if argv is not None else sys.argv[1:]))
    json_output = _json_requested(argv)
    try:
        result = cli.main(args=argv, prog_name="cagentic", standalone_mode=False)
        return int(result or 0)
    except click.exceptions.NoArgsIsHelpError as exc:
        if exc.ctx is not None:
            click.echo(exc.ctx.get_help())
        return 2
    except click.UsageError as exc:
        if json_output:
            _json_error(exc.format_message(), 2, exc.ctx)
            return 2
        if exc.ctx is not None:
            click.echo(exc.ctx.get_usage(), err=True)
        ui.error(exc.format_message())
        click.echo("  Run the command with --help for valid syntax.", err=True)
        return 2
    except click.ClickException as exc:
        if json_output:
            _json_error(exc.format_message(), 1)
            return 1
        ui.error(exc.format_message())
        return 1
    except click.Abort:
        if json_output:
            _json_error("cancelled", 1)
            return 1
        ui.error("cancelled")
        return 1
    except KeyboardInterrupt:
        if json_output:
            _json_error("interrupted", 1)
            return 1
        ui.error("interrupted")
        return 1
    except Exception as exc:
        if _DEBUG_ACTIVE:
            raise
        if json_output:
            _json_error(f"unexpected {type(exc).__name__}: {exc}", 1)
            return 1
        ui.error(f"unexpected {type(exc).__name__}: {exc}")
        click.echo("  Re-run with 'cagentic --debug …' to see the traceback.", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
