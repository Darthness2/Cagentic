# Repository Guidelines

## Project Structure & Module Organization

`cagentic/` contains the Python package and CLI. The central agent loop is in
`cagentic/engine.py`; terminal rendering and commands live in `agent.py`,
`ui.py`, and `cli.py`. Persistence and configuration are handled by
`storage.py` and `config.py`. The local web gateway is in `gateway.py`, with
shipped frontend files in `cagentic/gateway_assets/`. Browser integration
files are in `extension/`, and tests are in `tests/`. `run.py` is the
convenient source-tree launcher.

## Build, Test, and Development Commands

Create/update the project virtual environment and install contributor tools with:

```bash
python run.py --install
.venv/bin/python -m pip install -e ".[dev]"
```

Run the assistant from source with `python run.py`; pass CLI arguments through,
for example `python run.py -p "hello"`. Run the full suite with
`.venv/bin/pytest -q`, or one module with
`.venv/bin/pytest tests/test_bugfixes.py -q`. Before submitting changes, run
`.venv/bin/ruff format --check cagentic tests`, `.venv/bin/ruff check cagentic tests`,
and `.venv/bin/mypy cagentic`.

## Coding Style & Naming Conventions

Use four-space indentation, type hints where practical, and `snake_case` for
modules, functions, and variables; use `PascalCase` for classes. Ruff is
configured for a 100-character line length and `E`, `F`, `W`, and `I` rules.
Preserve comments that explain why a workaround exists. Use `pathlib.Path`,
guard Windows-specific behavior, and route terminal output through `cagentic/ui.py`.
Tool functions should follow the `t_<name>(args, ctx) -> str` convention and
return failures beginning with `ERROR:`.

## Testing Guidelines

Add regression tests for bug fixes and place them in the most relevant
`tests/test_*.py` module. Tests should isolate configuration and persistent
state with `tmp_path`/temporary directories and avoid network or real user
data. Test names should describe the behavior or invariant being protected.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects consistent with project history, such as
`Fix gateway token validation` or `Add reminder migration tests`. Pull requests
should explain the behavior change, identify validation commands run, link a
relevant issue when applicable, and include screenshots or API examples for
gateway or extension changes. Keep unrelated refactors out of the change.

## Security & Configuration Tips

Never commit API tokens, personal data, or files from `~/.config/cagentic/`.
Keep gateway LAN exposure disabled unless a token and host/origin protections
are configured, and test file operations within an allowed workspace root.
