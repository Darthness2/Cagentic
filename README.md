# Cagentic

Your local personal AI assistant — a terminal companion like Claude or ChatGPT, but powered by [Ollama](https://ollama.com) running on your own machine. Everything stays local.

Cagentic **remembers things about you** across sessions, keeps a persistent reminder list, searches the web, edits files, runs shell commands, and bridges out to external services like Notion, Google Drive, and Slack through MCP (Model Context Protocol) servers. It's the friend who keeps your calendar straight and remembers the small things — and it never sends your life to someone else's cloud.

<sub>Cagentic began as a fork of [Collama](https://github.com/Darthness2/Collama), a local coding agent, and grew its own shape, palette, and personality from there.</sub>

## What it can do

- **Remember things** — `note_write` / `note_get`: ask it to remember your dietary preferences, partner's birthday, or weekly schedule once, and it'll surface them when relevant.
- **Reminders that survive** — `reminder_add "call mom" when="tomorrow"` — persistent, separate from per-session todos.
- **Web search & fetch** — DuckDuckGo search, full-page fetch with optional HTML-strip for readability.
- **Files & shell** — read/edit/create files, run shell commands (each one asks for approval unless you `/yolo`).
- **Reads PDFs & Word docs** — `read_file` extracts text from `.pdf` and `.docx` files, so you can ask Cagentic to summarize a contract, pull dates out of an invoice, or review a résumé without converting anything first.
- **MCP bridges** — point Cagentic at any MCP server (Notion, Google Drive, Slack, your own custom ones) via stdio JSON-RPC and it can call their tools and read their resources.
- **Controls your browser** — a companion Chrome extension lets Cagentic read pages, open tabs, click links, and fill forms in your actual browser.
- **Web UI** — `/gateway` starts a local web app: the full assistant in a browser tab, with tool approvals shown right on the page.
- **Conversations persist** — sessions auto-save to `~/.config/cagentic/sessions/`. `/resume` to come back to one.
- **Background jobs** — slow shell commands run in the background; their output gets injected back into the conversation when they finish.
- **GitHub integration** (optional) — list repos, read files, browse issues/PRs with a personal access token.

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

```bash
git clone https://github.com/Darthness2/Cagentic.git ~/Cagentic
cd ~/Cagentic
pip install -e .
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
| `/groups` | manage which tool groups are sent (default: files, web, notes, reminders, mcp, shell, tasks, interaction, planning, system) |
| `/plan on\|off` | read-only mode |
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

## File locations

```
~/.config/cagentic/
├── config.json          # persistent config (chmod 600)
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

## License

MIT
