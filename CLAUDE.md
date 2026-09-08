# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Cagentic is a local-first personal assistant: a terminal REPL, a web gateway, a
Chrome extension and a native iOS client, all driving one tool-calling loop
against Ollama, OpenAI, or Anthropic. It has since absorbed a full coding agent
(Collama) and a "personal OS" layer — goals, calendar, deadlines, inbox,
routines.

Runtime dependencies are deliberately four (`click`, `requests`, `prompt_toolkit`,
`pypdf`) — no LLM SDKs, no web framework. The provider clients, the JSON-RPC MCP
client, the HTTP gateway, the mDNS responder and the browser bridge are all
hand-rolled on the stdlib. `tiktoken` is an optional extra (`.[performance]`)
that only sharpens token counting.

**Adding a runtime dependency is a design decision, not a convenience** — the
four-package list is the reason so much here is hand-rolled. Don't reach for one
without saying why the stdlib won't do.

## Repository layout

```
cagentic/            the Python package — everything below is under here
  services/          compact.py (context management), transcript.py
  gateway_assets/    the web UI: index.html, app.css, app.js (package-data)
extension/           unpacked Chrome extension talking to browser.py
iOS/                 native SwiftUI client (XcodeGen: project.yml → .xcodeproj)
design-system/       brand + gateway design tokens, shared with iOS/design-system
tests/               pytest suite, ~55 files
run.py               source-tree launcher: bootstraps .venv, then the REPL
daemon.py            gateway-only launcher: same bootstrap, never enters the REPL
```

## Commands

```bash
python run.py                 # run from source — bootstraps .venv, pip install -e ., starts the REPL
python run.py --install       # force reinstall deps into .venv
python run.py -p "hello"      # extra args forward to the CLI
python daemon.py              # gateway on :8700, no REPL, no attached console
```

`run.py` re-execs itself inside `.venv` if it isn't already there, so never
`pip install` by hand to test a change — just run it.

The installed entry point (`pip install -e .`) is `cagentic` → `cagentic.cli:main`:

```bash
cagentic -m qwen2.5:7b -C ~/work --yolo
cagentic -p "hello" --json         # noninteractive one-shot
cagentic --serve --port 8700       # headless gateway, no REPL
cagentic --doctor --json           # scriptable install/connectivity diagnostics
cagentic --setup                   # explicit interactive first-run wizard
cagentic --sessions                # list sessions without starting a provider
cagentic --search TEXT             # search saved conversations
cagentic --context ID              # inspect one session's context usage
cagentic --compact ID --dry-run    # preview compacting one saved session
```

Pytest is the full-suite runner; it collects both `unittest.TestCase` tests and
plain pytest regressions:

```bash
.venv/bin/pytest -q
```

```bash
.venv/bin/pytest tests/test_bugfixes.py -q
```

```bash
.venv/bin/pytest tests/test_bugfixes.py::TestReadFileRangeCache::test_second_range_is_served -q
```

