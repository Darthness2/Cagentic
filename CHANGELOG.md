# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).
The version lives in `cagentic/__init__.py`; a `v*` tag matching it is what
publishes a release.

## Unreleased

### Fixed — the shipped defaults could not hold a conversation

Tool schemas and the system prompt are a fixed cost on every request that no
compaction can shrink. 99 schemas (~7,610 tokens) plus a ~2,722-token prompt is
a floor of ~10,332 against a shipped `ollama.num_ctx` of 8192 — **126% of the
window**, before a word of conversation. A fresh install tripped the engine's own
"little fits in this context" warning on turn one.

- `ollama.num_ctx` default raised to 32768.
- `DEFAULT_GROUPS` no longer includes the assistant surface (`life`, `browser`,
  `notes`, `reminders`), which was three quarters of the schema cost. Nothing is
  lost: `tool_search` already lets the model ask for tools outside its schema
  set, and `/groups enable life browser notes reminders` restores them. The
  floor is now ~18%.
- `panels_enabled` defaults off — the terminal cannot draw a widget, so it was
  paying for a tool whose output it could not render.
- New `context_floor` check in `cagentic --doctor` reports the number.
- README now recommends models that can actually hold a tool-calling loop.

### Fixed — safety

- **Sub-agents inherited `yolo` and dropped every restriction.** `dry_run`,
  `permission_rules`, `plan_mode` and `tool_groups` all reset to permissive
  defaults, so a parent in yolo with a standing `deny run_bash` rule forked a
  child that approved `rm -rf /` with reason "yolo mode". `dry_run` is
  documented as a gate yolo cannot open; a sub-agent bypassed it.
- **Edits could silently discard concurrent work.** An edit now refuses a file
  whose fingerprint changed since we last read or wrote it. Deliberately a
  guard, not a precondition — a file never read is not blocked.
- **Leaving plan mode is now the user's decision.** `exit_plan_mode` took no
  argument and gated on nothing. It now carries the plan, renders it at the
  approval prompt, and a refusal leaves plan mode on.

### Fixed — search

- `glob("**/x")` did not match `x` in the search root. The pattern went straight
  to `fnmatch`, which has no concept of `**`, so the most natural pattern a model
  writes silently missed files it could see — and sent it into a retry loop.
- `glob` prunes ignored directories during the walk instead of enumerating the
  whole tree and filtering, and is bounded like `grep`. Repo glob 2.8s → 0.0s.
- The `.gitignore` discovery walk was unbounded: its break capped files
  collected, not directories walked, so a tree with fewer than 50 ignore files
  kept descending. 59s → 0.1s on a home directory.

### Added

- **Git tools** — `git_status`, `git_diff`, `git_log`, `git_commit`. Reading
  never prompts; committing goes through the approval gate. Push, fetch, reset,
  rebase and checkout are deliberately absent and stay in `run_bash`, where the
  approval prompt names the command. Nothing here can lose committed work.
- **Lifecycle hooks** — `hooks.py` existed in full and had zero call sites.
  `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` and `Stop` now
  fire. A `PreToolUse` handler exiting 2 blocks the call and beats `/yolo`; a
  hook that crashes, hangs or is missing fails open. `/hooks` lists them.
- **Project settings beyond permissions** — `.cagentic/settings.json` now also
  carries `tool_groups` and `model`, applied at startup and on `/cd`.
  `mcp.servers` is honoured only from the gitignored `settings.local.json`: an
  MCP server is a command Cagentic spawns, so a committed file must not choose
  it.
- **Skills are discoverable** — they carry a summary, are named in the system
  prompt, and `/skill` lists or loads one.
- **Project slash commands appear in the completion popup**, not only `/help`.
- **A coding section in the system prompt**, which previously said "code" once
  and "test" not at all.
- **Edits report syntax breakage** — the in-process checkers run after every
  edit and warn when one leaves a file unparseable.
- **`grep`** gains context lines, a file-type filter and
  `files_with_matches`/`count` modes; **`glob`** gains `sort=mtime`.
- **Syntax highlighting** for fenced code blocks, via the new `.[code]` extra.
  Without it, blocks render exactly as before.
- **Ctrl-C at the prompt asks for confirmation.** Ctrl-D still exits at once.

### Changed

- `enter_worktree` / `exit_worktree` renamed to `push_workspace` /
  `pop_workspace`. They never ran `git worktree add` or created a branch. Old
  tool names and the old group key still resolve.
- The system prompt describes only the tools actually enabled, so it cannot
  advertise a tool the model does not have. 2,722 → 2,052 tokens.
- The terminal renders the human tool labels the engine already shipped, so it
  no longer says `read_file` where the web page says "Read file".

### Infrastructure

- The version has one home (`cagentic/__init__.py`); `pyproject.toml` and the
  MCP handshake read it.
- CI runs the suite across Linux, macOS and Windows on Python 3.9–3.13, instead
  of Ubuntu and 3.12 alone — the code carries real platform branches CI never
  reached. A `v*` tag publishes to PyPI once the matrix is green.
- `install.sh` asks Python whether it is new enough rather than comparing the
  halves of its version string, which rejected a future Python 4.0.
