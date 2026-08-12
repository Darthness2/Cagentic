# Cagentic

Your local personal AI assistant — a terminal companion like Claude or ChatGPT, but powered by [Ollama](https://ollama.com) running on your own machine. Everything stays local.

Cagentic **remembers things about you** across sessions, keeps a persistent reminder list, searches the web, edits files, runs shell commands, and bridges out to external services like Notion, Google Drive, and Slack through MCP (Model Context Protocol) servers. It's the friend who keeps your calendar straight and remembers the small things — and it never sends your data to someone else's cloud.

## What it can do

- **Remember things** — `note_write` / `note_get`: ask it to remember your dietary preferences, partner's birthday, or weekly schedule once, and it'll surface them when relevant.
- **Reminders that survive** — `reminder_add "call mom" when="tomorrow"` — persistent, separate from per-session todos.
- **Web search & fetch** — DuckDuckGo search, full-page fetch with optional HTML-strip for readability.
- **Files & shell** — read/edit/create files, run shell commands (each one asks for approval unless you `/yolo`).
- **Reads PDFs & Word docs** — `read_file` extracts text from `.pdf` and `.docx` files, so you can ask Cagentic to summarize a contract, pull dates out of an invoice, or review a résumé without converting anything first.
- **MCP bridges** — point Cagentic at any MCP server (Notion, Google Drive, Slack, your own custom ones) via stdio JSON-RPC and it can call their tools and read their resources.
- **Controls your browser** — a companion Chrome extension lets Cagentic read pages, open tabs, click links, and fill forms in your actual browser.
- **Web UI** — `/gateway` starts a local web app: the full assistant in a browser tab, with tool approvals shown right on the page.
- **Rich answers in the tab** — the assistant can draw bar/line/pie charts, tables, stat blocks and progress bars as draggable panels in the web UI instead of dumping an ASCII table into the chat.
- **Personal OS data** — goals, calendar events, deadlines, a unified inbox and scheduled AI routines are stored locally and available to the assistant as tools (the `life` group). Ask for them in chat; there is no separate dashboard to keep in sync.
- **Conversations persist** — sessions auto-save to `~/.config/cagentic/sessions/`. `/resume` to come back to one.
- **Background jobs** — slow shell commands run in the background; their output gets injected back into the conversation when they finish.
- **GitHub integration** (optional) — list repos, read files, browse issues/PRs with a personal access token.
- **Full coding agent** (absorbed from Collama) — atomic `multi_edit` batches, parser-only `check_syntax` for 12+ languages, Jupyter `notebook_edit`, a worktree stack for sub-projects, PowerShell support, sub-agents (`agent_call` / `agent_call_async`), a persistent cross-session task graph, and an `/effort low|medium|high` dial for how hard the model works.
- **Agent teams** (optional, `/groups enable teams`) — long-lived teammate personas with mailboxes; a coordinator processes each teammate's mail by forking sub-agents and can auto-claim matching tasks from the task graph.
- **Multi-provider** — use Ollama locally, or connect OpenAI and Anthropic for cloud models when you need more power.
- **LAN access** — to reach the web UI from another machine, set `"gateway": {"lan": true, "token": "<secret>"}` in `~/.config/cagentic/config.json` (LAN exposure is off by default; the token is required on every request).

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com) running locally (`ollama serve`)
- A model with native tool calling — good defaults:
  - `llama3.2` — lightest of the four, ~2 GB (what `install.sh` pulls)
  - `llama3.1:8b` — good general-purpose, ~5 GB
  - `qwen2.5:7b` — solid all-rounder, ~5 GB
  - `mistral-nemo` — friendly chat style, ~7 GB

```bash
ollama pull llama3.1:8b
```

## Install

