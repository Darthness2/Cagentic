"""Shell sandboxing — confine `run_bash` to the workspace, no network.

Approving one `run_bash` used to approve arbitrary access to the machine: the
tool was a bare `subprocess.run` with the workspace as cwd and nothing else
between the model and the filesystem or the internet. This module puts the
platform's own sandbox in the way, so an approved command can still read the
system, but can only *write* inside the workspace (plus temp dirs — see
_MACOS_WRITABLE) and can't reach the network unless the call asked for it.

Backends, in order of preference:
    macOS   sandbox-exec (Seatbelt) — always present on stock macOS
    Linux   bubblewrap (bwrap) — present on most desktops, not guaranteed
    other   none — the command runs unconfined and the caller says so

Degrading to "none" is deliberate and must stay *visible*: a sandbox that
silently isn't there is worse than no sandbox, because the user acts on the
belief that it is.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Temp directories stay writable on top of the workspace. This is a deliberate
# hole: pip, npm, cargo, gcc and pytest all write scratch files outside the
# project, and a sandbox that breaks every build is one the user turns off. So
# the guarantee is "workspace + temp", not "workspace only" — say so everywhere
# rather than overstating it. /tmp is a symlink to /private/tmp on macOS and
# Seatbelt matches the real path.
_MACOS_WRITABLE = (
    "/private/tmp",
    "/private/var/folders",
    "/private/var/tmp",
    "/dev/null",
)

# Markers that a failure was the sandbox rather than the command's own fault.
# Heuristic on purpose — Seatbelt denials surface as the *command's* error, so
# this drives a hint, never a hard claim.
_NETWORK_DENIAL_MARKERS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "network is unreachable",
    "operation not permitted",
    "nodename nor servname provided",
    "connection refused",
    "name or service not known",
    "failed to establish a new connection",
)


def backend() -> str:
    """Which sandbox this machine can actually use: 'seatbelt'|'bwrap'|'none'."""
    if sys.platform == "darwin":
        return "seatbelt" if shutil.which("sandbox-exec") else "none"
    if sys.platform.startswith("linux"):
        return "bwrap" if shutil.which("bwrap") else "none"
    return "none"


def describe() -> str:
    """One line for /doctor and /diag."""
    kind = backend()
    if kind == "seatbelt":
        return "sandbox-exec (macOS Seatbelt)"
    if kind == "bwrap":
        return "bubblewrap"
    if sys.platform.startswith("linux"):
        return "none — install bubblewrap (apt install bubblewrap) to confine shell commands"
    if os.name == "nt":
        return "none — no sandbox backend on Windows"
    return "none"


def _quote_profile_path(path: str) -> str:
    """Escape a path for a Seatbelt profile string literal."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _real(path: Path) -> str:
    """Resolve symlinks — Seatbelt matches on the real path, and on macOS the
    workspace often sits under /tmp, which is really /private/tmp."""
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return str(path)


def seatbelt_profile(workspace: Path, *, network: bool) -> str:
    """Build the macOS profile: allow by default, then take writes and network
    away and hand back only what a build actually needs."""
    writable = [_real(workspace), *_MACOS_WRITABLE]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        writable.append(_real(Path(tmpdir)))

    subpaths = "\n".join(f'    (subpath "{_quote_profile_path(p)}")' for p in sorted(set(writable)))
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
        subpaths,
        '    (literal "/dev/null")',
        '    (literal "/dev/stdout")',
        '    (literal "/dev/stderr")',
        '    (literal "/dev/dtracehelper")',
        '    (regex #"^/dev/tty")',
        ")",
    ]
    if not network:
        lines.append("(deny network*)")
    return "\n".join(lines)


def wrap(
    run_cmd: "str | list[str]",
    use_shell: bool,
    *,
    workspace: Path,
    network: bool,
    enabled: bool = True,
) -> "tuple[str | list[str], bool, str]":
    """Wrap a subprocess invocation in the platform sandbox.

    Takes and returns the `(args, shell)` pair that `_shell_run_invocation`
    produces, plus a short note describing what confinement was applied — the
    caller surfaces that note so "no sandbox available" can never be silent.

    A POSIX `shell=True` string invocation becomes an explicit argv list, since
    the sandbox binary has to be the thing that execs the shell.
    """
    if not enabled:
        return run_cmd, use_shell, "sandbox off"

    kind = backend()
    if kind == "none":
        return run_cmd, use_shell, f"NOT sandboxed ({describe()})"

    # Normalise to argv. shell=True means run_cmd is the raw command string.
    if use_shell and isinstance(run_cmd, str):
        inner = ["/bin/sh", "-c", run_cmd]
    elif isinstance(run_cmd, str):
        inner = ["/bin/sh", "-c", run_cmd]
    else:
        inner = list(run_cmd)

    net_note = "network allowed" if network else "no network"

    if kind == "seatbelt":
        profile = seatbelt_profile(workspace, network=network)
        return (
            ["sandbox-exec", "-p", profile, *inner],
            False,
            f"sandboxed (seatbelt, writes limited to the workspace + temp, {net_note})",
        )

    # bubblewrap: whole filesystem read-only, workspace writable, fresh /tmp.
    ws = _real(workspace)
    argv = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        ws,
        ws,
        "--chdir",
        ws,
    ]
    if not network:
        argv.append("--unshare-net")
    argv.append("--")
    argv.extend(inner)
    return (
        argv,
        False,
        f"sandboxed (bubblewrap, writes limited to the workspace + temp, {net_note})",
    )


def looks_like_network_denial(stderr: str, stdout: str = "") -> bool:
    """Did this failure plausibly come from the network being cut off?

    Used only to add a "re-run with network=true" hint to a failing command —
    a false positive costs one misleading sentence, a false negative costs the
    user ten minutes wondering why curl is broken.
    """
    haystack = f"{stderr}\n{stdout}".lower()
    return any(marker in haystack for marker in _NETWORK_DENIAL_MARKERS)