Lint and types are configured but their tools are dev extras — install them
before relying on them:

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/ruff format --check cagentic tests
.venv/bin/ruff check cagentic tests
.venv/bin/mypy cagentic
```

Ruff is line-length 100 with `E,F,W,I` and a deliberate ignore baseline
(`E402,E501,E701,E702,E741,F401,F541`) for the older large modules. Mypy checks
the full `cagentic` package. **Run `ruff check` before pushing** — `F821`
(undefined name) is *not* ignored, and a merge that drops a definition while
keeping its call sites is exactly the failure this repo has hit before.

## Architecture

### One loop, four front ends

`engine.py` → `QueryEngine.submit_message()` is *the* agent loop and it is
UI-free: it yields `Message(kind, data, task_id)` events (`delta`, `thinking`,
`plan`, `tool_call`, `tool_result`, `tool_denied`, `compact`, `done`, …). Every
front end is a renderer of that stream:

- `agent.py` — `Agent` wraps `QueryEngine` and renders events to the terminal
  (`render_event`); the REPL in `cli.py` calls `Agent.turn()`.
- `gateway.py` — owns a **second** `QueryEngine` over the *same* `AppState`, and
  maps the same events to SSE. Its UI is no longer embedded strings: it lives in
  `cagentic/gateway_assets/` (`index.html`, `app.css`, `app.js`, shipped via
  `[tool.setuptools.package-data]`) and is read through `_asset_text()`.
- `extension/` and `iOS/` are clients of the gateway's HTTP+SSE surface, not
  separate loops. A change to the SSE payload shape can break them silently —
  they aren't covered by pytest.

Adding an event kind means touching `EventKind`, `agent.render_event`, and the
gateway's SSE mapping together.

### State and permissions

`state.py` `AppState` is the single mutable record (workspace, yolo, plan_mode,
permission cache, edit history, live MCP/browser handles, active model spec).
Mutate via `state.update(**changes)`, never by assigning fields — subscribers
(autosave, tool-support detection) fire on update only. The REPL agent and the
gateway share one `AppState`.

Every tool call passes `permissions.can_use_tool()`: per-tool cache → plan mode →
`READ_ONLY` → yolo → a `Resolver` callback. Resolvers are the UI seam —
`terminal_resolver` prompts on stdin, `Gateway._resolve` blocks on a condition
variable until the browser answers, `auto_deny_resolver` is the headless default
(and what sub-agents get). `READ_ONLY` doubles as `CONCURRENT_SAFE`: the executor
parallelizes only *adjacent* runs of concurrent-safe calls so the model's
intended ordering survives.

Because the terminal resolver identifies "the engine that owns the tty", a tool
that needs stdin (`ask_user_question`) must check for it — the gateway's engine
runs on an HTTP worker thread and would steal or block the REPL's input.

`sandbox.py` is the second line: an approved `run_bash` is confined by the
platform's own sandbox (macOS `sandbox-exec`, Linux `bwrap`) to writes inside
the workspace and no network unless asked. Where no backend exists it degrades
to unconfined — and that degradation must stay **visible to the user**, because
a sandbox that silently isn't there is worse than none.

`hooks.py` adds config-declared command handlers per lifecycle event
(`config.json` → `"hooks"`). Exit code 2 on a gating event (`PreToolUse`,
`UserPromptSubmit`) denies; everything else — including timeouts and crashes —
allows. Hooks must never be able to break the agent loop.

### Tools

`tools.py` plus `coding_tools.py` hold the tool bodies as
`t_<name>(args, ctx) -> str`, with `TOOL_GROUPS` deciding which schemas reach the
model (`teams` and `github` are off by default). Contracts the rest of the
system depends on:

- A tool returns a **string**. Failure is a leading `ERROR:` — the engine keys
  `ok` off that prefix, and `dispatch()` converts exceptions into it.
- `ToolContext` carries `state`, `engine`, `background`, `tasks`, the widget
  callback, and the per-turn `read_cache`. `ctx.confirm()` is a no-op `True`: approval already
  happened in `can_use_tool`, and re-prompting would deadlock the gateway.
- Path arguments go through `_resolve_contained()`; ids that become filenames
  go through a validator (see `tasks._safe_id`).
- Images (`browser_screenshot`) travel via `ctx.pending_images`.

**Every tool call the model emits must get exactly one result.** The assistant
message records all of them, and OpenAI and Anthropic both reject an unanswered
one with a 400 on the *next* request — which poisons the conversation rather
than skipping a call. If the engine declines to run a call (loop guard, early
abort), it still records a synthetic result (`_record_unrun_call`). Compaction
has the same duty: `snip_compact` must never drop a `tool` message.

### Providers

`providers.py` is the only place mapping `"provider:model"` → client. Only
`ollama`, `openai`, `anthropic` are treated as prefixes; anything else
(`llama3:8b`) is an Ollama tag. Clients expose the same surface: `chat`,
`chat_stream`, `chat_stream_assembled`, `list_models`. Switching provider means
rebuilding the client, in *both* directions — cloud→local included.

Models without native tool calls raise `ToolsUnsupportedError`; the engine falls
back to a **text tool protocol** (`<tool>{…}</tool>`, fenced JSON, DeepSeek's
`<|tool▁call▁begin|>` — see the extractors at the top of `engine.py`) and
persists the fact per-model in config.

### Context, tokens, effort

`services/compact.py` — `manage_context()` runs before every turn: snip thinking,
collapse, then bulletize older history behind a boundary marker (stripped in
`normalize_messages_for_api`). `token_count.py` counts provider-aware with a
deterministic fallback. The **effort dial** (`/effort`, `/api/effort`) steers how
hard the model works by injecting a system-prompt section — local models have no
native reasoning knob. Thresholds (`COMPACT_TOKENS`, `COMPACT_KEEP_RECENT`,
`LOOP_THRESHOLD`) are constants at the top of `engine.py`, commented with why
they were retuned; read those before changing them.

### The background service and auto-reload

`service.py` installs `cagentic --serve` as a launchd agent / systemd user unit.
Because that daemon outlives every edit, `autoreload.py` polls the *installed
package directory* (`Path(cagentic.__file__).parent` — so it self-corrects to
whatever tree the process actually imported) and `os.execv`s the process when
it changes. exec rather than exit-and-be-restarted: launchd's
`ThrottleInterval` would cost a 30s outage and systemd's `Restart=on-failure`
wouldn't restart a clean exit at all.

It refuses to restart mid-turn (`Gateway.is_busy`), before the tree settles, or
into code that fails `compile()` — that last one matters because a supervisor
will happily restart a crash-looping process forever. Wired only into the
`serve_mode` branch of `_run_runtime`; the REPL's `/gateway` shares its process
and must never be re-execed.

### Persistence, and the import graph

Everything lives under `~/.config/cagentic/`: `config.json`, `notes/`,
`sessions/`, `projects/`, `tasks/`, `teams/`, `skills/`, `reminders.json`,
`history`, and **`state.sqlite3`**.

`storage.py` is a small transactional SQLite JSON store (`put/get/delete/
list_values/search_values/migrate_json_files`) with non-destructive migration —
sessions, projects, reminders and tasks still write their JSON files *and* mirror
into SQLite, so a downgrade doesn't lose data. The newer subsystems
(`personal_os`, `inbox`, `routines`, `proactive`) are SQLite-only, each owning a
namespace (`os_goals`, `os_events`, …).

Mind the import graph, which has bitten this repo:

```
config.py            (bottom — imports nothing from the package)
  └── storage.py     (imports config_dir)
        └── everything else