### One-liner (macOS & Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/Darthness2/Cagentic/main/install.sh | bash
```

### From source

```bash
git clone https://github.com/Darthness2/Cagentic.git ~/cagentic
cd ~/cagentic
pip install -e .
```

### From PyPI (coming soon)

```bash
pip install cagentic
```

## Quickstart

```bash
cagentic                                           # interactive REPL
cagentic -p "remind me to call mom tonight"        # one-shot
cagentic --name Sam                                # save your name
cagentic -m qwen2.5:7b                             # pick a model for this session
cagentic --no-yolo                                 # require tool approval
```

Running `cagentic` opens the conversational prompt. On the first run, installed
Ollama models are listed and your selection is saved to
`~/.config/cagentic/config.json`.

Type `/` in the REPL to see slash-command completions (`/notes`, `/remind`, `/mcp`, etc.).
Type `@` anywhere in a prompt to complete files from the active workspace.
Use `Esc+Enter` for a newline and Enter to send.

## How it remembers you

When you tell Cagentic something it should keep, it saves a markdown note to `~/.config/cagentic/notes/<name>.md`. The notes are plain files — you can open them in any editor or sync them with iCloud / Drive / git.

A few special names get auto-loaded into the system prompt:

- `profile`, `about-me`, or `me` — gets pulled into context automatically so the assistant knows who it's talking to without you re-introducing yourself.

Example:

```
❯ I'm vegetarian and allergic to peanuts. Save that to my profile.
  ▸ note_write  profile
    ✓ wrote profile (52 chars)

