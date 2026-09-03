"""Human phrasing for tool activity.

Every front end shows the same tool runs — the terminal, the web page, and the
phone — and `list_dir` means nothing to somebody who did not write it. The
phrasing lives here rather than in each renderer so the three cannot drift, and
so the gateway can ship the label alongside the event instead of asking clients
to keep their own copy of a 99-entry table.

Phrases are past tense: by the time one is rendered the tool has run.
"""

from __future__ import annotations

# name -> (one, many). `many` carries a `{n}` slot for the collapsed form.
_LABELS: dict[str, tuple[str, str]] = {
    # -- files and code ----------------------------------------------------
    "list_dir": ("Listed directory", "Listed {n} directories"),
    "read_file": ("Read file", "Read {n} files"),
    "write_file": ("Wrote file", "Wrote {n} files"),
    "edit_file": ("Edited file", "Edited {n} files"),
    "multi_edit": ("Edited file", "Edited {n} files"),
    "replace_lines": ("Replaced lines", "Replaced lines in {n} files"),
    "notebook_edit": ("Edited notebook", "Edited {n} notebooks"),
    "glob": ("Searched for files", "Ran {n} file searches"),
    "grep": ("Searched file contents", "Ran {n} content searches"),
    "check_syntax": ("Checked syntax", "Checked syntax {n} times"),
    # -- shell -------------------------------------------------------------
    "run_bash": ("Ran command", "Ran {n} commands"),
    "bash_async": ("Started command", "Started {n} commands"),
    "powershell": ("Ran PowerShell", "Ran {n} PowerShell commands"),
    # -- web ---------------------------------------------------------------
    "web_search": ("Searched the web", "Ran {n} web searches"),
    "web_fetch": ("Fetched a page", "Fetched {n} pages"),
    # -- browser -----------------------------------------------------------
    "browser_open": ("Opened the browser", "Opened the browser {n} times"),
    "browser_close": ("Closed the browser", "Closed the browser {n} times"),
    "browser_navigate": ("Opened a page", "Opened {n} pages"),
    "browser_read": ("Read the page", "Read {n} pages"),
    "browser_click": ("Clicked the page", "Made {n} clicks"),
    "browser_click_at": ("Clicked the page", "Made {n} clicks"),
    "browser_fill": ("Filled a field", "Filled {n} fields"),
    "browser_scroll": ("Scrolled the page", "Scrolled {n} times"),
    "browser_screenshot": ("Took a screenshot", "Took {n} screenshots"),
    "browser_links": ("Listed links", "Listed links {n} times"),
    "browser_tabs": ("Listed tabs", "Listed tabs {n} times"),
    "browser_eval": ("Ran page script", "Ran {n} page scripts"),
    "browser_download": ("Downloaded a file", "Downloaded {n} files"),
    "browser_status": ("Checked the browser", "Checked the browser {n} times"),
    # -- notes -------------------------------------------------------------
    "note_write": ("Saved a note", "Saved {n} notes"),
    "note_get": ("Read a note", "Read {n} notes"),
    "note_list": ("Listed notes", "Listed notes {n} times"),
    "note_search": ("Searched notes", "Ran {n} note searches"),
    "note_delete": ("Deleted a note", "Deleted {n} notes"),
    # -- tasks -------------------------------------------------------------
    "task_create": ("Started a task", "Started {n} tasks"),
    "task_list": ("Listed tasks", "Listed tasks {n} times"),
    "task_get": ("Checked a task", "Checked {n} tasks"),
    "task_status": ("Checked a task", "Checked {n} tasks"),
    "task_output": ("Read task output", "Read output from {n} tasks"),
    "task_update": ("Updated a task", "Updated {n} tasks"),
    "task_delete": ("Deleted a task", "Deleted {n} tasks"),
    "task_stop": ("Stopped a task", "Stopped {n} tasks"),
    "task_wait": ("Waited for a task", "Waited for {n} tasks"),
    "todo_write": ("Updated the todo list", "Updated the todo list {n} times"),
    # -- goals, calendar, email, inbox -------------------------------------
    "goal_create": ("Added a goal", "Added {n} goals"),
    "goal_list": ("Listed goals", "Listed goals {n} times"),
    "goal_update": ("Updated a goal", "Updated {n} goals"),
    "goal_delete": ("Deleted a goal", "Deleted {n} goals"),
    "calendar_event_add": ("Added an event", "Added {n} events"),
    "calendar_event_list": ("Checked the calendar", "Checked the calendar {n} times"),
    "calendar_event_update": ("Updated an event", "Updated {n} events"),
    "calendar_event_delete": ("Deleted an event", "Deleted {n} events"),
    "calendar_connection_create": ("Added a calendar", "Added {n} calendars"),
    "calendar_connection_list": ("Listed calendars", "Listed calendars {n} times"),
    "calendar_connection_sync": ("Synced a calendar", "Synced {n} calendars"),
    "calendar_connection_delete": ("Removed a calendar", "Removed {n} calendars"),
    "email_connection_create": ("Added a mailbox", "Added {n} mailboxes"),
    "email_connection_list": ("Listed mailboxes", "Listed mailboxes {n} times"),
    "email_connection_sync": ("Synced a mailbox", "Synced {n} mailboxes"),
    "email_connection_delete": ("Removed a mailbox", "Removed {n} mailboxes"),
    "inbox_capture": ("Saved to the inbox", "Saved {n} items to the inbox"),
    "inbox_list": ("Checked the inbox", "Checked the inbox {n} times"),
    "inbox_update": ("Updated an inbox item", "Updated {n} inbox items"),
    "inbox_delete": ("Deleted an inbox item", "Deleted {n} inbox items"),
    # -- reminders, routines, notifications --------------------------------
    "reminder_add": ("Set a reminder", "Set {n} reminders"),
    "reminder_list": ("Checked reminders", "Checked reminders {n} times"),
    "reminder_update": ("Updated a reminder", "Updated {n} reminders"),
    "reminder_done": ("Completed a reminder", "Completed {n} reminders"),
    "reminder_delete": ("Deleted a reminder", "Deleted {n} reminders"),
    "routine_create": ("Added a routine", "Added {n} routines"),
    "routine_list": ("Listed routines", "Listed routines {n} times"),
    "routine_update": ("Updated a routine", "Updated {n} routines"),
    "routine_delete": ("Deleted a routine", "Deleted {n} routines"),
    "notification_list": ("Checked notifications", "Checked notifications {n} times"),
    "notification_update": ("Updated a notification", "Updated {n} notifications"),
    # -- chats and briefings -----------------------------------------------
    "chat_search": ("Searched past chats", "Ran {n} chat searches"),
    "chat_get": ("Read a past chat", "Read {n} past chats"),
    "brief": ("Prepared a briefing", "Prepared {n} briefings"),
    "personal_briefing": ("Prepared your briefing", "Prepared {n} briefings"),
    # -- MCP ---------------------------------------------------------------
    "mcp_call": ("Used a connected tool", "Used {n} connected tools"),
    "mcp_list_servers": ("Listed connections", "Listed connections {n} times"),
    "mcp_list_tools": ("Listed connected tools", "Listed connected tools {n} times"),
    "mcp_list_prompts": ("Listed MCP prompts", "Listed MCP prompts {n} times"),
    "mcp_get_prompt": ("Loaded an MCP prompt", "Loaded {n} MCP prompts"),
    "mcp_list_resources": ("Listed resources", "Listed resources {n} times"),
    "mcp_read_resource": ("Read a resource", "Read {n} resources"),
    "mcp_restart": ("Restarted a connection", "Restarted {n} connections"),
    # -- workspace and modes -----------------------------------------------
    "set_workspace": ("Changed workspace", "Changed workspace {n} times"),
    # -- git ---------------------------------------------------------------
    "git_status": ("Checked git status", "Checked git status {n} times"),
    "git_diff": ("Reviewed the diff", "Reviewed {n} diffs"),
    "git_log": ("Read the commit log", "Read the commit log {n} times"),
    "git_commit": ("Committed", "Made {n} commits"),
    "push_workspace": ("Switched workspace", "Switched workspace {n} times"),
    "pop_workspace": ("Returned to the previous workspace", "Returned workspace {n} times"),
    "enter_plan_mode": ("Started planning", "Started planning {n} times"),
    "exit_plan_mode": ("Finished planning", "Finished planning {n} times"),
    "config_get": ("Read a setting", "Read {n} settings"),
    "config_set": ("Changed a setting", "Changed {n} settings"),
    # -- everything else ---------------------------------------------------
    "agent_call": ("Asked a helper", "Asked {n} helpers"),
    "agent_call_async": ("Started a helper", "Started {n} helpers"),
    "ask_user_question": ("Asked you a question", "Asked you {n} questions"),
    "skill": ("Used a skill", "Used {n} skills"),
    "tool_search": ("Looked for a tool", "Looked for tools {n} times"),
    "show_widget": ("Showed a chart", "Showed {n} charts"),
    "sleep": ("Waited", "Waited {n} times"),
}

