"""Shell completion script generation."""

from __future__ import annotations

COMMANDS = "--doctor --setup --sessions --search --compact --context --serve --model --host --cwd --yolo --version"


def script(shell: str) -> str:
    if shell == "bash":
        return f"complete -W '{COMMANDS}' cagentic"
    if shell == "zsh":
        return f"#compdef cagentic\n_arguments '*:option:({COMMANDS})'"
    if shell == "fish":
        return "\n".join(
            f"complete -c cagentic -l {item[2:]}"
            for item in COMMANDS.split()
            if item.startswith("--")
        )
    raise ValueError("shell must be bash, zsh, or fish")