❯ /new
❯ what's a good lunch recipe?
  (Cagentic already knows you're vegetarian with a peanut allergy)
```

Persistent reminders work the same way but live in `~/.config/cagentic/reminders.json`:

```
❯ remind me to renew my passport in 2 weeks
  ▸ reminder_add  renew my passport @ in 2 weeks
    ✓ added: [ ] r1a2b3c4d  renew my passport  (in 14d)
```

When you launch Cagentic, overdue reminders are surfaced in the greeting so they don't get lost.

## Configuring MCP servers

[Model Context Protocol](https://modelcontextprotocol.io/) servers let Cagentic talk to outside services. Add them under `mcp.servers` in `~/.config/cagentic/config.json`:

```json
{
  "mcp": {
    "servers": {
      "notion": {
        "command": ["npx", "-y", "@notionhq/notion-mcp-server"],
        "env": {"NOTION_TOKEN": "secret_xxxxxxxxxxxx"},
        "enabled": true
      },
      "gdrive": {
        "command": ["npx", "-y", "@modelcontextprotocol/server-gdrive"],
        "env": {},
        "enabled": true
      },
      "slack": {
        "command": ["npx", "-y", "@modelcontextprotocol/server-slack"],
        "env": {
          "SLACK_BOT_TOKEN": "xoxb-...",
          "SLACK_TEAM_ID": "T..."
        },
        "enabled": true
      }
    }
  }
}
```

Then in the REPL:

```
❯ /mcp                          # list configured servers
❯ /mcp notion                   # list the tools notion exposes
❯ summarize the last 5 docs I touched in google drive
  ▸ mcp_list_tools  gdrive
  ▸ mcp_call        gdrive/search …
```

Cagentic launches each server as a subprocess on first use and keeps a long-lived JSON-RPC connection going. Tokens in `env` are redacted when `/config` prints them.

## Slash commands

### Personal
| | |
|---|---|
| `/notes` | list saved notes |
| `/note <name>` | show one note |
| `/remind` | list active reminders |
| `/remind add <text>` | add one (use `text @ in 2h` for time) |
| `/remind done <id>` | mark done |
| `/remind delete <id>` | delete |
| `/remind all` | include completed reminders |
| `/remind clear` | mark every active reminder done |
| `/name <your name>` | tell the assistant what to call you |

### MCP, browser & web
| | |
|---|---|
| `/mcp` | list configured MCP servers |
| `/mcp <server>` | list tools on that server |
| `/browser` | Chrome extension status + setup steps |
| `/gateway` | start the web UI (`/gateway off` to stop) |

### Conversation
| | |
|---|---|
| `/new [title]` | start fresh |
| `/resume [id]` | list / resume saved sessions |
| `/sessions` | list saved sessions |
| `/search <text>` | search titles and message content |
| `/context` | show context token usage |
| `/compact` | summarize older turns, keep the recent ones |
| `/save [title]` | force-save |
| `/rename <title>` | rename the current conversation |
| `/delete <id>` | delete a saved conversation |
| `/clear` | wipe history (keeps the saved session) |
| `/retry` | re-run your last message |
| `/quit` | leave |

### Files
| | |
|---|---|
| `/cd [path]` | show or change the working dir |
| `/diff [N]` | show file edits this session |
| `/undo` | revert the most recent edit |

### Tools & permissions
| | |
|---|---|
| `/tools` | list every tool the model can call |
| `/groups [enable\|disable <name>]` | manage which tool groups are sent (default: files, web, notes, reminders, life, mcp, browser, shell, tasks, interaction, planning, system, coding, worktree, subagent, widgets; opt-in: teams, github) |
| `/plan on\|off` | read-only mode |
| `/effort low\|medium\|high` | how hard the model works per turn |
| `/todo [add <text>\|done <n>\|clear]` | manage the per-session todo list |
| `/yolo [on\|off]` | toggle auto-approve for tool calls |

### System
| | |
|---|---|
| `/diag` | model / workspace / tools / MCP / data status |
| `/model [name]`, `/models` | switch / list models |
| `/host [url]` | switch Ollama host |
| `/config`, `/set <key> <value>` | view / edit saved config |
| `/login github` | save a GitHub PAT using a hidden prompt |
| `/login openai` | save an OpenAI API key using a hidden prompt |
| `/login anthropic` | save an Anthropic API key using a hidden prompt |
| `/logout <service>` | remove a saved key |
| `/whoami` | show authenticated GitHub user |
| `/stream on\|off` | toggle token streaming |
| `/help` | show this list in the REPL |

## `@path` mentions

Reference files directly in your prompt — Cagentic inlines them before sending so the model doesn't have to read them first:

```
help me plan a trip — see @~/trip-ideas.md
fix the typo in @~/Documents/letter.txt:42
compare @"release notes.md":10-30 with @src/changelog.md
```

Type `@` to open workspace-aware file completion; it follows `/cd` changes and
quotes paths containing spaces automatically. Supports `@path`, `@path:N`,
`@path:N-M`, and quoted paths. Works for PDFs and Word docs too —
`@~/Documents/contract.pdf` inlines the extracted text. Cagentic prints an
attachment count before the model starts so the context is never ambiguous.

## Reading PDFs & Word documents

`read_file` (and `@path` mentions) transparently extract text from:

- **`.docx`** — Word documents. Handled with the Python standard library, no extra dependency.
- **`.pdf`** — needs the `pypdf` package, which is installed automatically with Cagentic. Scanned/image-only PDFs have no text layer and would need OCR, which Cagentic doesn't do.

Just point Cagentic at the file — *"summarize ~/Downloads/lease.pdf"* or *"what's the total on @invoice.pdf"*. The old binary `.doc` format isn't supported; re-save it as `.docx`.

## Controlling your browser

Cagentic ships with a companion Chrome extension (in the `extension/` folder). Once it's loaded, the assistant can see and act in your real browser — read the page you're on, open tabs, follow links, fill in forms.

**How it works:** Cagentic runs a tiny HTTP server bound to `127.0.0.1`. The extension long-polls it for commands, runs them with Chrome's own APIs, and posts results back. Nothing is exposed beyond localhost, and every action that changes anything (open/navigate/click/fill/eval/close) asks for your approval first — only reads (`browser_read`, `browser_tabs`) go through unprompted.

**Install the extension** (one time):

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top-right)
3. Click **Load unpacked** and select the `extension/` folder in this repo
4. That's it — the extension connects automatically whenever Cagentic is running

Run `/browser` in the REPL any time to check the connection or get the exact folder path. Then just ask: *"what's on this page?"*, *"open my email"*, *"click the login button"*, *"fill the search box with "weekend trips" and submit"*.

Browser tools include `browser_status`, `browser_tabs`, `browser_read`, `browser_open`, `browser_navigate`, `browser_click`, `browser_fill`, `browser_screenshot`, `browser_links`, `browser_download`, `browser_eval`, and `browser_close`. The bridge port is configurable (`browser.port` in config, default `8765`); set the matching port in the extension's popup if you change it.

## The web UI — `/gateway`

`/gateway` starts a local web app — the full Cagentic assistant in a browser tab instead of the terminal. Everything works: tools, notes, reminders, MCP, browser control. It streams responses token-by-token, and when a tool needs approval an **Approve / Always / Deny** prompt appears right in the page.

```
❯ /gateway
  · gateway is live — open http://localhost:8700 in your browser.
❯ /gateway off       # stop it
```

It runs its own conversation (separate from the terminal session) but shares your notes, reminders, and connected services. The port is `gateway.port` in config (default `8700`). Like everything else, it's bound to localhost only.

Type `/help` in the web composer for its focused command list. Chat saving is automatic, and the message **resend**, **edit**, and **delete** controls replace redundant web-only `/save` and `/undo` commands; `/retry` regenerates the latest reply.

If the configured port is already occupied, Cagentic automatically tries the next 20 ports and prints the URL it selected. Set `gateway.auto_port` to `false` if you prefer startup to fail instead.

### Running it as a background service

```bash
cagentic --install-service     # launchd (macOS) or a systemd user unit (Linux)
cagentic --uninstall-service
```

The service runs `cagentic --serve` at every login and restarts if it crashes.

**It also updates itself.** A daemon lives for weeks, so without this it would
keep serving whatever source it imported at boot — you'd edit a file and the
background gateway would silently stay on yesterday's code. It watches the
installed package directory (Python plus the web UI's assets) and re-executes
itself once the tree settles.

It deliberately will *not* restart:

- while a reply is in flight — your conversation isn't dropped mid-answer;
- until edits stop landing, so a `git pull` or a save-all doesn't restart into
  a half-written tree;
- into code that doesn't compile — a syntax error would otherwise become a
  crash loop, since the supervisor keeps restarting a process that dies on
  import. It logs the error and keeps serving the last good code.

Set `gateway.auto_reload` to `false` to turn it off. Restart activity is logged
to `~/.config/cagentic/logs/cagentic.log`. This applies only to the standalone
`--serve` daemon; `/gateway` from the REPL shares that process and is never
re-executed out from under your session.

The gateway is a chat surface, not a dashboard — there are no separate
workspace pages to keep in sync. Ask for your inbox, calendar, deadlines,
goals, or routines in the conversation and the assistant reads and writes the
same local data through the `inbox_*`, `calendar_event_*`, `goal_*`,
`routine_*`, and `personal_briefing` tools. Configured MCP servers remain the
bridge to services such as Notion, Drive, and Slack.

When an answer is easier to read as a picture, the assistant calls
`show_widget` and the result opens as a draggable panel in the tab — bar, line
and pie charts, tables, stat blocks, progress bars, metrics, and alerts.

Calendars can synchronize directly:

- **iCalendar feeds** (`https://…/*.ics` or `webcal://…`) import events from Google Calendar, Outlook, Apple Calendar, and other products that expose a subscription URL.
- **CalDAV** connects to standards-compatible providers for import-only, publish-only, or two-way event sync.
- **Export .ics** in Planner exports the local Cagentic calendar for transfer or subscription elsewhere.

Calendar connections can refresh automatically in the background. Connector credentials are stored only in the local SQLite database, whose files are restricted to the current OS user, and credential fields are never returned by gateway APIs.

The unified inbox combines manual capture with standards-based IMAP ingestion. Email sync imports unread message headers—sender, subject, date, message ID, and size metadata—without downloading message bodies or attachments. Read, done, archived, priority, and snooze state remain local and survive provider refreshes.

Proactive routines put useful thinking on a local schedule: daily planning, inbox triage, weekly review, or a custom prompt. A routine uses the configured model when it is available and falls back to a deterministic local briefing when it is not, then stores the result in the notification center.

The proactive monitor also watches for overdue deadlines, events starting soon, schedule conflicts, and goals at risk. Alerts are durable and deduplicated, and can optionally be delivered as native desktop notifications. Both monitoring and desktop delivery can be disabled in Settings.

The frontend is shipped as package assets rather than embedded in Python. API chat requests may include a saved session `id`; those sessions run through isolated actors with separate engines, message buffers, workspace boundaries, repository defaults, and locks.

## Setup, diagnostics, and saved context

```bash
cagentic setup --interactive                       # guided setup
cagentic doctor                                    # readable health report
cagentic doctor --format json                      # machine-readable report
cagentic completion bash                           # also zsh or fish
cagentic sessions                                  # list conversations
cagentic search "auth bug"                         # search saved context
cagentic context SESSION_ID                        # inspect token usage
cagentic compact SESSION_ID --dry-run              # preview compaction
```

Use `-h`/`--help` for the complete flag reference and `--version` for the
installed version. Commands use `--format json`; top-level compatibility modes
also accept `--json`. Mutating modes offer `--dry-run`. Exit status is `0` for
success, `1` for a runtime failure, and `2` for invalid usage.

The earlier top-level forms such as `cagentic --doctor` and
`cagentic -p "prompt"` remain supported for scripts and muscle memory; the
command forms shown above are the discoverable interface for new usage.

Inside the REPL, `/search`, `/context`, and `/compact` provide the same core workflows. Unknown slash commands suggest the closest known command.

Sessions, projects, tasks, and reminders are indexed transactionally in `~/.config/cagentic/state.sqlite3` using WAL mode. Existing JSON is imported automatically and retained as a non-destructive compatibility backup.

## File locations

```
~/.config/cagentic/
├── config.json          # persistent config (chmod 600)
├── state.sqlite3        # indexed transactional state (WAL mode)
├── history              # REPL input history
├── notes/               # *.md knowledge-base notes
├── reminders.json       # persistent reminders
├── sessions/            # auto-saved conversations
├── projects/            # project folders grouping sessions
├── transcripts/         # append-only JSONL of every turn
├── tasks/               # background-job tracking
├── teams/               # teammate personas and their mailboxes
└── skills/              # *.md skills the model can attach
```

## Look & feel

Cagentic uses a restrained graphite-and-indigo terminal system with a compact
startup summary that preserves scrollback. Help, sessions, diagnostics, tool
activity, and Markdown responses reflow for narrow terminals; `NO_COLOR=1`
produces clean plain text rather than exposing Markdown syntax.

The startup block makes the active model, workspace, tool state, and safety
mode explicit. With `prompt_toolkit` available, the footer keeps that context
live as you use `/cd`, `/model`, `/plan`, or `/yolo`, then progressively
shortens itself in split panes. Permission requests use a dedicated approval
surface with a plain-language action, target workspace, visible deny-by-default
choice, and separately disclosed session-wide options.

| | |
|---|---|
| `›` | your prompt |
| `●` | Cagentic speaking |
| `·` | context or progress |
| `→` | a tool call |
| `✓` / `×` | success or failure |
| `!` | something needs attention |

The default working indicator is a compact braille spinner. Set
`CAGENTIC_SPINNER=ascii` for an ASCII fallback, `CAGENTIC_SPINNER=spark` for
the decorative animation, or `CAGENTIC_SPINNER=off` to disable it. Use
`CAGENTIC_STATUS_BAR=off` to hide the per-turn footer and
`CAGENTIC_CLEAR_SCREEN=1` to clear scrollback at startup. Set
`CAGENTIC_MOTION=reduce` to disable animated cursor painting everywhere; dumb
terminals, redirected output, and CI logs automatically receive stable output
without cursor-control escapes.

## Personality

Cagentic ships with its own character: warm, attentive, unflappable — the friend who keeps your calendar straight and remembers the small things, with a light dry humor and no lecturing. It takes action instead of narrating.

You can amend it for a given workspace by dropping a `CAGENTIC.md` or `AGENTS.md` in any parent directory, or attach a skill from `~/.config/cagentic/skills/`. Tell it your name with `/name` (or `cagentic --name Alex`) and it'll use it naturally.

## Cloud providers (OpenAI & Anthropic)

Cagentic works with Ollama by default, but you can also use cloud models:

```bash
# Save a key at a hidden prompt (never pass the key as an argument)
cagentic --login openai
cagentic --login anthropic

# Switch to a cloud model
cagentic -m openai:gpt-4o
cagentic -m anthropic:claude-sonnet-4-20250514

# Remove a saved key
cagentic --logout openai
```

Keys are stored in `~/.config/cagentic/config.json` (chmod 600). In an active
session, `/login openai`, `/login anthropic`, and `/logout <service>` provide
the same controls. You can also use `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

## Support & Licensing

Cagentic is MIT-licensed. You're free to use it, modify it, and redistribute it.

For commercial licensing, priority support, or custom integrations, contact the maintainer.

## Contributing

Contributions are welcome. Please open an issue to discuss what you'd like to change before submitting a pull request.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

MIT — see [LICENSE](LICENSE) for details.
