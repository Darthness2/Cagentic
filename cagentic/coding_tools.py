"""Coding-agent tools absorbed from Collama.

Collama (the standalone terminal coding agent) was folded into Cagentic;
this module carries the tools that only existed there: parser-only syntax
checking, batch multi-edit, Jupyter notebook editing, the worktree stack,
PowerShell, sub-agent forking, the persistent-task-graph writers, briefs,
and the multi-agent team/coordinator surface.

Follows the same pattern as `cagentic.github`: a TOOLS dict plus a
TOOL_SCHEMAS list that `cagentic.tools` merges into its registry.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from . import diff as _diff
from .tools import (
    PathEscapeError,
    ToolContext,
    _contain,
    _fuzzy_span,
    _norm_eol,
    _read_text_robust,
    _record_edit,
    _resolve,
    _resolve_contained,
    _truncate,
    _write_text_raw,
)

# ============================================================================
# check_syntax — parser-only syntax linting across many languages
# ============================================================================

_SYNTAX_EXT_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".sh": "bash",
    ".bash": "bash",
    ".go": "go",
    ".rs": "rust",
    ".toml": "toml",
    ".html": "html",
    ".xml": "xml",
    ".css": "css",
}


def _check_python(src: str, label: str) -> tuple[bool, str]:
    try:
        compile(src, label, "exec")
    except SyntaxError as e:
        loc = f"{e.filename or label}:{e.lineno or '?'}:{e.offset or '?'}"
        return False, f"SyntaxError at {loc}: {e.msg}"
    return True, "ok"


def _check_json(src: str, label: str) -> tuple[bool, str]:
    import json as _json

    try:
        _json.loads(src)
    except _json.JSONDecodeError as e:
        return False, f"JSONDecodeError at {label}:{e.lineno}:{e.colno}: {e.msg}"
    return True, "ok"


def _check_yaml(src: str, label: str) -> tuple[bool, str]:
    try:
        import yaml
    except ImportError:
        return True, "skipped (PyYAML not installed)"
    try:
        list(yaml.safe_load_all(src))
    except yaml.YAMLError as e:
        return False, f"YAMLError in {label}: {e}"
    return True, "ok"


def _check_toml(src: str, label: str) -> tuple[bool, str]:
    try:
        import tomllib  # py311+
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            return True, "skipped (no tomllib/tomli)"
    try:
        tomllib.loads(src)
    except Exception as e:
        return False, f"TOMLDecodeError in {label}: {e}"
    return True, "ok"


def _check_via_cmd(cmd: list[str], src: str | None, label: str) -> tuple[bool, str]:
    """Run a parser-only external command. If the binary is missing return
    'skipped' so the tool stays useful on machines without every toolchain."""
    try:
        proc = subprocess.run(
            cmd,
            input=src if src is not None else None,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except FileNotFoundError:
        return True, f"skipped ({cmd[0]} not installed)"
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]} timed out checking {label}"
    if proc.returncode == 0:
        return True, "ok"
    err = (proc.stderr or proc.stdout).strip()
    return False, err or f"{cmd[0]} exit {proc.returncode}"


def _check_js(src: str, label: str, is_ts: bool) -> tuple[bool, str]:
    # `node --check` parses without running. For TS there's no built-in
    # parser; use tsc only when present.
    if is_ts:
        import shutil

        if shutil.which("tsc"):
            import tempfile

            suffix = ".tsx" if label.endswith(".tsx") else ".ts"
            with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as f:
                f.write(src)
                tmp = f.name
            try:
                return _check_via_cmd(["tsc", "--noEmit", "--allowJs", tmp], None, label)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return True, "skipped (tsc not installed; node --check can't parse TS)"
    return _check_via_cmd(["node", "--check", "-"], src, label)


def _check_bash(src: str, label: str) -> tuple[bool, str]:
    return _check_via_cmd(["bash", "-n"], src, label)


def _check_go(src: str, label: str) -> tuple[bool, str]:
    return _check_via_cmd(["gofmt", "-e"], src, label)


def _check_rust(src: str, label: str) -> tuple[bool, str]:
    # No stdin parse mode; write temp and use `rustc --emit=metadata`.
    import shutil
    import tempfile

    if not shutil.which("rustc"):
        return True, "skipped (rustc not installed)"
    with tempfile.NamedTemporaryFile("w", suffix=".rs", delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        return _check_via_cmd(
            ["rustc", "--edition=2021", "--emit=metadata", "-o", os.devnull, tmp],
            None,
            label,
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _check_xml_like(src: str, label: str) -> tuple[bool, str]:
    # HTML uses the lenient stdlib parser (catches gross issues); XML is strict.
    if label.lower().endswith((".html", ".htm")):
        from html.parser import HTMLParser

        class _P(HTMLParser):
            def error(self, msg):
                raise ValueError(msg)

        p = _P()
        try:
            p.feed(src)
            p.close()
        except Exception as e:
            return False, f"HTMLParseError in {label}: {e}"
        return True, "ok"
    import xml.etree.ElementTree as ET

    try:
        ET.fromstring(src)
    except ET.ParseError as e:
        return False, f"XMLParseError in {label}: {e}"
    return True, "ok"


def _check_css(src: str, label: str) -> tuple[bool, str]:
    # Stdlib has no CSS parser; brace-balance catches the common
    # "model cut the file off mid-rule" case.
    depth = 0
    for i, ch in enumerate(src):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False, f"CSS in {label}: stray '}}' at offset {i}"
    if depth != 0:
        return False, f"CSS in {label}: {depth} unclosed brace(s)"
    return True, "ok (brace-balance only; install a CSS linter for full check)"


_SYNTAX_CHECKERS = {
    "python": lambda s, l: _check_python(s, l),
    "json": lambda s, l: _check_json(s, l),
    "yaml": lambda s, l: _check_yaml(s, l),
    "javascript": lambda s, l: _check_js(s, l, is_ts=False),
    "typescript": lambda s, l: _check_js(s, l, is_ts=True),
    "bash": lambda s, l: _check_bash(s, l),
    "go": lambda s, l: _check_go(s, l),
    "rust": lambda s, l: _check_rust(s, l),
    "toml": lambda s, l: _check_toml(s, l),
    "html": lambda s, l: _check_xml_like(s, l),
    "xml": lambda s, l: _check_xml_like(s, l),
    "css": lambda s, l: _check_css(s, l),
}


def t_check_syntax(args: dict, ctx: ToolContext) -> str:
    """Parser-only syntax check across one or more files / inline snippets.
    Read-only — never executes the code under test."""
    paths = args.get("paths") or ([args["path"]] if args.get("path") else [])
    content = args.get("content")
    language = (args.get("language") or "").lower().strip() or None

    if content is not None and not paths:
        if not language:
            return "ERROR: 'language' is required when checking inline 'content'."
        checker = _SYNTAX_CHECKERS.get(language)
        if not checker:
            return f"ERROR: unsupported language '{language}'. Known: {', '.join(sorted(_SYNTAX_CHECKERS))}"
        ok, msg = checker(content, f"<inline:{language}>")
        return f"{'PASS' if ok else 'FAIL'}  <inline:{language}>  {msg}"

    if not paths:
        return "ERROR: provide 'paths' (list of files) or 'content' + 'language'."

    lines: list[str] = []
    any_fail = False
    for raw in paths:
        try:
            p = _resolve_contained(raw, ctx.root)
        except Exception as e:
            lines.append(f"FAIL  {raw}  resolve error: {e}")
            any_fail = True
            continue
        if not p.exists():
            lines.append(f"FAIL  {raw}  no such file")
            any_fail = True
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception as e:
            lines.append(f"FAIL  {raw}  read error: {e}")
            any_fail = True
            continue
        lang = language or _SYNTAX_EXT_LANG.get(p.suffix.lower())
        if not lang:
            lines.append(f"SKIP  {raw}  unknown extension '{p.suffix}'")
            continue
        checker = _SYNTAX_CHECKERS.get(lang)
        if not checker:
            lines.append(f"SKIP  {raw}  no checker for '{lang}'")
            continue
        ok, msg = checker(src, str(p))
        if ok:
            lines.append(f"PASS  {raw}  [{lang}]  {msg}")
        else:
            any_fail = True
            lines.append(f"FAIL  {raw}  [{lang}]  {msg}")

    header = "syntax check: " + ("FAILED" if any_fail else "all passed")
    return _truncate(header + "\n" + "\n".join(lines))


# ============================================================================
# multi_edit — atomic batch of edits against one file
# ============================================================================


def _apply_one_edit(text: str, old: str, new: str, replace_all: bool):
    """Resolve one edit against `text`. Returns (new_text | None, note).
    Mirrors edit_file's exact → EOL-normalized recovery ladder; refuses
    whitespace-fuzzy matches the same way edit_file does."""
    count = text.count(old)
    if count == 1 or (count > 1 and replace_all):
        return (text.replace(old, new) if replace_all else text.replace(old, new, 1)), "exact"
    if count > 1:
        return None, f"old_string matches {count} times — pass replace_all=true or add context"
    norm = _norm_eol(text)
    old_n = _norm_eol(old)
    if norm.count(old_n) == 1:
        return norm.replace(old_n, _norm_eol(new), 1), "eol-normalized"
    span = _fuzzy_span(norm, old_n)
    if span is not None and not span[2]:
        i, j, _ = span
        return None, (
            f"no exact match; closest region (lines {i + 1}-{j}) differs only "
            f"in whitespace — re-supply old_string exactly"
        )
    return None, "old_string not found"


def t_multi_edit(args: dict, ctx: ToolContext) -> str:
    """Apply MANY edits to a single file in one call. Edits apply in order,
    each against the result of the previous one. The batch is atomic: if any
    edit fails to match, NOTHING is written and the failing edit is named."""
    path = args["path"]
    edits = args.get("edits") or []
    if not isinstance(edits, list) or not edits:
        return "ERROR: 'edits' must be a non-empty list of {old_string, new_string} objects."
    try:
        p = _resolve_contained(path, ctx.root)
    except PathEscapeError as exc:
        return f"ERROR: {exc}"
    if not p.exists():
        return f"ERROR: file not found: {path}"
    raw = _read_text_robust(p)

    text = raw
    notes: list[str] = []
    for idx, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return f"ERROR: edit #{idx} is not an object. Nothing written."
        old = edit.get("old_string")
        new = edit.get("new_string")
        if old is None or new is None:
            return f"ERROR: edit #{idx} is missing old_string or new_string. Nothing written."
        new_text, note = _apply_one_edit(text, old, new, bool(edit.get("replace_all", False)))
        if new_text is None:
            return (
                f"ERROR: edit #{idx} of {len(edits)} failed: {note}.\n"
                f"The batch is ATOMIC — nothing was written. Fix edit #{idx} "
                f"(copy old_string exactly from a recent read_file) and resend the "
                f"whole batch, or apply it separately with replace_lines."
            )
        text = new_text
        notes.append(f"#{idx}:{note}")

    # An EOL-normalizing recovery flattened the file to LF; restore CRLF so a
    # single whitespace-tolerant edit doesn't rewrite every line ending.
    if ("\r\n" in raw) and any(not n.endswith(":exact") for n in notes):
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")

    if text == raw:
        return f"OK: no-op — all {len(edits)} edits left {path} unchanged."
    if raw.strip() and not text.strip() and not args.get("allow_empty"):
        return f"ERROR: refusing to empty {path}. If intentional, pass allow_empty=true."
    if not ctx.confirm("file edit", f"{path}: apply {len(edits)} edits in one batch"):
        return "ERROR: user denied edit"
    _write_text_raw(p, text)
    _record_edit(ctx, p, raw, text, "edit")
    adds, dels = _diff.stats(raw, text)
    recovered = sum(1 for n in notes if not n.endswith(":exact"))
    tail = f"  ({recovered} fuzzy-matched)" if recovered else ""
    return f"OK: applied {len(edits)} edits to {path} +{adds} -{dels}{tail}"


# ============================================================================
# notebook_edit — get/insert/replace/delete cells in a Jupyter .ipynb
# ============================================================================


def t_notebook_edit(args: dict, ctx: ToolContext) -> str:
    import json as _json

    path = args["path"]
    op = str(args.get("op", "replace")).strip().lower()  # replace | insert | delete | get
    cell_index = args.get("cell_index")
    new_source = args.get("source", "")
    requested_cell_type = args.get("cell_type")
    cell_type = (
        ("code" if op == "insert" else None) if requested_cell_type is None else requested_cell_type
    )
    if op not in {"get", "replace", "insert", "delete"}:
        return f"ERROR: unknown op '{op}'"
    if cell_type is not None and cell_type not in {"code", "markdown", "raw"}:
        return f"ERROR: invalid cell_type '{cell_type}'"
    if not isinstance(new_source, (str, list)) or (
        isinstance(new_source, list) and any(not isinstance(line, str) for line in new_source)
    ):
        return "ERROR: source must be a string or list of strings"
    try:
        p = _resolve_contained(path, ctx.root)
    except PathEscapeError as exc:
        return f"ERROR: {exc}"
    existed = p.exists()
    raw = ""
    if not p.exists():
        if op == "insert" and cell_index in (None, 0, "0"):
            p.parent.mkdir(parents=True, exist_ok=True)
            nb: dict[str, Any] = {
                "cells": [],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        else:
            return f"ERROR: file not found: {path}"
    else:
        try:
            raw = _read_text_robust(p)
            parsed = _json.loads(raw)
        except _json.JSONDecodeError as exc:
            return f"ERROR: invalid notebook JSON: {exc}"
        if not isinstance(parsed, dict):
            return "ERROR: notebook root must be a JSON object"
        nb = parsed
    raw_cells = nb.setdefault("cells", [])
    if not isinstance(raw_cells, list) or any(not isinstance(cell, dict) for cell in raw_cells):
        return "ERROR: notebook 'cells' must be a list of objects"
    cells: list[dict[str, Any]] = raw_cells

    def index_value(*, required: bool) -> tuple[int | None, str | None]:
        if cell_index is None:
            return (None, "cell_index is required") if required else (None, None)
        if isinstance(cell_index, bool):
            return None, f"cell_index must be an integer, got {cell_index!r}"
        try:
            return int(cell_index), None
        except (TypeError, ValueError):
            return None, f"cell_index must be an integer, got {cell_index!r}"

    if op == "get":
        if cell_index is None:
            return _truncate(
                "\n\n".join(
                    f"# cell {i} [{c.get('cell_type', '?')}]\n"
                    + (
                        "".join(c.get("source", []))
                        if isinstance(c.get("source"), list)
                        else (c.get("source") or "")
                    )
                    for i, c in enumerate(cells)
                )
            )
        i, error = index_value(required=True)
        if error or i is None:
            return f"ERROR: {error}"
        if not 0 <= i < len(cells):
            return f"ERROR: cell_index {i} out of range"
        c = cells[i]
        return f"cell {i} [{c.get('cell_type', '?')}]\n" + (
            "".join(c.get("source", []))
            if isinstance(c.get("source"), list)
            else (c.get("source") or "")
        )

    if not ctx.confirm("notebook edit", f"{op} {path} [cell {cell_index}]"):
        return "ERROR: user denied"

    if op == "replace":
        i, error = index_value(required=True)
        if error or i is None:
            return f"ERROR: {error}"
        if not 0 <= i < len(cells):
            return f"ERROR: cell_index {i} out of range"
        cells[i]["source"] = new_source
        if cell_type:
            cells[i]["cell_type"] = cell_type
    elif op == "insert":
        i, error = index_value(required=False)
        if error:
            return f"ERROR: {error}"
        i = len(cells) if i is None else i
        new_cell: dict[str, Any] = {
            "cell_type": cell_type or "code",
            "source": new_source,
            "metadata": {},
        }
        if cell_type == "code":
            new_cell["execution_count"] = None
            new_cell["outputs"] = []
        cells.insert(max(0, min(i, len(cells))), new_cell)
    elif op == "delete":
        i, error = index_value(required=True)
        if error or i is None:
            return f"ERROR: {error}"
        if not 0 <= i < len(cells):
            return f"ERROR: cell_index {i} out of range"
        del cells[i]

    rendered = _json.dumps(nb, indent=1) + "\n"
    _write_text_raw(p, rendered)
    _record_edit(ctx, p, raw, rendered, "edit" if existed else "create")
    return f"OK: {op} on {path} (now {len(cells)} cells)"


# ============================================================================
# enter_worktree / exit_worktree — push/pop the workspace stack
# ============================================================================


def t_enter_worktree(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    create = bool(args.get("create", False))
    p = _resolve(path, ctx.root)
    if ctx.workspace_boundary is not None:
        try:
            p = _contain(p, ctx.workspace_boundary)
        except PathEscapeError as e:
            return f"ERROR: pinned project boundary: {e}"
    if not p.exists():
        if not create:
            return f"ERROR: worktree dir not found: {p}. Pass create=true to mkdir it."
        p.mkdir(parents=True, exist_ok=True)
    elif not p.is_dir():
        return f"ERROR: not a directory: {p}"
    state = getattr(ctx, "state", None)
    if state is None:
        ctx.root = p.resolve()
        return f"OK: workspace set to {p} (no state for stack)"
    stack = list(getattr(state, "worktree_stack", []) or [])
    stack.append(str(ctx.root))
    state.update(worktree_stack=stack, workspace=p.resolve())
    ctx.root = p.resolve()
    return f"OK: entered worktree {p}  (stack depth {len(stack)})"


def t_exit_worktree(args: dict, ctx: ToolContext) -> str:
    from pathlib import Path

    state = getattr(ctx, "state", None)
    if state is None:
        return "ERROR: worktree stack not available"
    stack = list(getattr(state, "worktree_stack", []) or [])
    if not stack:
        return "ERROR: worktree stack is empty (no enter_worktree to pop)"
    prev = stack.pop()
    state.update(worktree_stack=stack, workspace=Path(prev))
    ctx.root = Path(prev)
    return f"OK: exited worktree, back to {prev}"


# ============================================================================
# powershell — run a command via pwsh / powershell.exe
# ============================================================================


def t_powershell(args: dict, ctx: ToolContext) -> str:
    import shutil as _shutil

    pwsh = _shutil.which("pwsh") or _shutil.which("powershell")
    if not pwsh:
        return "ERROR: PowerShell not installed (pwsh / powershell.exe not on PATH)"
    cmd = args["command"]
    timeout = int(args.get("timeout", 60))
    if not ctx.confirm("PowerShell command", cmd):
        return "ERROR: user denied command"
    try:
        proc = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", cmd],
            cwd=str(ctx.root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {timeout}s"
    status = "PASS" if proc.returncode == 0 else "FAIL"
    parts = [f"{status} (exit code {proc.returncode})"]
    if proc.stdout:
        parts.append(f"--- stdout ---\n{proc.stdout}")
    if proc.stderr:
        parts.append(f"--- stderr ---\n{proc.stderr}")
    return _truncate("\n".join(parts))


# ============================================================================
# agent_call / agent_call_async — fork a sub-agent on a fresh conversation
# ============================================================================


def t_agent_call(args: dict, ctx: ToolContext) -> str:
    engine = getattr(ctx, "engine", None)
    if engine is None:
        return "ERROR: engine not available for sub-agent"
    from .subagent import fork_subagent

    prompt = args["prompt"]
    model = args.get("model")
    answer = fork_subagent(engine, prompt, model=model, title=args.get("title", "subagent"))
    return _truncate(f"[sub-agent answer]\n{answer}")


def t_agent_call_async(args: dict, ctx: ToolContext) -> str:
    engine = getattr(ctx, "engine", None)
    bg = getattr(ctx, "background", None)
    if engine is None or bg is None:
        return "ERROR: engine/background not available"
    from .subagent import fork_subagent

    prompt = args["prompt"]
    model = args.get("model")

    def _run(p):
        return fork_subagent(engine, p, model=model)

    job_id = bg.submit_dream(prompt, _run)
    return f"OK: dream {job_id} dispatched (will surface on completion)"


# ============================================================================
# Persistent task graph writers + briefs (readers live in cagentic.tools)
# ============================================================================


def _task_graph(ctx: ToolContext):
    return getattr(ctx, "tasks", None)


def t_task_create(args: dict, ctx: ToolContext) -> str:
    tg = _task_graph(ctx)
    if tg is None:
        return "ERROR: task graph not available"
    task = tg.create(
        title=args["title"],
        description=args.get("description", ""),
        deps=args.get("deps") or [],
        parent_id=args.get("parent_id"),
        worktree=args.get("worktree"),
    )
    return f"OK: created {task.id}  {task.short()}"


def t_task_update(args: dict, ctx: ToolContext) -> str:
    tg = _task_graph(ctx)
    if tg is None:
        return "ERROR: task graph not available"
    tid = args["id"]
    changes = {k: v for k, v in args.items() if k != "id"}
    task = tg.update(tid, **changes)
    if not task:
        return f"ERROR: no task with id {tid}"
    return f"OK: updated {task.short()}"


def t_task_delete(args: dict, ctx: ToolContext) -> str:
    tg = _task_graph(ctx)
    if tg is None:
        return "ERROR: task graph not available"
    return "OK: deleted" if tg.delete(args["id"]) else f"ERROR: no task {args['id']}"


def t_task_stop(args: dict, ctx: ToolContext) -> str:
    """Mark a task or background job cancelled. (Background threads can't be
    killed cleanly in pure Python; we mark cancelled and ignore the result.)"""
    bg = getattr(ctx, "background", None)
    tid = args["id"]
    if bg is not None:
        job = bg.status(tid)
        if job and job.status == "running":
            job.status = "cancelled"
            return f"OK: marked background job {tid} cancelled (the thread may still finish)"
    tg = _task_graph(ctx)
    if tg is not None:
        if tg.update(tid, status="cancelled"):
            return f"OK: task {tid} marked cancelled"
    return f"ERROR: no task/job with id {tid}"


def t_brief(args: dict, ctx: ToolContext) -> str:
    """Store/retrieve a short markdown brief on the session state."""
    state = getattr(ctx, "state", None)
    if state is None:
        return "ERROR: state not available"
    op = args.get("op", "set")
    name = args["name"]
    if op == "get":
        return state.briefs.get(name, f"(no brief named '{name}')")
    if op == "list":
        return (
            "\n".join(f"- {k}  ({len(v)} chars)" for k, v in state.briefs.items()) or "(no briefs)"
        )
    if op == "delete":
        state.briefs.pop(name, None)
        state.update(briefs=dict(state.briefs))
        return f"OK: deleted brief '{name}'"
    text = args.get("content") or ""
    state.briefs[name] = text
    state.update(briefs=dict(state.briefs))
    return f"OK: brief '{name}' saved ({len(text)} chars)"


# ============================================================================
# Teams + mailboxes + coordinator — multi-agent collaboration
# ============================================================================


def _teams(ctx: ToolContext):
    return getattr(ctx, "teams", None)


def t_team_create(args: dict, ctx: ToolContext) -> str:
    reg = _teams(ctx)
    if reg is None:
        return "ERROR: team registry not available"
    name = args["name"]
    reg.create_team(name)
    return f"OK: team '{name}' ready at {reg.root / name}"


def t_team_delete(args: dict, ctx: ToolContext) -> str:
    reg = _teams(ctx)
    if reg is None:
        return "ERROR: team registry not available"
    name = args["name"]
    if not ctx.confirm("delete team", f"team {name} and all its teammates"):
        return "ERROR: user denied"
    return "OK: deleted" if reg.delete_team(name) else f"ERROR: no team {name}"


def t_team_list(args: dict, ctx: ToolContext) -> str:
    reg = _teams(ctx)
    if reg is None:
        return "ERROR: team registry not available"
    teams = reg.list_teams()
    if not teams:
        return "(no teams)"
    out: list[str] = []
    for t in teams:
        members = reg.list_teammates(t)
        out.append(f"{t}  ({len(members)} member{'s' if len(members) != 1 else ''})")
        for m in members:
            out.append(f"  - {m.short()}")
    return "\n".join(out)


def t_teammate_create(args: dict, ctx: ToolContext) -> str:
    reg = _teams(ctx)
    if reg is None:
        return "ERROR: team registry not available"
    tm = reg.add_teammate(
        team=args["team"],
        name=args["name"],
        role=args.get("role", ""),
        skills=args.get("skills") or [],
    )
    return f"OK: created teammate {tm.short()}"


def t_teammate_delete(args: dict, ctx: ToolContext) -> str:
    reg = _teams(ctx)
    if reg is None:
        return "ERROR: team registry not available"
    return (
        "OK: deleted"
        if reg.delete_teammate(args["team"], args["name"])
        else f"ERROR: no teammate {args['name']} on {args['team']}"
    )


def t_teammate_list(args: dict, ctx: ToolContext) -> str:
    reg = _teams(ctx)
    if reg is None:
        return "ERROR: team registry not available"
    members = reg.list_teammates(args.get("team"))
    if not members:
        return "(no teammates)"
    return "\n".join(m.short() for m in members)


def t_send_message(args: dict, ctx: ToolContext) -> str:
    """Request/response across teammates via mailboxes."""
    reg = _teams(ctx)
    if reg is None:
        return "ERROR: team registry not available"
    team = args["team"]
    to = args["to"]
    sender = args.get("from", "lead")
    content = args["content"]
    kind = args.get("kind", "msg")
    tm = reg.deliver(team, to, sender, content, kind=kind)
    if tm is None:
        return f"ERROR: no teammate {to} on team {team}"
    return f"OK: delivered to {team}/{tm.name}  (inbox now {len(tm.mailbox)})"


def t_inbox(args: dict, ctx: ToolContext) -> str:
    reg = _teams(ctx)
    if reg is None:
        return "ERROR: team registry not available"
    tm = reg.get_teammate(args["team"], args["name"])
    if not tm:
        return f"ERROR: no teammate {args['name']} on {args['team']}"
    if not tm.mailbox:
        return f"{tm.team}/{tm.name}: inbox empty"
    out = [f"{tm.team}/{tm.name}: {len(tm.mailbox)} message(s)"]
    for i, m in enumerate(tm.mailbox, 1):
        head = (m.get("content") or "").splitlines()[0][:160]
        out.append(f"  {i}. [{m.get('kind', 'msg')}] from {m.get('from', '?')}: {head}")
    return "\n".join(out)


def t_coordinator_tick(args: dict, ctx: ToolContext) -> str:
    engine = getattr(ctx, "engine", None)
    if engine is None:
        return "ERROR: engine not available"
    from .coordinator import tick as _coordinator_tick

    results = _coordinator_tick(
        engine,
        team=args.get("team"),
        auto_claim=bool(args.get("auto_claim", False)),
        max_per_teammate=int(args.get("max_per_teammate", 1)),
    )
    if not results:
        return "(no teammates with pending mail or claimable tasks)"
    out = [f"processed {len(results)} teammate(s):"]
    for r in results:
        first = r.answer.splitlines()[0][:140] if r.answer else ""
        claimed = f"  claimed={r.claimed_task_id}" if r.claimed_task_id else ""
        out.append(f"  - {r.teammate}  inbox={r.inbox_count}{claimed}")
        if first:
            out.append(f"      → {first}")
    return "\n".join(out)


def t_coordinator_run(args: dict, ctx: ToolContext) -> str:
    """Tick repeatedly until everyone is idle (or `max_rounds` reached)."""
    engine = getattr(ctx, "engine", None)
    if engine is None:
        return "ERROR: engine not available"
    from .coordinator import tick as _coordinator_tick

    max_rounds = int(args.get("max_rounds", 5))
    auto_claim = bool(args.get("auto_claim", True))
    team = args.get("team")
    rounds: list[str] = []
    for r in range(1, max_rounds + 1):
        results = _coordinator_tick(engine, team=team, auto_claim=auto_claim)
        if not results:
            break
        rounds.append(f"round {r}: processed {len(results)} teammate(s)")
    return "\n".join(rounds) if rounds else "(idle — nothing to do)"


# ============================================================================
# Registry
# ============================================================================

CODING_TOOLS = {
    "check_syntax": t_check_syntax,
    "multi_edit": t_multi_edit,
    "notebook_edit": t_notebook_edit,
    "enter_worktree": t_enter_worktree,
    "exit_worktree": t_exit_worktree,
    "powershell": t_powershell,
    "agent_call": t_agent_call,
    "agent_call_async": t_agent_call_async,
    "task_create": t_task_create,
    "task_update": t_task_update,
    "task_delete": t_task_delete,
    "task_stop": t_task_stop,
    "brief": t_brief,
    "team_create": t_team_create,
    "team_delete": t_team_delete,
    "team_list": t_team_list,
    "teammate_create": t_teammate_create,
    "teammate_delete": t_teammate_delete,
    "teammate_list": t_teammate_list,
    "send_message": t_send_message,
    "inbox": t_inbox,
    "coordinator_tick": t_coordinator_tick,
    "coordinator_run": t_coordinator_run,
}


CODING_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "check_syntax",
            "description": (
                "Parser-only syntax check for one or more files (or an inline snippet). "
                "Read-only — never executes the code. Use after edits to confirm the "
                "file still parses. Languages auto-detected from extension: python, "
                "json, yaml, javascript, typescript, bash, go, rust, toml, html, xml, "
                "css. External-tool checkers skip gracefully when the binary is missing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files to check (absolute or workspace-relative).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Single-file convenience alias for 'paths'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Inline source to check instead of a file.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Force language (python|json|yaml|javascript|typescript|bash|go|rust|toml|html|xml|css). Required with 'content'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": (
                "Apply MANY {old_string, new_string} edits to ONE file in a single "
                "atomic call. Edits apply in order, each against the result of the "
                "previous one; if any edit fails to match, nothing is written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                                "replace_all": {"type": "boolean"},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                    "allow_empty": {"type": "boolean"},
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notebook_edit",
            "description": "Get/insert/replace/delete a cell in a Jupyter .ipynb file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "op": {"type": "string", "enum": ["get", "insert", "replace", "delete"]},
                    "cell_index": {"type": "integer"},
                    "source": {"type": "string"},
                    "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"]},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enter_worktree",
            "description": "Push the current workspace onto a stack and switch to `path`. Use when working on a sub-task in its own directory. Pair with exit_worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "create": {"type": "boolean"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exit_worktree",
            "description": "Pop the worktree stack and restore the previous workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "powershell",
            "description": "Run a command via pwsh / powershell.exe (Windows or PowerShell Core). Asks for approval like run_bash.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agent_call",
            "description": (
                "Fork a sub-agent on a FRESH conversation to handle a focused subtask "
                "(e.g. 'find every file that imports requests and summarize'). Returns "
                "the sub-agent's final answer. Inherits workspace, tokens, etc., but "
                "its messages are isolated so the main context stays clean."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {
                        "type": "string",
                        "description": "Override model for this sub-agent. Default: same as parent.",
                    },
                    "title": {"type": "string"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agent_call_async",
            "description": "Like agent_call but runs in the background; result is auto-injected on completion. Useful for long research while you keep working.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a persistent task with status tracking and optional dependencies. Returns the new task id (e.g. t9f3e2c1).",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "deps": {"type": "array", "items": {"type": "string"}},
                    "parent_id": {"type": "string"},
                    "worktree": {
                        "type": "string",
                        "description": "Optional worktree directory bound to this task.",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": "Update a task. Common: status (pending|active|done|blocked|failed|cancelled) and result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "result": {"type": "string"},
                    "deps": {"type": "array", "items": {"type": "string"}},
                    "worktree": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_delete",
            "description": "Delete a task by id.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_stop",
            "description": "Mark a task or background job cancelled.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "brief",
            "description": "Store / retrieve / list a named markdown brief in this session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["set", "get", "list", "delete"]},
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "team_create",
            "description": "Create a persistent team (a directory of long-lived teammate personas).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "team_delete",
            "description": "Delete a team and all its teammates. Requires user approval.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "team_list",
            "description": "List all teams and their teammates.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "teammate_create",
            "description": "Add a teammate to a team. `role` is appended to the system prompt for that teammate; `skills` are tags used by the coordinator's auto-claim.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team": {"type": "string"},
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["team", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "teammate_delete",
            "description": "Remove a teammate from a team.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["team", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "teammate_list",
            "description": "List teammates, optionally filtered to one team.",
            "parameters": {"type": "object", "properties": {"team": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to a teammate's inbox (request-response protocol). Recipient processes mail next coordinator_tick / coordinator_run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team": {"type": "string"},
                    "to": {"type": "string", "description": "Recipient teammate name or id."},
                    "content": {"type": "string"},
                    "from": {"type": "string", "description": "Sender label. Default 'lead'."},
                    "kind": {"type": "string", "description": "msg | task | question | reply"},
                },
                "required": ["team", "to", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inbox",
            "description": "Read a teammate's pending mailbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["team", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "coordinator_tick",
            "description": "One coordinator tick: process every teammate's mailbox by spawning a sub-agent. With auto_claim=true, idle teammates also pick up matching pending tasks from the task graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team": {
                        "type": "string",
                        "description": "Limit to one team. Default: all teams.",
                    },
                    "auto_claim": {"type": "boolean"},
                    "max_per_teammate": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "coordinator_run",
            "description": "Tick the coordinator repeatedly until no teammate has work (or max_rounds is reached).",
            "parameters": {
                "type": "object",
                "properties": {
                    "team": {"type": "string"},
                    "max_rounds": {"type": "integer"},
                    "auto_claim": {"type": "boolean"},
                },
            },
        },
    },
]
