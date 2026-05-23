"""Tool implementations the model can call.

Every tool receives a dict of arguments and returns a string result that gets
fed back to the model as the tool's output.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import diff as _diff
from . import documents as _documents
from . import notes as _notes
from . import reminders as _reminders
from . import ui

MAX_OUTPUT_CHARS = 16000


def _truncate(s: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[truncated, {len(s) - limit} more chars]"


def _resolve(path: str, root: Path) -> Path:
    expanded = os.path.expanduser(os.path.expandvars(path))
    p = Path(expanded)
    if not p.is_absolute():
        p = root / p
    return p


@dataclass
class ToolContext:
    root: Path
    yolo: bool = False
    github_token: str | None = None
    insecure_ssl: bool = False
    # Plumbing populated by the QueryEngine.
    state: object | None = None
    engine: object | None = None
    background: object | None = None
    tasks: object | None = None
    read_cache: dict | None = None

    def confirm(self, action: str, detail: str) -> bool:
        """Approval hook for inside a tool.

        Every tool is already gated by can_use_tool() + the permission
        resolver BEFORE it runs — the terminal resolver prompts in the
        REPL, the gateway resolver prompts in the web UI. So by the time a
        tool body calls confirm(), the action is approved; this returns
        True rather than prompting again (a second input() prompt would
        double-ask in the REPL and block the thread in the web gateway).
        """
        return True


# ============================================================================
# Files: read, write, edit, replace_lines, list_dir, grep, glob, set_workspace
# ============================================================================

def t_read_file(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    start = int(args.get("start_line", 1))
    end = args.get("end_line")
    p = _resolve(path, ctx.root)
    if not p.exists():
        return f"ERROR: file not found: {path}"
    if not p.is_file():
        return f"ERROR: not a file: {path}"

    abs_path = str(p.resolve())
    cache = ctx.read_cache if isinstance(ctx.read_cache, dict) else None
    state = getattr(ctx, "state", None)
    already_read = (
        (cache is not None and abs_path in cache)
        or (state is not None and abs_path in getattr(state, "files_read", set()))
    )
    if already_read:
        return (
            f"[CACHED — you already read {path} earlier in this turn. "
            f"Scroll back instead of re-reading.]"
        )

    # PDF / DOCX get their text extracted; everything else is read as text.
    if _documents.is_document(p):
        try:
            text = _documents.extract_text(p)
        except _documents.DocumentError as e:
            return f"ERROR: {e}"
        except Exception as e:
            return f"ERROR: could not read {path}: {type(e).__name__}: {e}"
        kind = p.suffix.lower().lstrip(".")
    else:
        try:
            text = p.read_text(errors="replace")
        except Exception as e:
            return f"ERROR: {e}"
        kind = None
    lines = text.splitlines()
    s = max(1, start) - 1
    e = len(lines) if end is None else min(len(lines), int(end))
    selected = lines[s:e]
    numbered = "\n".join(f"{i + s + 1:>5}  {ln}" for i, ln in enumerate(selected))
    if kind:
        header = f"{path}  ({len(lines)} lines of text extracted from {kind})"
    else:
        header = f"{path}  ({len(lines)} lines)"
    out = _truncate(f"{header}\n{numbered}")
    if cache is not None:
        cache[abs_path] = out
    if state is not None:
        files_read = set(getattr(state, "files_read", set()) or set())
        files_read.add(abs_path)
        state.update(files_read=files_read)
    return out


MAX_EDIT_HISTORY = 50


def _record_edit(ctx: ToolContext, path: Path, before: str, after: str, op: str) -> None:
    state = getattr(ctx, "state", None)
    if state is not None:
        import time as _t
        hist: list = getattr(state, "edit_history", None) or []
        hist.append({
            "ts": _t.time(),
            "path": str(path.resolve()),
            "before": before,
            "after": after,
            "op": op,
        })
        del hist[:-MAX_EDIT_HISTORY]
        state.update(edit_history=hist)
    rp = str(path.resolve())
    cache = getattr(ctx, "read_cache", None)
    if isinstance(cache, dict):
        cache.pop(rp, None)
    if state is not None:
        files_read = set(getattr(state, "files_read", set()) or set())
        if rp in files_read:
            files_read.discard(rp)
            state.update(files_read=files_read)


def t_write_file(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    content = args.get("content")
    if content is None:
        return "ERROR: missing argument 'content'"
    if not isinstance(content, str):
        return f"ERROR: 'content' must be a string, got {type(content).__name__}"
    p = _resolve(path, ctx.root)
    existed = p.exists()
    old_text = p.read_text(errors="replace") if existed else ""
    if existed and old_text.strip() and not content.strip() and not args.get("allow_empty"):
        return (
            f"ERROR: refusing to write empty content to existing file {path}. "
            f"If you really meant to empty it, pass allow_empty=true."
        )
    detail = f"{'overwrite' if existed else 'create'} {path} ({len(content)} bytes)"
    if not ctx.confirm("file write", detail):
        return "ERROR: user denied write"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _record_edit(ctx, p, old_text, content, "write")
    adds, dels = _diff.stats(old_text, content)
    return f"OK: wrote {path} +{adds} -{dels}"


def _norm_eol(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _fuzzy_span(text: str, old: str):
    text_lines = text.split("\n")
    old_lines = old.split("\n")
    while len(old_lines) > 1 and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    while len(old_lines) > 1 and old_lines[0] == "":
        old_lines = old_lines[1:]
    n = len(old_lines)
    if n == 0:
        return None
    norm_old = [ln.rstrip() for ln in old_lines]
    hits = [
        i for i in range(len(text_lines) - n + 1)
        if [ln.rstrip() for ln in text_lines[i:i + n]] == norm_old
    ]
    return (hits[0], hits[0] + n) if len(hits) == 1 else None


def _closest_region(text: str, old: str) -> str:
    import difflib
    first = ""
    for ln in old.split("\n"):
        if ln.strip():
            first = ln.strip()
            break
    if not first:
        return ""
    text_lines = text.split("\n")
    best_i, best = 0, 0.0
    for i, ln in enumerate(text_lines):
        score = difflib.SequenceMatcher(None, ln.strip(), first).ratio()
        if score > best:
            best, best_i = score, i
    if best < 0.4:
        return ""
    lo, hi = max(0, best_i - 2), min(len(text_lines), best_i + 6)
    return "\n".join(f"{j + 1:>5}  {text_lines[j]}" for j in range(lo, hi))


def _read_text_robust(p: Path) -> str:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except (UnicodeDecodeError, OSError):
        text = p.read_text(errors="replace")
    if text and text[0] == "﻿":
        text = text[1:]
    return text


def t_edit_file(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    old = args["old_string"]
    new = args["new_string"]
    replace_all = bool(args.get("replace_all", False))
    p = _resolve(path, ctx.root)
    if not p.exists():
        return f"ERROR: file not found: {path}"
    raw = _read_text_robust(p)

    count = raw.count(old)
    if count == 1 or (count > 1 and replace_all):
        if not ctx.confirm("file edit", f"{path}: replace {count} occurrence(s)"):
            return "ERROR: user denied edit"
        new_text = raw.replace(old, new) if replace_all else raw.replace(old, new, 1)
        if raw.strip() and not new_text.strip() and not args.get("allow_empty"):
            return f"ERROR: refusing to empty {path}. Pass allow_empty=true to confirm."
        p.write_text(new_text, encoding="utf-8")
        _record_edit(ctx, p, raw, new_text, "edit")
        adds, dels = _diff.stats(raw, new_text)
        return f"OK: edited {path} +{adds} -{dels}"
    if count > 1:
        return f"ERROR: old_string matches {count} times — pass replace_all=true or supply more context"

    # Recovery: line-ending + whitespace tolerant
    text = _norm_eol(raw)
    old_n = _norm_eol(old)
    new_n = _norm_eol(new)
    if text.count(old_n) == 1:
        if not ctx.confirm("file edit", f"{path}: replace 1 occurrence (line-ending normalized)"):
            return "ERROR: user denied edit"
        new_text = text.replace(old_n, new_n, 1)
    else:
        span = _fuzzy_span(text, old_n)
        if span is None:
            lines = len(text.splitlines())
            state = getattr(ctx, "state", None)
            fail_count = 1
            if state is not None:
                fails = dict(getattr(state, "edit_fails", {}) or {})
                rp = str(p.resolve())
                fails[rp] = fails.get(rp, 0) + 1
                fail_count = fails[rp]
                state.update(edit_fails=fails)
            msg = (
                f"ERROR: old_string not found in {path} (file has {lines} lines). "
                f"old_string must match the file EXACTLY, including indentation."
            )
            hint = _closest_region(text, old_n)
            if hint:
                msg += f"\n\nClosest region:\n{hint}"
            if fail_count >= 2:
                msg += (
                    f"\n\nSwitch to replace_lines(path, start_line, end_line, new_content) "
                    f"— surgical line-range edit, no string matching."
                )
            return msg
        i, j = span
        if not ctx.confirm("file edit", f"{path}: replace lines {i + 1}-{j} (whitespace-tolerant)"):
            return "ERROR: user denied edit"
        file_lines = text.split("\n")
        new_text = "\n".join(file_lines[:i] + new_n.split("\n") + file_lines[j:])

    if raw.strip() and not new_text.strip() and not args.get("allow_empty"):
        return f"ERROR: refusing to empty {path}. Pass allow_empty=true."
    p.write_text(new_text, encoding="utf-8")
    _record_edit(ctx, p, raw, new_text, "edit")
    adds, dels = _diff.stats(text, new_text)
    return f"OK: edited {path} +{adds} -{dels}"


def t_replace_lines(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    start = args.get("start_line")
    end = args.get("end_line")
    new_content = args.get("new_content", "")
    if start is None or end is None:
        return "ERROR: replace_lines requires start_line and end_line (1-indexed, inclusive)"
    p = _resolve(path, ctx.root)
    if not p.exists():
        return f"ERROR: file not found: {path}"
    raw = _read_text_robust(p)
    lines = raw.splitlines(keepends=True)
    s = max(1, int(start)) - 1
    e = min(len(lines), int(end))
    if s >= len(lines):
        return f"ERROR: start_line {start} is past end of file ({len(lines)} lines)"
    if e < s + 1:
        return f"ERROR: end_line ({end}) must be >= start_line ({start})"
    if not ctx.confirm("file edit (replace_lines)", f"{path}: lines {s + 1}-{e}"):
        return "ERROR: user denied edit"
    eol = "\r\n" if (raw and "\r\n" in raw and raw.count("\r\n") >= raw.count("\n") / 2) else "\n"
    new_chunk = new_content if new_content.endswith(("\n", "\r")) else new_content + eol
    new_lines = lines[:s] + [new_chunk] + lines[e:]
    new_text = "".join(new_lines)
    if raw.strip() and not new_text.strip() and not args.get("allow_empty"):
        return f"ERROR: refusing to empty {path}. Pass allow_empty=true."
    p.write_text(new_text, encoding="utf-8")
    _record_edit(ctx, p, raw, new_text, "replace_lines")
    adds, dels = _diff.stats(raw, new_text)
    return f"OK: replaced {path}:{s + 1}-{e} +{adds} -{dels}"


def t_list_dir(args: dict, ctx: ToolContext) -> str:
    path = args.get("path", ".")
    p = _resolve(path, ctx.root).resolve()
    if not p.exists():
        return f"ERROR: not found: {path}"
    if not p.is_dir():
        return f"ERROR: not a directory: {path}"
    rows = []
    for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
        if entry.name.startswith("."):
            continue
        kind = "dir " if entry.is_dir() else "file"
        try:
            size = entry.stat().st_size if entry.is_file() else 0
        except OSError:
            size = 0
        rows.append(f"  {kind}  {size:>9}  {entry.name}")
    header = f"{path}  ({len(rows)} entries)"
    body = "\n".join(rows) if rows else "(empty or only dotfiles)"
    return _truncate(f"{header}\n{body}")


def t_grep(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern") or args.get("query") or args.get("regex")
    if not pattern:
        return "ERROR: missing argument 'pattern'"
    path = args.get("path", ".")
    case_insensitive = bool(args.get("case_insensitive", False))
    p = _resolve(path, ctx.root)

    home = Path.home().resolve()
    target = p.resolve()
    if target == home or target == Path(home.anchor):
        return (
            f"ERROR: grep target {p} is too broad. Specify a narrower path."
        )

    import shutil as _sh
    rg = _sh.which("rg")
    if rg and p.exists():
        cmd = [rg, "-n", "--no-heading", "--color=never", "-m", "200"]
        if case_insensitive:
            cmd.append("-i")
        cmd.extend(["--", pattern, str(p)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "ERROR: rg timed out after 30s"
        if proc.returncode in (0, 1):
            out = proc.stdout.strip()
            return _truncate(out) if out else "(no matches)"

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"ERROR: bad regex: {e}"
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    matches: list[str] = []
    targets = [p] if p.is_file() else list(p.rglob("*")) if p.is_dir() else []
    scanned = 0
    MAX_SCAN = 2000
    for f in targets:
        if not f.is_file():
            continue
        if any(part in skip_dirs for part in f.parts):
            continue
        if any(s in f.parts for s in (".cache", "site-packages", ".tox", ".pytest_cache")):
            continue
        scanned += 1
        if scanned > MAX_SCAN:
            matches.append(f"…[scanned {MAX_SCAN}+ files, stopping]")
            return _truncate("\n".join(matches))
        try:
            with f.open("r", errors="replace") as fp:
                for i, line in enumerate(fp, 1):
                    if rx.search(line):
                        rel = f.relative_to(ctx.root) if f.is_relative_to(ctx.root) else f
                        matches.append(f"{rel}:{i}: {line.rstrip()}")
                        if len(matches) >= 200:
                            matches.append("…[200 match cap]")
                            return _truncate("\n".join(matches))
        except (OSError, UnicodeDecodeError):
            continue
    return _truncate("\n".join(matches) if matches else "(no matches)")


def t_glob(args: dict, ctx: ToolContext) -> str:
    import fnmatch
    pattern = args["pattern"]
    base = _resolve(args.get("path", "."), ctx.root)
    if not base.exists() or not base.is_dir():
        return f"ERROR: not a directory: {base}"
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    matches: list[str] = []
    if "**" in pattern or "/" in pattern:
        for p in base.rglob("*"):
            if any(part in skip_dirs for part in p.parts):
                continue
            rel = p.relative_to(base)
            if fnmatch.fnmatch(str(rel), pattern):
                matches.append(str(rel))
    else:
        for p in base.iterdir():
            if fnmatch.fnmatch(p.name, pattern):
                matches.append(p.name)
    matches.sort()
    return _truncate("\n".join(matches[:500]) if matches else "(no matches)")


def t_set_workspace(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    create = bool(args.get("create", False))
    p = _resolve(path, ctx.root)
    if not p.exists():
        if not create:
            return f"ERROR: directory does not exist: {p}. Pass create=true to mkdir it."
        p.mkdir(parents=True, exist_ok=True)
    elif not p.is_dir():
        return f"ERROR: not a directory: {p}"
    ctx.root = p.resolve()
    return f"OK: workspace set to {ctx.root}"


# ============================================================================
# Notes — persistent knowledge base
# ============================================================================

def t_note_write(args: dict, ctx: ToolContext) -> str:
    name = args.get("name")
    body = args.get("body") or args.get("content")
    append = bool(args.get("append", False))
    if not name or body is None:
        return "ERROR: note_write requires 'name' and 'body'"
    if not ctx.confirm("save note", f"{name} ({len(body)} chars, {'append' if append else 'overwrite'})"):
        return "ERROR: user denied"
    note = _notes.write(name, body, append=append)
    return f"OK: saved note '{note.name}' ({len(note.body)} chars) at {note.path}"


def t_note_get(args: dict, ctx: ToolContext) -> str:
    name = args.get("name")
    if not name:
        return "ERROR: note_get requires 'name'"
    note = _notes.get(name)
    if not note:
        return f"(no note named '{name}')"
    return _truncate(f"--- {note.name} ({note.path}) ---\n{note.body}")


def t_note_list(args: dict, ctx: ToolContext) -> str:
    items = _notes.list_all()
    if not items:
        return "(no notes saved yet)"
    return "\n".join(n.short() for n in items[:80])


def t_note_search(args: dict, ctx: ToolContext) -> str:
    query = args.get("query") or args.get("q")
    if not query:
        return "ERROR: note_search requires 'query'"
    hits = _notes.search(query)
    if not hits:
        return f"(no notes match '{query}')"
    out: list[str] = []
    for note, lines in hits[:20]:
        out.append(f"--- {note.name} ---")
        for ln in lines:
            out.append(f"  {ln}")
    return _truncate("\n".join(out))


def t_note_delete(args: dict, ctx: ToolContext) -> str:
    name = args.get("name")
    if not name:
        return "ERROR: note_delete requires 'name'"
    if not ctx.confirm("delete note", name):
        return "ERROR: user denied"
    return "OK: deleted" if _notes.delete(name) else f"ERROR: no note named '{name}'"


# ============================================================================
# Reminders — persistent to-do across sessions
# ============================================================================

def t_reminder_add(args: dict, ctx: ToolContext) -> str:
    text = args.get("text")
    if not text:
        return "ERROR: reminder_add requires 'text'"
    when_raw = args.get("when")
    due_at = _reminders.parse_when(when_raw) if when_raw else None
    if when_raw and due_at is None:
        # Not fatal — keep the reminder, just without a due time, and note it.
        note = f"  (couldn't parse 'when' = {when_raw!r}; saved without due time)"
    else:
        note = ""
    tags = args.get("tags") or []
    if not ctx.confirm("add reminder", text[:80] + (f" @ {when_raw}" if when_raw else "")):
        return "ERROR: user denied"
    r = _reminders.add(text, due_at=due_at, tags=list(tags))
    return f"OK: {r.short().strip()}{note}"


def t_reminder_list(args: dict, ctx: ToolContext) -> str:
    include_done = bool(args.get("include_done", False))
    status = args.get("status")
    rems = _reminders.list_all(status=status, include_done=include_done)
    if not rems:
        return "(no reminders)"
    return "\n".join(r.short() for r in rems[:80])


def t_reminder_done(args: dict, ctx: ToolContext) -> str:
    rid = args.get("id")
    if not rid:
        return "ERROR: reminder_done requires 'id'"
    r = _reminders.update(rid, status="done")
    return f"OK: marked done — {r.short().strip()}" if r else f"ERROR: no reminder {rid}"


def t_reminder_delete(args: dict, ctx: ToolContext) -> str:
    rid = args.get("id")
    if not rid:
        return "ERROR: reminder_delete requires 'id'"
    if not ctx.confirm("delete reminder", rid):
        return "ERROR: user denied"
    return "OK: deleted" if _reminders.delete(rid) else f"ERROR: no reminder {rid}"


def t_reminder_update(args: dict, ctx: ToolContext) -> str:
    rid = args.get("id")
    if not rid:
        return "ERROR: reminder_update requires 'id'"
    changes: dict = {}
    if "text" in args:
        changes["text"] = args["text"]
    if "status" in args:
        changes["status"] = args["status"]
    if "when" in args:
        due = _reminders.parse_when(args["when"]) if args["when"] else None
        changes["due_at"] = due
    if "tags" in args:
        changes["tags"] = list(args["tags"] or [])
    if not changes:
        return "ERROR: nothing to update"
    r = _reminders.update(rid, **changes)
    return f"OK: {r.short().strip()}" if r else f"ERROR: no reminder {rid}"


# ============================================================================
# MCP — Model Context Protocol bridge
# ============================================================================

def _mcp_manager(ctx: ToolContext):
    """Lazy-init the MCPManager on the engine state."""
    state = getattr(ctx, "state", None)
    if state is None:
        return None
    if getattr(state, "mcp", None) is None:
        from .mcp_client import MCPManager
        engine = getattr(ctx, "engine", None)
        cfg = engine.config if engine is not None else {}
        state.mcp = MCPManager(cfg or {})
    return state.mcp


def t_mcp_list_servers(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    names = mgr.names()
    if not names:
        return ("(no MCP servers configured — add one under mcp.servers in "
                "~/.config/cagentic/config.json, e.g. notion / gdrive / slack)")
    return "\n".join(f"  - {n}" for n in names)


def t_mcp_list_tools(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    server = args.get("server")
    if not server:
        return "ERROR: mcp_list_tools requires 'server'"
    try:
        tools = mgr.list_tools(server)
    except Exception as e:
        return f"ERROR: {e}"
    if not tools:
        return f"(server '{server}' exposes no tools)"
    lines = []
    for t in tools[:60]:
        n = t.get("name", "?")
        d = (t.get("description") or "").splitlines()[0][:160]
        lines.append(f"  - {n}  —  {d}")
    return "\n".join(lines)


def t_mcp_call(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    server = args.get("server")
    name = args.get("tool") or args.get("name")
    arguments = args.get("arguments") or {}
    if not server or not name:
        return "ERROR: mcp_call requires 'server' and 'tool'"
    if not ctx.confirm("MCP call", f"{server}/{name}  {str(arguments)[:80]}"):
        return "ERROR: user denied"
    try:
        from .mcp_client import format_tool_result
        result = mgr.call_tool(server, name, arguments)
    except Exception as e:
        return f"ERROR: {e}"
    return _truncate(format_tool_result(result))


def t_mcp_list_resources(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    server = args.get("server")
    if not server:
        return "ERROR: mcp_list_resources requires 'server'"
    try:
        items = mgr.list_resources(server)
    except Exception as e:
        return f"ERROR: {e}"
    if not items:
        return f"(server '{server}' exposes no resources)"
    lines = []
    for r in items[:60]:
        uri = r.get("uri", "?")
        name = r.get("name") or ""
        mime = r.get("mimeType") or ""
        lines.append(f"  - {uri}  {name}  [{mime}]".rstrip())
    return "\n".join(lines)


def t_mcp_read_resource(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    server = args.get("server")
    uri = args.get("uri")
    if not server or not uri:
        return "ERROR: mcp_read_resource requires 'server' and 'uri'"
    try:
        res = mgr.read_resource(server, uri)
    except Exception as e:
        return f"ERROR: {e}"
    contents = res.get("contents") or []
    parts: list[str] = []
    for c in contents:
        if not isinstance(c, dict):
            continue
        if "text" in c:
            parts.append(c["text"])
        elif "blob" in c:
            parts.append(f"[binary blob: {len(c['blob'])} chars b64]")
    return _truncate("\n".join(parts) if parts else "(empty resource)")


# ============================================================================
# Browser — control Chrome through the companion extension
# ============================================================================

def _browser(ctx: ToolContext):
    """Get (lazily creating + starting) the BrowserBridge on the state."""
    state = getattr(ctx, "state", None)
    if state is None:
        return None
    if getattr(state, "browser", None) is None:
        from .browser import BrowserBridge
        engine = getattr(ctx, "engine", None)
        cfg = (engine.config if engine is not None else {}) or {}
        port = int((cfg.get("browser") or {}).get("port", 8765))
        bridge = BrowserBridge(port=port)
        bridge.start()
        state.browser = bridge
    return state.browser


def _browser_setup_hint(bridge) -> str:
    return (
        f"the Cagentic Chrome extension isn't connected (bridge listening on "
        f"port {bridge.port}). To connect it: open chrome://extensions, turn on "
        f"Developer mode, click 'Load unpacked', and select the 'extension/' "
        f"folder in the Cagentic repo. Run /browser for the exact path."
    )


def t_browser_status(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    if b.error:
        return f"ERROR: browser bridge could not start — {b.error}"
    if b.is_connected():
        return f"OK: the Chrome extension is connected (bridge on port {b.port})."
    return _browser_setup_hint(b)


def t_browser_tabs(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    r = b.send("tabs", {})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    tabs = r.get("result") or []
    if not tabs:
        return "(no open browser tabs)"
    lines = []
    for t in tabs:
        mark = "*" if t.get("active") else " "
        title = (t.get("title") or "")[:60]
        lines.append(f"  [{mark}] tab {t.get('id')}  {title}  — {t.get('url', '')}")
    return _truncate("\n".join(lines))


def t_browser_read(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    r = b.send("read", {"tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    return _truncate(
        f"{res.get('title', '')}\n{res.get('url', '')}\n\n{res.get('text', '')}"
    )


def t_browser_open(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    url = args.get("url")
    if not url:
        return "ERROR: browser_open requires 'url'"
    if not ctx.confirm("open a browser tab", url):
        return "ERROR: user denied"
    r = b.send("open", {"url": url, "active": args.get("active", True)})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    return f"OK: opened tab {res.get('id')} → {res.get('url', url)}"


def t_browser_navigate(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    url = args.get("url")
    if not url:
        return "ERROR: browser_navigate requires 'url'"
    if not ctx.confirm("navigate the browser", url):
        return "ERROR: user denied"
    r = b.send("navigate", {"url": url, "tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return f"OK: navigated to {url}"


def t_browser_click(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    selector = args.get("selector")
    text = args.get("text")
    if not selector and not text:
        return "ERROR: browser_click requires 'selector' or 'text'"
    target = selector or f"text:{text}"
    if not ctx.confirm("click in the browser", target):
        return "ERROR: user denied"
    r = b.send("click", {"selector": selector, "text": text, "tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'click failed')}"
    return f"OK: clicked {res.get('clicked', target)}"


def t_browser_fill(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    selector = args.get("selector")
    value = args.get("value")
    if not selector or value is None:
        return "ERROR: browser_fill requires 'selector' and 'value'"
    if not ctx.confirm("fill a browser field", f"{selector} = {str(value)[:60]}"):
        return "ERROR: user denied"
    r = b.send("fill", {"selector": selector, "value": value, "tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'fill failed')}"
    return f"OK: filled {selector}"


def t_browser_eval(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    code = args.get("code")
    if not code:
        return "ERROR: browser_eval requires 'code'"
    if not ctx.confirm("run JavaScript in the browser", code[:120]):
        return "ERROR: user denied"
    r = b.send("eval", {"code": code, "tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'eval failed')}"
    return _truncate(f"OK: {res.get('value', '')}")


def t_browser_close(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    tab_id = args.get("tab_id")
    if tab_id is None:
        return "ERROR: browser_close requires 'tab_id' (use browser_tabs to find it)"
    if not ctx.confirm("close a browser tab", f"tab {tab_id}"):
        return "ERROR: user denied"
    r = b.send("close", {"tab_id": tab_id})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return f"OK: closed tab {tab_id}"


# ============================================================================
# Web — fetch + search
# ============================================================================

def t_web_fetch(args: dict, ctx: ToolContext) -> str:
    import requests
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        return "ERROR: url must be http:// or https://"
    timeout = int(args.get("timeout", 20))
    max_bytes = int(args.get("max_bytes", 200_000))
    headers = {"User-Agent": "cagentic/0.1"}
    try:
        r = requests.get(url, timeout=timeout, headers=headers,
                         verify=not ctx.insecure_ssl, stream=True)
    except requests.RequestException as e:
        return f"ERROR: fetch failed: {e}"
    chunks: list[bytes] = []
    seen = 0
    for chunk in r.iter_content(8192):
        chunks.append(chunk)
        seen += len(chunk)
        if seen >= max_bytes:
            break
    raw = b"".join(chunks)
    try:
        body = raw.decode(r.encoding or "utf-8", errors="replace")
    except Exception:
        body = raw.decode("utf-8", errors="replace")
    # Optional: strip HTML tags for readability.
    if args.get("text_only") and ("<html" in body.lower() or "<body" in body.lower()):
        body = _strip_html(body)
    return _truncate(f"HTTP {r.status_code}  {url}\n{body}")


_HTML_TAG_RX = re.compile(r"<[^>]+>")
_HTML_SCRIPT_RX = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_WS_RX = re.compile(r"\n[ \t]*\n[ \t]*\n+")


def _strip_html(html: str) -> str:
    text = _HTML_SCRIPT_RX.sub("", html)
    text = _HTML_TAG_RX.sub("", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = _HTML_WS_RX.sub("\n\n", text)
    return text.strip()


def t_web_search(args: dict, ctx: ToolContext) -> str:
    """DuckDuckGo HTML-frontend scrape (no API key needed)."""
    import requests
    q = args["query"]
    n = int(args.get("limit", 10))
    try:
        r = requests.get(
            "https://duckduckgo.com/html/", params={"q": q},
            headers={"User-Agent": "Mozilla/5.0 cagentic/0.1"},
            timeout=15, verify=not ctx.insecure_ssl,
        )
    except requests.RequestException as e:
        return f"ERROR: search failed: {e}"
    if r.status_code != 200:
        return f"ERROR: HTTP {r.status_code}"
    rx = re.compile(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    items = rx.findall(r.text)
    out: list[str] = []
    for href, title in items[:n]:
        title_text = re.sub(r"<[^>]+>", "", title).strip()
        out.append(f"- {title_text}\n  {href}")
    return _truncate("\n".join(out) if out else "(no results)")


# ============================================================================
# Shell — run_bash (with confirmation) and async variant
# ============================================================================

_ERR_LOC_PATTERNS = [
    re.compile(r'File "([^"]+)", line (\d+)'),
    re.compile(r'-->\s+([^\s:]+):(\d+):\d+'),
    re.compile(r'\(([^()\s]+):(\d+):\d+\)'),
    re.compile(r'([\w./\\+-]+\.\w+):(\d+):\d+'),
    re.compile(r'([\w./\\+-]+\.\w+):(\d+)\b'),
]
_ERR_MSG_RX = re.compile(r'^\s*([A-Z]\w*(?:Error|Exception|Warning|Fault)): ?(.*)$', re.M)


def _analyze_failure(stdout: str, stderr: str) -> str:
    blob = (stderr or "") + "\n" + (stdout or "")
    hints: list[str] = []
    msgs = _ERR_MSG_RX.findall(blob)
    if msgs:
        kind, detail = msgs[-1]
        hints.append(f"{kind}: {detail.strip()[:200]}")
    loc = None
    for pat in _ERR_LOC_PATTERNS:
        found = pat.findall(blob)
        if found:
            loc = found[-1]
            break
    if loc:
        hints.append(f"likely at {loc[0]}:{loc[1]}")
    return ("  ↳ " + "  ·  ".join(hints)) if hints else ""


def t_run_bash(args: dict, ctx: ToolContext) -> str:
    cmd = args["command"]
    timeout = int(args.get("timeout", 60))
    if not ctx.confirm("shell command", cmd):
        return "ERROR: user denied command"
    try:
        with ui.Spinner(f"running: {cmd[:40] + ('…' if len(cmd) > 40 else '')}"):
            proc = subprocess.run(
                cmd, shell=True, cwd=str(ctx.root),
                capture_output=True, text=True, timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {timeout}s"
    status = "PASS" if proc.returncode == 0 else "FAIL"
    parts = [f"{status} (exit code {proc.returncode})"]
    if proc.stdout:
        parts.append(f"--- stdout ---\n{proc.stdout}")
    if proc.stderr:
        parts.append(f"--- stderr ---\n{proc.stderr}")
    if proc.returncode != 0:
        hint = _analyze_failure(proc.stdout, proc.stderr)
        if hint:
            parts.append(hint)
    return _truncate("\n".join(parts))


def t_bash_async(args: dict, ctx: ToolContext) -> str:
    bg = getattr(ctx, "background", None)
    if bg is None:
        return "ERROR: background executor not available"
    cmd = args["command"]
    timeout = int(args.get("timeout", 600))
    if not ctx.confirm("background shell command", cmd):
        return "ERROR: user denied command"
    job_id = bg.submit_bash(cmd, ctx.root, timeout=timeout)
    return f"OK: queued {job_id}  (poll with task_status / task_wait)"


# ============================================================================
# Tasks (light, kept for background-job tracking)
# ============================================================================

def _tasks(ctx: ToolContext):
    return getattr(ctx, "tasks", None)


def t_task_get(args: dict, ctx: ToolContext) -> str:
    tg = _tasks(ctx)
    if tg is None:
        return "ERROR: task graph not available"
    task = tg.get(args["id"])
    if not task:
        return f"ERROR: no task with id {args['id']}"
    import json as _json
    return _json.dumps(task.to_dict(), indent=2)


def t_task_list(args: dict, ctx: ToolContext) -> str:
    tg = _tasks(ctx)
    if tg is None:
        return "ERROR: task graph not available"
    tasks = tg.list(status=args.get("status"))
    if not tasks:
        return "(no tasks)"
    return "\n".join(t.short() for t in tasks[:60])


def t_task_status(args: dict, ctx: ToolContext) -> str:
    bg = getattr(ctx, "background", None)
    if bg is None:
        return "ERROR: background executor not available"
    job = bg.status(args["task_id"])
    if not job:
        return f"ERROR: no background job {args['task_id']}"
    summary = job.result.splitlines()[0][:160] if job.result else ""
    return f"{job.id}  status={job.status}  kind={job.kind}  label={job.label[:80]}\n{summary}"


def t_task_wait(args: dict, ctx: ToolContext) -> str:
    bg = getattr(ctx, "background", None)
    if bg is None:
        return "ERROR: background executor not available"
    timeout = float(args.get("timeout", 60))
    job = bg.wait(args["task_id"], timeout=timeout)
    if not job:
        return f"ERROR: no background job {args['task_id']}"
    if job.status == "running":
        return f"still running after {timeout}s"
    return _truncate(f"{job.id} finished {job.status}\n\n{job.result}")


def t_task_output(args: dict, ctx: ToolContext) -> str:
    bg = getattr(ctx, "background", None)
    tasks = getattr(ctx, "tasks", None)
    tid = args["id"]
    if bg is not None:
        job = bg.status(tid)
        if job:
            return _truncate(f"[bg {job.id} status={job.status}]\n{job.result}")
    if tasks is not None:
        t = tasks.get(tid)
        if t:
            return _truncate(f"[task {t.id} status={t.status}]\n{t.result}")
    return f"ERROR: no task/job with id {tid}"


# ============================================================================
# Interaction, planning, todo, config, sleep
# ============================================================================

def t_ask_user_question(args: dict, ctx: ToolContext) -> str:
    question = args.get("question") or args.get("prompt") or args.get("q")
    if not question:
        return "ERROR: missing argument 'question'"
    options = args.get("options") or []
    # Yolo mode skips APPROVAL prompts — but asking the user a question
    # is a separate channel (the model needs information, not permission),
    # so it's allowed regardless. EOFError still handles non-interactive runs.
    ui.stop_all_spinners()
    import sys as _sys
    if _sys.stdout.isatty():
        _sys.stdout.write("\033[?25h")
        _sys.stdout.flush()
    print()
    ui.warn("? " + question)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    try:
        ans = input("  > ").strip()
    except EOFError:
        return "ERROR: no tty"
    if options and ans.isdigit():
        idx = int(ans) - 1
        if 0 <= idx < len(options):
            return options[idx]
    return ans or "(empty)"


def t_enter_plan_mode(args: dict, ctx: ToolContext) -> str:
    state = getattr(ctx, "state", None)
    if state is None:
        return "ERROR: state not available"
    state.update(plan_mode=True)
    engine = getattr(ctx, "engine", None)
    if engine is not None:
        try:
            engine.refresh_system_prompt()
        except Exception:
            pass
    return "OK: PLAN MODE entered. Mutating tools blocked. Use exit_plan_mode to resume."


def t_exit_plan_mode(args: dict, ctx: ToolContext) -> str:
    state = getattr(ctx, "state", None)
    if state is None:
        return "ERROR: state not available"
    state.update(plan_mode=False)
    engine = getattr(ctx, "engine", None)
    if engine is not None:
        try:
            engine.refresh_system_prompt()
        except Exception:
            pass
    return "OK: plan mode OFF."


def t_todo_write(args: dict, ctx: ToolContext) -> str:
    state = getattr(ctx, "state", None)
    if state is None:
        return "ERROR: state not available"
    items = args.get("items")
    if not isinstance(items, list):
        return "ERROR: items must be a list of {text, status?}"
    todos: list[dict] = []
    for it in items:
        if isinstance(it, str):
            todos.append({"text": it, "status": "pending"})
        elif isinstance(it, dict) and "text" in it:
            todos.append({"text": it["text"], "status": it.get("status", "pending")})
    state.update(todos=todos)
    out = "\n".join(f"  [{t['status'][0]}] {t['text']}" for t in todos)
    return f"OK: {len(todos)} todo(s):\n{out}"


def t_tool_search(args: dict, ctx: ToolContext) -> str:
    q = (args.get("query") or "").lower().strip()
    out: list[str] = []
    for s in TOOL_SCHEMAS:
        fn = s.get("function") or {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        hay = f"{name} {desc}".lower()
        if not q or q in hay:
            out.append(f"{name}  —  {desc.splitlines()[0][:140] if desc else ''}")
    return _truncate("\n".join(out) if out else "(no matching tools)")


def t_config_get(args: dict, ctx: ToolContext) -> str:
    engine = getattr(ctx, "engine", None)
    if engine is None or engine.config is None:
        return "ERROR: config not available"
    from .config import get_value
    key = args["key"]
    v = get_value(engine.config, key, None)
    if v is None:
        return f"(unset: {key})"
    # Redact obvious secrets
    if "token" in key.lower() or "secret" in key.lower() or "key" in key.lower():
        s = str(v)
        v = s[:4] + "…" + s[-4:] if len(s) > 8 else "••••"
    return f"{key} = {v}"


def t_config_set(args: dict, ctx: ToolContext) -> str:
    engine = getattr(ctx, "engine", None)
    if engine is None or engine.config is None:
        return "ERROR: config not available"
    from .config import set_value, save
    key = args["key"]
    val = args["value"]
    if not ctx.confirm("config set", f"{key} = {val}"):
        return "ERROR: user denied"
    set_value(engine.config, key, val)
    save(engine.config)
    return f"OK: {key} = {val} (saved)"


def t_sleep(args: dict, ctx: ToolContext) -> str:
    import time as _time
    secs = float(args.get("seconds", 1))
    secs = max(0.0, min(60.0, secs))
    _time.sleep(secs)
    return f"OK: slept {secs}s"


def t_skill(args: dict, ctx: ToolContext) -> str:
    """Append a named skill's instructions onto the engine for the rest of
    the session. Skills live at ~/.config/cagentic/skills/<name>.md."""
    from .config import config_dir
    op = args.get("op", "use")
    name = args.get("name", "")
    skills_dir = config_dir() / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    if op == "list":
        files = sorted(skills_dir.glob("*"))
        if not files:
            return "(no skills installed; drop *.md files in " + str(skills_dir) + ")"
        return "\n".join(f"- {f.stem}  ({f.stat().st_size} bytes)" for f in files)

    if not name:
        return "ERROR: skill name required"
    candidate = None
    for ext in (".md", ".txt", ""):
        c = skills_dir / f"{name}{ext}"
        if c.exists():
            candidate = c
            break
    if op == "get":
        if not candidate:
            return f"ERROR: no skill '{name}'"
        return _truncate(candidate.read_text(errors="replace"))
    if op == "use":
        if not candidate:
            return f"ERROR: no skill '{name}'"
        engine = getattr(ctx, "engine", None)
        if engine is None:
            return "ERROR: engine not available"
        body = candidate.read_text(errors="replace")
        if engine.messages and engine.messages[0].get("role") == "system":
            engine.messages[0]["content"] += f"\n\n=== SKILL: {name} ===\n{body}"
        return f"OK: skill '{name}' attached ({len(body)} chars)"
    return f"ERROR: unknown op '{op}'"


# ============================================================================
# Registry + schemas
# ============================================================================

ToolFn = Callable[[dict, ToolContext], str]

TOOLS: dict[str, ToolFn] = {
    # files
    "read_file": t_read_file,
    "write_file": t_write_file,
    "edit_file": t_edit_file,
    "replace_lines": t_replace_lines,
    "list_dir": t_list_dir,
    "grep": t_grep,
    "glob": t_glob,
    "set_workspace": t_set_workspace,
    # notes
    "note_write": t_note_write,
    "note_get": t_note_get,
    "note_list": t_note_list,
    "note_search": t_note_search,
    "note_delete": t_note_delete,
    # reminders
    "reminder_add": t_reminder_add,
    "reminder_list": t_reminder_list,
    "reminder_done": t_reminder_done,
    "reminder_delete": t_reminder_delete,
    "reminder_update": t_reminder_update,
    # mcp
    "mcp_list_servers": t_mcp_list_servers,
    "mcp_list_tools": t_mcp_list_tools,
    "mcp_call": t_mcp_call,
    "mcp_list_resources": t_mcp_list_resources,
    "mcp_read_resource": t_mcp_read_resource,
    # browser
    "browser_status": t_browser_status,
    "browser_tabs": t_browser_tabs,
    "browser_read": t_browser_read,
    "browser_open": t_browser_open,
    "browser_navigate": t_browser_navigate,
    "browser_click": t_browser_click,
    "browser_fill": t_browser_fill,
    "browser_eval": t_browser_eval,
    "browser_close": t_browser_close,
    # web
    "web_fetch": t_web_fetch,
    "web_search": t_web_search,
    # shell
    "run_bash": t_run_bash,
    "bash_async": t_bash_async,
    # tasks (light)
    "task_get": t_task_get,
    "task_list": t_task_list,
    "task_status": t_task_status,
    "task_wait": t_task_wait,
    "task_output": t_task_output,
    # interaction / planning / system
    "ask_user_question": t_ask_user_question,
    "enter_plan_mode": t_enter_plan_mode,
    "exit_plan_mode": t_exit_plan_mode,
    "todo_write": t_todo_write,
    "tool_search": t_tool_search,
    "config_get": t_config_get,
    "config_set": t_config_set,
    "sleep": t_sleep,
    "skill": t_skill,
}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    # ---------- files ----------
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file, or extract the text from a PDF or Word (.docx) document. Returns line-numbered content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content. Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace an exact string in a file. old_string must be unique unless replace_all=true.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        }, "required": ["path", "old_string", "new_string"]},
    }},
    {"type": "function", "function": {
        "name": "replace_lines",
        "description": "Surgical line-range replacement (1-indexed, inclusive). Use when edit_file fails on string matching.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "new_content": {"type": "string"},
        }, "required": ["path", "start_line", "end_line", "new_content"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List entries in a directory (skips dotfiles).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Recursive regex search. Skips .git, node_modules, build dirs.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"},
            "case_insensitive": {"type": "boolean"},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "glob",
        "description": "File pattern matching (supports ** for recursive globs).",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "set_workspace",
        "description": "Change the workspace directory used to resolve relative paths.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "create": {"type": "boolean"},
        }, "required": ["path"]},
    }},

    # ---------- notes ----------
    {"type": "function", "function": {
        "name": "note_write",
        "description": "Save or update a markdown note in the assistant's knowledge base. Use for facts the user wants you to remember across sessions.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Short name like 'home-wifi' or 'travel-prefs'"},
            "body": {"type": "string"},
            "append": {"type": "boolean", "description": "Prepend a dated entry instead of overwriting"},
        }, "required": ["name", "body"]},
    }},
    {"type": "function", "function": {
        "name": "note_get",
        "description": "Read a saved note by name.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "note_list",
        "description": "List all saved notes (most recently updated first).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "note_search",
        "description": "Substring search across saved notes.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "note_delete",
        "description": "Delete a saved note. Asks for approval.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    }},

    # ---------- reminders ----------
    {"type": "function", "function": {
        "name": "reminder_add",
        "description": "Add a persistent reminder. 'when' accepts 'in 10m', 'in 2h', 'tomorrow', 'tonight', or YYYY-MM-DD[ HH:MM].",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "when": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "reminder_list",
        "description": "List active reminders (use include_done=true for all).",
        "parameters": {"type": "object", "properties": {
            "include_done": {"type": "boolean"},
            "status": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "reminder_done",
        "description": "Mark a reminder done by id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "reminder_delete",
        "description": "Delete a reminder by id. Asks for approval.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "reminder_update",
        "description": "Update a reminder's text, status, when, or tags by id.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"},
            "text": {"type": "string"},
            "status": {"type": "string"},
            "when": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        }, "required": ["id"]},
    }},

    # ---------- mcp ----------
    {"type": "function", "function": {
        "name": "mcp_list_servers",
        "description": "List configured MCP servers (Notion, Google Drive, Slack, etc.).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "mcp_list_tools",
        "description": "List the tools an MCP server exposes.",
        "parameters": {"type": "object", "properties": {"server": {"type": "string"}}, "required": ["server"]},
    }},
    {"type": "function", "function": {
        "name": "mcp_call",
        "description": "Call a tool on an MCP server. Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string"},
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
        }, "required": ["server", "tool"]},
    }},
    {"type": "function", "function": {
        "name": "mcp_list_resources",
        "description": "List URI-addressable resources exposed by an MCP server.",
        "parameters": {"type": "object", "properties": {"server": {"type": "string"}}, "required": ["server"]},
    }},
    {"type": "function", "function": {
        "name": "mcp_read_resource",
        "description": "Read a resource by URI from an MCP server.",
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string"}, "uri": {"type": "string"},
        }, "required": ["server", "uri"]},
    }},

    # ---------- browser ----------
    {"type": "function", "function": {
        "name": "browser_status",
        "description": "Check whether the Cagentic Chrome extension is connected. Call this before other browser_* tools.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "browser_tabs",
        "description": "List the open browser tabs (id, title, url, which is active).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "browser_read",
        "description": "Read the title, URL and visible text of a browser tab (the active tab if tab_id is omitted).",
        "parameters": {"type": "object", "properties": {
            "tab_id": {"type": "integer"},
        }},
    }},
    {"type": "function", "function": {
        "name": "browser_open",
        "description": "Open a URL in a new browser tab. Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "active": {"type": "boolean"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "browser_navigate",
        "description": "Navigate a tab to a URL (active tab if tab_id omitted). Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "tab_id": {"type": "integer"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "browser_click",
        "description": "Click an element in a tab — by CSS 'selector' or by visible 'text'. Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "selector": {"type": "string"},
            "text": {"type": "string"},
            "tab_id": {"type": "integer"},
        }},
    }},
    {"type": "function", "function": {
        "name": "browser_fill",
        "description": "Set the value of a form field matched by CSS selector. Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "tab_id": {"type": "integer"},
        }, "required": ["selector", "value"]},
    }},
    {"type": "function", "function": {
        "name": "browser_eval",
        "description": "Run a snippet of JavaScript in a browser tab and return its result. Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"},
            "tab_id": {"type": "integer"},
        }, "required": ["code"]},
    }},
    {"type": "function", "function": {
        "name": "browser_close",
        "description": "Close a browser tab by id. Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "tab_id": {"type": "integer"},
        }, "required": ["tab_id"]},
    }},

    # ---------- web ----------
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Fetch a URL and return the body. Pass text_only=true to strip HTML for readability.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "timeout": {"type": "integer"},
            "max_bytes": {"type": "integer"},
            "text_only": {"type": "boolean"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web (DuckDuckGo HTML frontend). Returns title + URL pairs.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"},
        }, "required": ["query"]},
    }},

    # ---------- shell ----------
    {"type": "function", "function": {
        "name": "run_bash",
        "description": "Run a shell command in the workspace. Requires user approval.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "bash_async",
        "description": "Run a shell command in the background. Returns a job id; poll with task_status / task_wait.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}, "timeout": {"type": "integer"},
        }, "required": ["command"]},
    }},

    # ---------- tasks ----------
    {"type": "function", "function": {
        "name": "task_get",
        "description": "Get one task by id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "task_list",
        "description": "List tasks, optionally filtered by status.",
        "parameters": {"type": "object", "properties": {"status": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "task_status",
        "description": "Check the status of a background job.",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "task_wait",
        "description": "Block until a background job finishes or timeout elapses.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"}, "timeout": {"type": "number"},
        }, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "task_output",
        "description": "Read the result/output of a task or background job.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }},

    # ---------- interaction / planning ----------
    {"type": "function", "function": {
        "name": "ask_user_question",
        "description": "Pause and ask the user a question, with optional multiple-choice options.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        }, "required": ["question"]},
    }},
    {"type": "function", "function": {
        "name": "enter_plan_mode",
        "description": "Enter PLAN MODE: read-only, no mutating tools.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "exit_plan_mode",
        "description": "Leave plan mode.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "todo_write",
        "description": "Replace this session's todo list. Use reminder_add for persistent reminders.",
        "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {}}}, "required": ["items"]},
    }},
    {"type": "function", "function": {
        "name": "tool_search",
        "description": "Search the registered tools by keyword.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    }},

    # ---------- system ----------
    {"type": "function", "function": {
        "name": "config_get",
        "description": "Read a value from the persistent config (e.g. 'user_name').",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    }},
    {"type": "function", "function": {
        "name": "config_set",
        "description": "Set a config value. Asks for approval.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}, "value": {},
        }, "required": ["key", "value"]},
    }},
    {"type": "function", "function": {
        "name": "sleep",
        "description": "Pause for `seconds` (capped at 60).",
        "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}},
    }},
    {"type": "function", "function": {
        "name": "skill",
        "description": "Manage and apply skills (markdown bundles in ~/.config/cagentic/skills/). op: list | get | use.",
        "parameters": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["list", "get", "use"]},
            "name": {"type": "string"},
        }},
    }},
]


def _all_tools() -> dict[str, ToolFn]:
    from .github import GITHUB_TOOLS
    return {**TOOLS, **GITHUB_TOOLS}


# Tool groups — bundle related tools so the user can keep the prompt lean.
TOOL_GROUPS: dict[str, list[str]] = {
    "files": ["read_file", "write_file", "edit_file", "replace_lines",
              "list_dir", "grep", "glob", "set_workspace"],
    "web": ["web_fetch", "web_search"],
    "notes": ["note_write", "note_get", "note_list", "note_search", "note_delete"],
    "reminders": ["reminder_add", "reminder_list", "reminder_done",
                  "reminder_delete", "reminder_update"],
    "mcp": ["mcp_list_servers", "mcp_list_tools", "mcp_call",
            "mcp_list_resources", "mcp_read_resource"],
    "browser": ["browser_status", "browser_tabs", "browser_read",
                "browser_open", "browser_navigate", "browser_click",
                "browser_fill", "browser_eval", "browser_close"],
    "shell": ["run_bash", "bash_async"],
    "tasks": ["task_get", "task_list", "task_status", "task_wait", "task_output"],
    "interaction": ["ask_user_question"],
    "planning": ["enter_plan_mode", "exit_plan_mode", "todo_write"],
    "system": ["config_get", "config_set", "sleep", "skill", "tool_search"],
    # off by default
    "github": [
        "gh_whoami", "gh_list_repos", "gh_get_repo", "gh_get_file",
        "gh_list_issues", "gh_create_issue", "gh_list_pulls", "gh_get_pull",
        "gh_search_code", "github_api",
    ],
}

# Personal-assistant defaults. Shell uses run_bash's per-call confirm;
# browser tools gate their mutating actions the same way.
DEFAULT_GROUPS: set[str] = {
    "files", "web", "notes", "reminders", "mcp", "browser",
    "shell", "tasks", "interaction", "planning", "system",
}


def _compact_schema(schema: dict) -> dict:
    fn = schema.get("function", {}) or {}
    desc = (fn.get("description") or "").strip()
    short_desc = desc.splitlines()[0].strip() if desc else ""
    if len(short_desc) > 140:
        short_desc = short_desc[:137] + "…"
    new_fn = {**fn, "description": short_desc}
    params = new_fn.get("parameters") or {}
    if isinstance(params, dict) and isinstance(params.get("properties"), dict):
        new_props = {}
        for pname, pspec in params["properties"].items():
            if isinstance(pspec, dict):
                new_props[pname] = {k: v for k, v in pspec.items() if k != "description"}
            else:
                new_props[pname] = pspec
        new_fn["parameters"] = {**params, "properties": new_props}
    return {**schema, "function": new_fn}


def all_tool_schemas(
    enabled_groups: set[str] | None = None,
    compact: bool = True,
) -> list[dict]:
    from .github import GITHUB_TOOL_SCHEMAS
    groups = DEFAULT_GROUPS if enabled_groups is None else set(enabled_groups)
    allowed = {n for g in groups for n in TOOL_GROUPS.get(g, ())}
    schemas = TOOL_SCHEMAS + GITHUB_TOOL_SCHEMAS
    filtered = [s for s in schemas if s.get("function", {}).get("name") in allowed]
    return [_compact_schema(s) for s in filtered] if compact else filtered


TOOL_ALIASES: dict[str, str] = {
    "read":         "read_file",
    "open":         "read_file",
    "view":         "read_file",
    "cat":          "read_file",
    "write":        "write_file",
    "create":       "write_file",
    "edit":         "edit_file",
    "patch":        "edit_file",
    "replace":      "edit_file",
    "ls":           "list_dir",
    "list":         "list_dir",
    "dir":          "list_dir",
    "search":       "grep",
    "find":         "glob",
    "bash":         "run_bash",
    "shell":        "run_bash",
    "exec":         "run_bash",
    "run":          "run_bash",
    "cd":           "set_workspace",
    "fetch":        "web_fetch",
    "curl":         "web_fetch",
    "wget":         "web_fetch",
    "search_web":   "web_search",
    "todo":         "todo_write",
    "todos":        "todo_write",
    # personal-assistant friendly aliases
    "note":         "note_write",
    "save_note":    "note_write",
    "remember":     "note_write",
    "remind":       "reminder_add",
    "add_reminder": "reminder_add",
    "todo_persistent": "reminder_add",
}


def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    all_tools = _all_tools()
    fn = all_tools.get(name)
    if fn is None:
        canonical = TOOL_ALIASES.get(name.lower())
        if canonical and canonical in all_tools:
            result = all_tools[canonical](args, ctx)
            if isinstance(result, str) and not result.startswith("ERROR"):
                result = f"[note: '{name}' is an alias for '{canonical}']\n{result}"
            return result
        import difflib
        pool = list(all_tools.keys()) + list(TOOL_ALIASES.keys())
        suggestions = difflib.get_close_matches(name.lower(), pool, n=3, cutoff=0.4)
        canonical_suggestions: list[str] = []
        for s in suggestions:
            c = TOOL_ALIASES.get(s, s)
            if c in all_tools and c not in canonical_suggestions:
                canonical_suggestions.append(c)
        hint = f"  Did you mean: {', '.join(canonical_suggestions)}?" if canonical_suggestions else ""
        return f"ERROR: unknown tool '{name}'.{hint}  Use /tools to see the full list."
    try:
        return fn(args, ctx)
    except KeyError as e:
        return f"ERROR: missing argument {e}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