fmt.py               (leaf — imports nothing; safe from any layer)
```

`config.py` must never import `storage`, or `import cagentic` dies with a
circular import. Shared display helpers (`fmt_duration`, `fmt_ago`,
`status_mark`) live in `fmt.py` for exactly that reason.

### Per-project config

`project_scope.py` reads `.cagentic/` from the *workspace*, so agent
configuration can be committed and shared:

```
.cagentic/
    settings.json         shared, committed — permission rules
    settings.local.json   personal, gitignored — overrides the above
    commands/*.md         custom slash commands
    skills/*.md           project skills, shadowing the user's own
```

Project files take precedence over the user's global ones: a repo saying "use
tabs here" outranks a personal default, and a fresh clone should behave right
with no setup.

### The personal-OS layer

`personal_os.py` (goals, calendar events, deadlines) backs the agent's `life`
tool group. It used to also back a gateway dashboard; that dashboard and its
~25 `/api/os/*` routes were removed, so the *only* way in is now the tools —
which means a change here shows up in chat, with nothing to keep in sync.
`capabilities.py` went with it (it existed to build the architecture map for
the dashboard's Skills page). Around it: `proactive.py` (background monitoring +
notification inbox), `routines.py` (user-defined scheduled routines evaluated
locally — `Gateway._run_proactive_routine` still runs them), `integrations.py`
(read-only iCalendar / CalDAV — open standards, no vendor SDKs), and
`inbox.py` (local capture + standards-based email ingestion).

### Sub-agents and teams

`subagent.py` forks a child `QueryEngine` with fresh `messages[]`, inherited
state, a **fresh permission cache**, and `auto_deny_resolver` — it must never
block on a terminal prompt. `teams.py` holds long-lived teammate personas with
mailboxes; `coordinator.py` ticks them, forking a sub-agent per teammate with
non-empty mail. Exposed as `agent_call` / `agent_call_async` and the `teams`
group.

### Workspaces, the browser bridge, and discovery

`workspaces.py` enforces canonical, allow-listed workspace roots (the gateway can
switch directories, so this is a real boundary). `browser.py` runs an HTTP server
bound to `127.0.0.1` that the unpacked `extension/` long-polls; reads are
unprompted, anything that acts goes through the permission gate. The gateway
additionally requires a per-process token plus Host/Origin checks on `/api/*`,
and LAN exposure is opt-in (`gateway.lan` + `gateway.token`).
`discovery.py` is a hand-rolled mDNS *responder* only (IPv4, `_cagentic._tcp.local.`)
so the iOS client can find the gateway; it never browses.

## Change recipes

Each of these is a set of places that must move together — the repo has broken
before when only one of them did.

- **New tool** — body `t_<name>(args, ctx) -> str` in `tools.py` or
  `coding_tools.py`; schema registration; membership in a `TOOL_GROUPS` entry;
  a `READ_ONLY` listing if it is safe to run concurrently and without approval;
  a label in `tool_labels.py`. Paths through `_resolve_contained()`, errors as
  `ERROR: …`.
- **New event kind** — `EventKind`, `agent.render_event`, the gateway's SSE
  mapping. All three, same commit.
- **New terminal slash command** — an entry in `prompt.COMMAND_GROUPS` (the
  single catalog feeding both `/help` and the completion popup) *and* a branch in
  `cli.repl()`. `tests/test_polish.py` asserts the two match.
- **New web slash command** — `gateway.GATEWAY_COMMANDS`, `Gateway._handle_cmd()`,
  and the command-matrix tests. Don't add a web command when an existing chat
  control is the clearer interaction.
- **New provider capability** — every client in `providers.py` exposes the same
  four methods; adding a fifth means adding it to all three.
- **New persisted subsystem** — SQLite-only via `storage.py` with its own
  namespace. Don't add new JSON-file storage; the file mirroring that exists is
  a migration compatibility shim, not the pattern.

## Repo conventions

- Windows is a first-class target: guard with `sys.platform`/`os.name`, use
  `Path`, don't assume `chmod`/`fchmod` works, and remember SQLite holds file
  handles that Windows won't let you unlink (test teardown must tolerate it).
- File edits read and write **verbatim** (`newline=""` on both sides) so a
  CRLF file survives an edit. Use `_read_text_robust` / `_write_text_raw` /
  `_restore_eol` rather than `Path.read_text` / `write_text`.
- Comments explain *why*, usually citing the bug that motivated the code. Match
  that; don't strip them. The module docstrings in `sandbox.py`, `discovery.py`
  and `autoreload.py` are the house style: what it does, and what trade it made.
- Terminal output goes through `ui.py` — its palette, markers (`✦ ◦ · ↳ ✓ ✗ ❀`),
  `ui.sync_write` (shared paint lock with the status bar and spinners), and the
  UTF-8/ANSI setup at import. Never `print()` escape codes directly.

## Testing conventions

- Tests are named for the *change* they defend, not the module they cover —
  `test_safety_regressions.py`, `test_stale_edits.py`, `test_context_overflow.py`.
  When you fix a bug, add the case to the file that names that class of bug
  rather than creating a new one per fix.
- Tests must not touch the real `~/.config/cagentic/`. There is no `conftest.py`
  doing this for you: each test that persists anything sets `XDG_CONFIG_HOME` to
  a tmp dir itself (`monkeypatch.setenv`, or save/restore in `setUp`/`tearDown`),
  which is the one knob `config.config_dir()` reads. Teardown also has to
  tolerate an un-unlinkable SQLite file on Windows.
- No network in tests: providers, the gateway and MCP are exercised against
  fakes. A test that needs Ollama running is a broken test.
- `tests/test_polish.py` and the gateway command-matrix tests exist to catch
  half-finished features (a command in one catalog but not the other). If one of
  them fails, the fix is almost never to edit the test.
