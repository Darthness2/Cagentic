# Repository Guidelines

Contributor workflow for Cagentic: how to build it, test it, and get a change
merged. **Architecture lives in [CLAUDE.md](CLAUDE.md)** — the agent loop, the
permission gate, the provider abstraction, the import graph, and the "these
files must move together" recipes. Read that before changing behaviour; read
this before opening a PR. Don't duplicate architecture notes here.

## Project Structure & Module Organization

```
cagentic/            the Python package and CLI
  engine.py            the agent loop — UI-free, yields events
  agent.py ui.py cli.py   terminal rendering, REPL, slash commands
  gateway.py           local web gateway (HTTP + SSE)
  gateway_assets/      its frontend — shipped as package-data, not embedded strings
  services/            compact.py (context management), transcript.py
  storage.py config.py persistence and configuration
extension/           unpacked Chrome extension (browser bridge)
iOS/                 native SwiftUI client — XcodeGen, project.yml is the source
design-system/       brand + gateway tokens, shared with iOS/design-system
tests/               pytest suite
run.py               source-tree launcher; daemon.py is the gateway-only variant
```

`extension/` and `iOS/` are clients of the gateway's HTTP+SSE surface and are
**not covered by pytest** — a payload change can break them silently. Exercise
them by hand and say so in the PR.

## Build, Test, and Development Commands

```bash
python run.py --install                       # create/refresh .venv with runtime deps
.venv/bin/python -m pip install -e ".[dev]"   # contributor tools: pytest, ruff, mypy
```

Run from source with `python run.py`; arguments pass through
(`python run.py -p "hello"`). Never `pip install` by hand to test a change —
`run.py` re-execs itself inside `.venv`, so a hand-installed package is not the
one that runs.

```bash
.venv/bin/pytest -q                           # full suite
.venv/bin/pytest tests/test_bugfixes.py -q    # one module
.venv/bin/ruff format --check cagentic tests
.venv/bin/ruff check cagentic tests
.venv/bin/mypy cagentic
```

Run all four before submitting. `ruff check` matters most: `F821` (undefined
name) is *not* in the ignore baseline, and a merge that drops a definition while
keeping its call sites is a failure this repo has actually shipped.

## Coding Style & Naming Conventions

Four-space indentation, type hints where practical, `snake_case` for modules,
functions and variables, `PascalCase` for classes. Ruff: 100-character lines,
rules `E`, `F`, `W`, `I`. The `ignore` list in `pyproject.toml`
(`E402,E501,E701,E702,E741`) is an **incremental baseline** for the
older large modules, not a target — don't add to it, and clearing entries as you
touch a file is welcome.

Constraints worth knowing before you write the code:

- **Python 3.9 is the floor** (`requires-python = ">=3.9"`, and CI runs it). No
  `match`, no `X | Y` in runtime positions, no `dict[str, int]` without
  `from __future__ import annotations` at the top — which most modules already
  have. Mypy is pinned to `python_version = "3.10"` in `pyproject.toml`, so it
  will *not* catch a 3.9 break for you; CI's 3.9 cell will.
- **Windows is a first-class target.** Guard with `sys.platform`/`os.name`, use
  `pathlib.Path`, don't assume `chmod`/`fchmod` works, and remember SQLite holds
  file handles Windows won't let you unlink.
- **Terminal output goes through `cagentic/ui.py`** — its palette, markers, and
  `ui.sync_write` paint lock. Never `print()` escape codes directly.
- **Tool functions** follow `t_<name>(args, ctx) -> str`, return failures as a
  string beginning with `ERROR:`, and route path arguments through
  `_resolve_contained()`. See CLAUDE.md for the full tool contract.
- **File edits are byte-preserving** — use `_read_text_robust` /
  `_write_text_raw` / `_restore_eol`, not `Path.read_text` / `write_text`, so a
  CRLF file survives an edit.
- **Comments explain *why*, citing the bug that motivated the code.** That is
  the house style throughout; preserve those comments rather than tidying them
  away, and match them in new code.
- **New frontend files under `gateway_assets/` need a matching glob in
  `[tool.setuptools.package-data]`**, or they work from a source tree and vanish
  from the wheel.

## Testing Guidelines

Add a regression test for every bug fix, in the existing `tests/test_*.py`
module that names that *class* of bug (`test_safety_regressions.py`,
`test_stale_edits.py`, `test_context_overflow.py`) rather than a new file per
fix. Name tests for the behaviour or invariant being protected.

- **Isolate persistent state yourself.** There is no `conftest.py`: a test that
  writes config must set `XDG_CONFIG_HOME` to a temp dir
  (`monkeypatch.setenv`, or save/restore in `setUp`/`tearDown`) — that is the one
  knob `config.config_dir()` reads. Teardown must tolerate an un-unlinkable
  SQLite file on Windows.
- **No network, no real user data.** Providers, the gateway and MCP are
  exercised against fakes; a test that needs Ollama running is a broken test.
- **Consistency tests are load-bearing.** `tests/test_polish.py` and the gateway
  command-matrix tests exist to catch half-finished features (a command
  registered in one catalog but not the other). When one fails, the fix is
  almost never to edit the test.

CI runs lint and types once on 3.12, then the suite across
`ubuntu/macos/windows` × `3.9/3.12/3.13` (minus the two oldest non-Linux cells).
The extra platforms are there for real branches — the Windows shell hunt, the
Seatbelt/bubblewrap sandbox backends, the POSIX-only shell pool, the
launchd/systemd split — so a green Linux run is not a green build.

## Commit & Pull Request Guidelines

Commit subjects are short, imperative, and describe the **outcome**, matching
project history: `Require that a file has been read before editing it`,
`Give MCP an HTTP transport, version negotiation and prompts`. Not
`fix stuff`, not a file list.

A pull request should:

- explain the behaviour change and why it was needed;
- name the validation commands actually run, and their result;
- link the relevant issue where one exists;
- include screenshots or API examples for gateway or extension changes;
- add a `CHANGELOG.md` entry under `## Unreleased` for anything user-visible,
  in that file's style — what was wrong, and how it was reproduced.

Keep unrelated refactors out. Report failures honestly: a PR saying "two tests
still fail, here's the output" is worth more than one that quietly skips them.

## Releasing

The version lives in `cagentic/__init__.py` and nowhere else — `pyproject.toml`
reads it via `[tool.setuptools.dynamic]`. Pushing a `v*` tag publishes to PyPI
through trusted publishing, gated on the full lint+test matrix, and CI fails the
release if the tag disagrees with `cagentic.__version__`. Bump the version and
move the `## Unreleased` section under it before tagging.

## Security & Configuration Tips

- Never commit API tokens, personal data, or anything from `~/.config/cagentic/`.
- **Permission-relevant config must not be readable from committed files.**
  `permissions.allow` and `mcp.servers` come only from the gitignored
  `.cagentic/settings.local.json`; a cloned repo must never be able to grant
  itself unattended shell. Deny rules may come from either file, since they can
  only restrict. Apply this reasoning to any new setting that widens capability.
- Keep gateway LAN exposure disabled unless a token and Host/Origin protections
  are configured.
- Test file operations inside an allowed workspace root — `workspaces.py` is a
  real boundary because the gateway can switch directories.
- A degraded security control must stay **visible**. `sandbox.py` reports when it
  falls back to unconfined, because a sandbox that silently isn't there is worse
  than none; hold new controls to the same rule.
