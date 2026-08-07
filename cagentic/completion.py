"""Shell completion scripts generated directly from the Click command tree."""

from __future__ import annotations

from click.shell_completion import get_completion_class


def script(shell: str) -> str:
    """Return Click's native completion source for bash, zsh, or fish."""
    from .cli import cli

    completion_class = get_completion_class(shell)
    if completion_class is None:
        raise ValueError("shell must be bash, zsh, or fish")
    completion = completion_class(
        cli=cli,
        ctx_args={},
        prog_name="cagentic",
        complete_var="_CAGENTIC_COMPLETE",
    )
    return completion.source()