# Verbs that read naturally in the past tense when a name has to be guessed at.
_PAST_TENSE = {
    "add": "Added",
    "call": "Called",
    "cancel": "Cancelled",
    "check": "Checked",
    "clear": "Cleared",
    "close": "Closed",
    "create": "Created",
    "delete": "Deleted",
    "edit": "Edited",
    "fetch": "Fetched",
    "get": "Read",
    "list": "Listed",
    "open": "Opened",
    "read": "Read",
    "remove": "Removed",
    "run": "Ran",
    "save": "Saved",
    "search": "Searched",
    "send": "Sent",
    "set": "Set",
    "start": "Started",
    "stop": "Stopped",
    "sync": "Synced",
    "update": "Updated",
    "write": "Wrote",
}


def label_pair(name: str) -> tuple[str, str]:
    """The singular phrase and the plural template (`{n}` slot) for one tool.

    Clients need both: they collapse consecutive runs themselves, so they have to
    be able to say "Listed 4 directories" without another round trip.
    """
    key = str(name or "").strip()
    if not key:
        return ("Used a tool", "Used {n} tools")
    return _LABELS.get(key) or _derive(key)


def describe_tool(name: str, count: int = 1) -> str:
    """A human phrase for one tool, or for `count` consecutive runs of it.

    Unknown names still have to read as English: MCP servers contribute tools at
    runtime, and anybody can add one, so this never falls back to the raw
    identifier when it can help it.
    """
    one, many = label_pair(name)
    if count <= 1:
        return one
    return many.format(n=count)


def _derive(name: str) -> tuple[str, str]:
    """Best effort for a tool this table has never seen."""
    parts = [p for p in name.replace("-", "_").split("_") if p]
    if not parts:
        return ("Used a tool", "Used {n} tools")
    # `note_write` and `write_file` both occur, so check either end for a verb.
    for index in (0, -1):
        verb = _PAST_TENSE.get(parts[index].lower())
        if not verb:
            continue
        rest = parts[1:] if index == 0 else parts[:-1]
        subject = " ".join(rest)
        if subject:
            return (f"{verb} {subject}", f"{verb} {subject} ×{{n}}")
        return (verb, f"{verb} {{n}} times")
    readable = " ".join(parts)
    return (f"Used {readable}", f"Used {readable} {{n}} times")


def is_known_tool(name: str) -> bool:
    """Whether the phrasing is curated rather than guessed."""
    return str(name or "") in _LABELS
