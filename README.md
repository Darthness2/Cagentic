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
- **Personal OS dashboard** — the gateway opens on a proactive command center for goals, calendar events, deadlines, a unified inbox, scheduled AI routines, connected services, and a focus feed. Everything is stored locally and is also available to the assistant as tools.
- **Conversations persist** — sessions auto-save to `~/.config/cagentic/sessions/`. `/resume` to come back to one.
- **Background jobs** — slow shell commands run in the background; their output gets injected back into the conversation when they finish.
- **GitHub integration** (optional) — list repos, read files, browse issues/PRs with a personal access token.
- **Full coding agent** (absorbed from Collama) — atomic `multi_edit` batches, parser-only `check_syntax` for 12+ languages, Jupyter `notebook_edit`, a worktree stack for sub-projects, PowerShell support, sub-agents (`agent_call` / `agent_call_async`), a persistent cross-session task graph, and an `/effort low|medium|high` dial for how hard the model works.
- **Agent teams** (optional, `/groups enable teams`) — long-lived teammate personas with mailboxes; a coordinator processes each teammate's mail by forking sub-agents and can auto-claim matching tasks from the task graph.
- **Multi-provider** — use Ollama locally, or connect OpenAI and Anthropic for cloud models when you need more power.
- **iOS companion** — the Cagentic iOS app's chat *and* coding tabs both connect to the same `/gateway`. To reach it from your phone, set `"gateway": {"lan": true, "token": "<secret>"}` in `~/.config/cagentic/config.json` (LAN exposure is off by default; the token is required on every request).

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com) running locally (`ollama serve`)
- A model with native tool calling — good defaults:
  - `llama3.1:8b` — good general-purpose, ~5 GB
  - `qwen2.5:7b` — solid all-rounder, ~5 GB
  - `mistral-nemo` — friendly chat style, ~7 GB

```bash
ollama pull llama3.1:8b
```

## Install

### One-liner (macOS & Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/cagentic/main/install.sh | bash
```

### From source

```bash
git clone https://github.com/YOUR_USERNAME/cagentic.git ~/cagentic
cd ~/cagentic
pip install -e .
```

### From PyPI (coming soon)

```bash
pip install cagentic
```

## Quickstart

```bash
cagentic                                      # interactive REPL
cagentic -p "remind me to call mom tonight"   # one-shot
cagentic --name Sam                           # tell it who you are
cagentic -m qwen2.5:7b                        # pick a model
```

First launch lists installed Ollama models and asks which to use; your choice is saved to `~/.config/cagentic/config.json`.

Type `/` in the REPL to see slash-command completions (`/notes`, `/remind`, `/mcp`, etc.).

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
| `/save [title]` | force-save |
| `/clear` | wipe history (keeps the saved session) |
| `/retry` | re-run your last message |
| `/exit`, `/quit` | leave |

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
| `/groups` | manage which tool groups are sent (default: files, web, notes, reminders, mcp, browser, shell, tasks, interaction, planning, system, coding, worktree, subagent; opt-in: teams, github) |
| `/plan on\|off` | read-only mode |
| `/effort low\|medium\|high` | how hard the model works per turn |
| `/todo` | per-session todo list |
| `/yolo` | toggle auto-approve for tool calls |

### System
| | |
|---|---|
| `/diag` | model / workspace / tools / MCP / data status |
| `/model [name]`, `/models` | switch / list models |
| `/host [url]` | switch Ollama host |
| `/config`, `/set <key> <value>` | view / edit saved config |
| `/login github <token>` | save a GitHub PAT |
| `/login openai <key>` | save OpenAI API key |
| `/login anthropic <key>` | save Anthropic API key |
| `/whoami` | show authenticated GitHub user |
| `/stream on\|off` | toggle token streaming |
| `/help` | show this list in the REPL |

## `@path` mentions

Reference files directly in your prompt — Cagentic inlines them before sending so the model doesn't have to read them first:

```
help me plan a trip — see @~/trip-ideas.md
fix the typo in @~/Documents/letter.txt:42
```

Supports `@path`, `@path:N`, and `@path:N-M`. Works for PDFs and Word docs too — `@~/Documents/contract.pdf` inlines the extracted text.

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

Browser tools: `browser_status`, `browser_tabs`, `browser_read`, `browser_open`, `browser_navigate`, `browser_click`, `browser_fill`, `browser_eval`, `browser_close`. The bridge port is configurable (`browser.port` in config, default `8765`); set the matching port in the extension's popup if you change it.

## The web UI — `/gateway`

`/gateway` starts a local web app — the full Cagentic assistant in a browser tab instead of the terminal. Everything works: tools, notes, reminders, MCP, browser control. It streams responses token-by-token, and when a tool needs approval an **Approve / Always / Deny** prompt appears right in the page.

```
❯ /gateway
  · gateway is live — open http://localhost:8700 in your browser.
