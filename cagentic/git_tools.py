"""Local git — status, diff, log, and commit.

Git was the largest hole in the coding surface: the model could read and edit a
repository all day and then had no way to see what it had changed as a whole, or
to record it. The only route was `run_bash("git ...")`, which asks for approval
on every invocation and gives the model a raw terminal blob to parse.

Scope is deliberately local and non-destructive. Anything that rewrites history
or talks to a remote — push, fetch, reset, rebase, checkout — is absent, not
forgotten: those stay in `run_bash`, where the approval prompt spells out the
command and the sandbox's network denial makes reaching a remote an explicit,
visible decision. The tools here cannot lose committed work.

`git_status`, `git_diff` and `git_log` are read-only and never prompt.
`git_commit` mutates and goes through the permission gate like any other write.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .tools import ToolContext, _truncate_ends

# Long enough for a real review diff, short enough not to swallow the context
# window when the model asks for the whole working tree at once.
MAX_DIFF_CHARS = 20000
GIT_TIMEOUT = 30


def _run_git(args: list[str], cwd: Path, timeout: int = GIT_TIMEOUT) -> tuple[int, str, str]:
    """Run one git command in `cwd`. Never raises; returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _repo_root(ctx: ToolContext) -> tuple[Path | None, str]:
    """The work tree containing the workspace, or an explanatory error."""
    root = Path(ctx.root)
    rc, out, err = _run_git(["rev-parse", "--show-toplevel"], root)
    if rc != 0:
        detail = (err or out).strip().splitlines()
        reason = detail[0] if detail else "not a git repository"
        return None, f"ERROR: {root} is not inside a git repository ({reason})"
    return Path(out.strip()), ""


def t_git_status(args: dict, ctx: ToolContext) -> str:
    """Branch, tracking, and what is staged / modified / untracked."""
    repo, problem = _repo_root(ctx)
    if repo is None:
        return problem

    rc, out, err = _run_git(["status", "--porcelain=v1", "--branch"], repo)
    if rc != 0:
        return f"ERROR: git status failed: {(err or out).strip()}"

    lines = out.splitlines()
    header = lines[0][3:] if lines and lines[0].startswith("## ") else "(unknown branch)"
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for line in lines[1:]:
        if len(line) < 4:
            continue
        index_state, work_state, path = line[0], line[1], line[3:]
        if index_state == "?" and work_state == "?":
            untracked.append(path)
            continue
        if index_state not in " ?":
            staged.append(f"{index_state}  {path}")
        if work_state not in " ?":
            unstaged.append(f"{work_state}  {path}")

    parts = [f"branch: {header}", f"repo:   {repo}"]

    def section(title: str, rows: list[str]) -> None:
        if not rows:
            return
        parts.append(f"\n{title} ({len(rows)}):")
        parts.extend(f"  {r}" for r in rows[:100])
        if len(rows) > 100:
            parts.append(f"  … {len(rows) - 100} more")

    section("staged", staged)
    section("not staged", unstaged)
    section("untracked", untracked)
    if not (staged or unstaged or untracked):
        parts.append("\nworking tree clean")
    return _truncate_ends("\n".join(parts))


def t_git_diff(args: dict, ctx: ToolContext) -> str:
    """The actual changes — unstaged by default, staged with staged=true."""
    repo, problem = _repo_root(ctx)
    if repo is None:
        return problem

    staged = bool(args.get("staged", False))
    paths = args.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]

    cmd = ["diff"]
    if staged:
        cmd.append("--cached")
    # No colour: this is going to a model, and escape codes are noise it pays
    # for by the token.
    cmd += ["--no-color", "--no-ext-diff"]
    if args.get("stat"):
        cmd.append("--stat")
    if paths:
        cmd.append("--")
        cmd += [str(p) for p in paths]

    rc, out, err = _run_git(cmd, repo)
    if rc != 0:
        return f"ERROR: git diff failed: {(err or out).strip()}"
    if not out.strip():
        where = "staged" if staged else "unstaged"
        return f"(no {where} changes)"
    return _truncate_ends(out, MAX_DIFF_CHARS)


def t_git_log(args: dict, ctx: ToolContext) -> str:
    """Recent commits, oneline, for orientation and message style."""
    repo, problem = _repo_root(ctx)
    if repo is None:
        return problem

    try:
        count = max(1, min(int(args.get("count", 15)), 200))
    except (TypeError, ValueError):
        count = 15

    cmd = ["log", f"-{count}", "--no-color", "--date=short", "--pretty=%h  %ad  %an  %s"]
    paths = args.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if paths:
        cmd.append("--")
        cmd += [str(p) for p in paths]

    rc, out, err = _run_git(cmd, repo)
    if rc != 0:
        message = (err or out).strip()
        if "does not have any commits yet" in message:
            return "(no commits yet)"
        return f"ERROR: git log failed: {message}"
    return _truncate_ends(out.strip() or "(no commits yet)")


def t_git_commit(args: dict, ctx: ToolContext) -> str:
    """Commit staged changes, or `paths`, or everything already tracked."""
    repo, problem = _repo_root(ctx)
    if repo is None:
        return problem

    message = (args.get("message") or "").strip()
    if not message:
        return "ERROR: missing argument 'message'"

    paths = args.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]

    if paths:
        rc, out, err = _run_git(["add", "--", *[str(p) for p in paths]], repo)
        if rc != 0:
            return f"ERROR: git add failed: {(err or out).strip()}"
    elif args.get("all"):
        # -u, not -A: stage changes to files git already tracks. Sweeping up
        # every untracked file is how build output and stray scratch files end
        # up in history, and the model cannot see what is in .gitignore's blind
        # spots as well as the user can.
        rc, out, err = _run_git(["add", "-u"], repo)
        if rc != 0:
            return f"ERROR: git add -u failed: {(err or out).strip()}"

    rc, staged_out, _ = _run_git(["diff", "--cached", "--name-only"], repo)
    if rc == 0 and not staged_out.strip():
        return (
            "ERROR: nothing staged to commit. Stage specific files with "
            "paths=[...], or pass all=true to include every tracked change."
        )

    rc, out, err = _run_git(["commit", "-m", message], repo)
    if rc != 0:
        detail = (err or out).strip()
        return f"ERROR: git commit failed: {detail}"

    rc, sha, _ = _run_git(["rev-parse", "--short", "HEAD"], repo)
    files = [ln for ln in staged_out.splitlines() if ln.strip()]
    noun = "file" if len(files) == 1 else "files"
    return (
        f"OK: committed {sha.strip() or 'HEAD'}  ({len(files)} {noun})\n{message.splitlines()[0]}"
    )


GIT_TOOLS = {
    "git_status": t_git_status,
    "git_diff": t_git_diff,
    "git_log": t_git_log,
    "git_commit": t_git_commit,
}


GIT_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "Branch and what has changed: staged, not staged, and untracked. "
                "Start here before reviewing or committing."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "The actual line-by-line changes in the work tree. Unstaged by "
                "default; pass staged=true for what is about to be committed, "
                "stat=true for a summary instead of full text. Read this before "
                "writing a commit message — describe what the diff does, not what "
                "you intended."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean"},
                    "stat": {"type": "boolean"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": (
                "Recent commits, one per line. Useful for orientation and for "
                "matching the project's existing commit-message style."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": (
                "Commit with `message`. Commits what is already staged; pass "
                "paths=[...] to stage those first, or all=true for every tracked "
                "change. Never stages untracked files. Read git_diff first and "
                "write the message from what actually changed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "all": {"type": "boolean"},
                },
                "required": ["message"],
            },
        },
    },
]
