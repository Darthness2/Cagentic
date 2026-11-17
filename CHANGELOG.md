# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).
The version lives in `cagentic/__init__.py`; a `v*` tag matching it is what
publishes a release.

## Unreleased

### Changed — Python 3.10 is now the minimum

Python 3.9 reached end-of-life in October 2025, and GitHub's current Ubuntu
runner no longer ships it, so CI could only test it on a pinned older image.
`requires-python` is now `>=3.10`; the oldest-version CI job runs 3.10.

### Fixed — safety properties that were claimed but not true

Found by re-auditing the session's own work rather than trusting its commit
messages. Each was reproduced before being fixed.

- **A cloned repo could grant itself unattended shell.** `permissions.allow` was
  read from `.cagentic/settings.json` — the *committed* file — and an allow rule
  returns True above yolo and above the resolver, so it skips the prompt
  entirely. A repo shipping `{"allow": ["run_bash(*)"]}` got shell the moment
  you opened the directory. Allow rules now come only from the gitignored
  `settings.local.json`; deny rules still work from either, since they can only
  restrict. This is the same reasoning already used to refuse `mcp.servers` from
  a committed file — it had not been applied one module over.
- **A sub-agent's `tools=` restriction did not restrict.** `tool_groups` was a
  schema filter only; neither the executor nor `dispatch` consulted it. A child
  told "files only" that emitted `run_bash` anyway ran it, and under an inherited
  yolo it was approved. Enforced at the gate now, with aliases resolved first so
  `bash` cannot walk around a check on `run_bash`.
- **Groups declared parallel-safe could write.** The hand-written list was
  `{files, web, mcp, system}`; `files` holds `write_file`/`edit_file`, `mcp` holds
  `mcp_call`, `system` holds `config_set`. That list gated parallel fan-out, so
  children that could write ran concurrently against one tree — each with a
  private staleness map that could not see its siblings. The set is now derived
  from `CONCURRENT_SAFE`, so a tool added to a group cannot silently re-introduce
  it.
- **An unknown group name granted everything.** `tools=["read-only"]` — or any
  plausible name a model invented — dropped the unknown entry and returned "no
  groups", which means *inherit the parent's full toolset*. It now fails closed.
- **A malformed hook timeout killed every turn.** `float()` sat outside the
  guard, so `{"timeout": "ten"}` raised through `decision()` — which, unlike
  `emit()`, has none of its own — breaking the one thing the module promises.
- **Loading a skill then entering plan mode silently dropped it.**
  `refresh_system_prompt` replaces the system message wholesale and is called
  from ~20 places; the skill had been concatenated into that string. Skills are
  now held in a list the prompt is rebuilt from.
- **`notebook_edit` had none of the edit safety.** The only write path outside
  the precondition, and it deletes by cell index — while being in `EDIT_TOOLS`
  (so `accept_edits` waved it through) and not in `DIFFABLE` (so the prompt
  showed no diff).

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
- **Edits could silently discard concurrent work, or be made blind.** An edit
  now refuses a file it has not read, and refuses one whose fingerprint changed
  since it last read or wrote it. Enforced on every write path, notebooks
  included.
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
- **MCP over Streamable HTTP**, alongside stdio — `url` in a server's config
  reaches a hosted server, which a subprocess transport could not do at all. The
  session id handed out at initialize is echoed on later calls, a POST is
  answered with either a JSON body or an SSE stream and the client branches on
  Content-Type, and the protocol revision is negotiated with a fallback for
  older servers. Adds `prompts/list` and `prompts/get` as `mcp_list_prompts` /
  `mcp_get_prompt`.
- **Sub-agents report what they did** rather than returning a paragraph with no
  evidence, can be narrowed to a toolset, and build their own HTTP client
  (`requests.Session` is not thread-safe and up to eight run at once).
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