❯ /gateway off       # stop it
```

It runs its own conversation (separate from the terminal session) but shares your notes, reminders, and connected services. The port is `gateway.port` in config (default `8700`). Like everything else, it's bound to localhost only.

If the configured port is already occupied, Cagentic automatically tries the next 20 ports and prints the URL it selected. Set `gateway.auto_port` to `false` if you prefer startup to fail instead.

The gateway's Core, Inbox, Planner, Goals, Routines, Skills, and Connections workspaces share one local data model with chat. You can capture an inbox item, event, deadline, or goal in the UI, or ask Cagentic naturally; the assistant can use the `inbox_*`, `calendar_event_*`, `goal_*`, `routine_*`, and `personal_briefing` tools to keep the same dashboard current. Configured MCP servers appear in Connections and remain the bridge to services such as Notion, Drive, and Slack.

The **Core** route uses a dense terminal-style command deck: live vitals and directives on the left, an animated cognitive network and primary intent in the center, and on-demand commands, schedule, deadlines, and proactive intelligence on the right. The **Skills** route exposes Cagentic's live architecture as four execution modes—manual actions, reusable skills, scheduled routines, and delegated agents—organized into Memory, Productivity, Research, Content, Community, Agency, Sales, Finance, and Custom Ops branches. Capability tiles are grounded in registered tools and hand their intent directly to the conductor instead of acting as decorative shortcuts.

Connections can also synchronize calendars directly:

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
cagentic --setup                  # guided model/name/workspace/gateway setup
cagentic --doctor                # readable health report
cagentic --doctor --json         # automation-friendly health report
cagentic --completion bash       # also zsh or fish
cagentic --sessions              # list conversations without starting a model
cagentic --search "auth bug"      # search titles and message content
cagentic --context SESSION_ID    # token usage
cagentic --compact SESSION_ID    # compact older context, keep recent turns
```

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
├── transcripts/         # append-only JSONL of every turn
├── tasks/               # background-job tracking
└── skills/              # *.md skills the model can attach
```

## Look & feel

Cagentic has its own visual identity — a warm "dusk" palette (mauve, peach, gold, plum) instead of a cold tech teal. The welcome screen is a small cozy card rather than a giant logo, it greets you by time of day, and the markers are consistent throughout:

| | |
|---|---|
| `✦` | Cagentic speaking |
| `◦` | quiet thinking |
| `·` | a small note |
| `↳` | a tool it reached for |
| `✓` / `✗` | how that turned out |
| `❀` | a plan |

The working spinner is a soft sparkle that breathes in and out. Set `CAGENTIC_SPINNER=braille` for a plainer one, or `NO_COLOR=1` to drop colors entirely.

## Personality

Cagentic ships with its own character: warm, attentive, unflappable — the friend who keeps your calendar straight and remembers the small things, with a light dry humor and no lecturing. It takes action instead of narrating.

You can amend it for a given workspace by dropping a `CAGENTIC.md` or `AGENTS.md` in any parent directory, or attach a skill from `~/.config/cagentic/skills/`. Tell it your name with `/name` (or `cagentic --name Alex`) and it'll use it naturally.

## Cloud providers (OpenAI & Anthropic)

Cagentic works with Ollama by default, but you can also use cloud models:

```bash
# Set your API key
cagentic -p "/login openai sk-..."

# Switch to a cloud model
cagentic -m openai:gpt-4o
cagentic -m anthropic:claude-sonnet-4-20250514
```

Keys are stored in `~/.config/cagentic/config.json` (chmod 600). You can also use environment variables: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`.

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
