"""Live capability architecture for the gateway's agentic-OS map.

The catalog is deliberately grounded in Cagentic's real tools.  It gives the
UI a stable domain architecture without pretending that decorative tiles are
separate services or executable features.
"""

from __future__ import annotations

from typing import Any, Iterable


MODES = (
    {
        "id": "manual",
        "label": "Manual",
        "description": "An explicit user-triggered action",
    },
    {
        "id": "skill",
        "label": "Skill",
        "description": "A reusable tool-backed capability",
    },
    {
        "id": "routine",
        "label": "Routine",
        "description": "A scheduled or repeatable workflow",
    },
    {
        "id": "agent",
        "label": "Agent",
        "description": "A delegated multi-step objective",
    },
)


BRANCHES: tuple[dict[str, Any], ...] = (
    {
        "id": "memory",
        "name": "Memory",
        "foundation": True,
        "capabilities": (
            ("Personal Vault", "skill", ("note_write", "note_get", "note_search"), "Review what you know about me and identify useful missing context."),
            ("Conversation Recall", "skill", ("chat_search", "chat_get"), "Find the past conversation most relevant to what I am working on now."),
            ("Project Context", "agent", ("read_file", "grep", "task_get"), "Build a concise working context for my current project."),
            ("Profile Capture", "manual", ("note_write",), "Help me add an important durable fact to my profile."),
            ("Knowledge Index", "skill", ("note_list", "note_search"), "Summarize the themes and gaps in my saved knowledge."),
        ),
    },
    {
        "id": "productivity",
        "name": "Productivity",
        "foundation": True,
        "capabilities": (
            ("Inbox Triage", "routine", ("inbox_list", "inbox_update"), "Triage my inbox into urgent, waiting, and next-action groups."),
            ("Calendar Brief", "routine", ("calendar_event_list", "personal_briefing"), "Brief me on my schedule, conflicts, and preparation needs."),
            ("Goal Momentum", "skill", ("goal_list", "goal_update"), "Review my active goals and identify the highest-leverage next action."),
            ("Daily Review", "routine", ("personal_briefing", "reminder_list"), "Run a concise end-of-day review across commitments and progress."),
            ("Morning Orientation", "routine", ("personal_briefing", "inbox_list"), "Orient me for today using my calendar, deadlines, goals, and inbox."),
            ("Focus Planner", "agent", ("calendar_event_list", "goal_list", "reminder_list"), "Create a realistic focus plan around my actual commitments."),
        ),
    },
    {
        "id": "research",
        "name": "Research",
        "capabilities": (
            ("Web Research", "skill", ("web_search", "web_fetch"), "Research the topic I give you and return a sourced brief."),
            ("Deep Research", "agent", ("web_search", "web_fetch", "note_write"), "Run a deep research pass and preserve the durable findings."),
            ("Browser Intelligence", "agent", ("browser_status", "browser_read", "browser_tabs"), "Inspect my active browser context and surface relevant intelligence."),
            ("Source Digest", "skill", ("web_fetch", "note_write"), "Turn a source into a concise digest with decisions and open questions."),
            ("Trend Scan", "routine", ("web_search",), "Scan for meaningful changes in the topics I care about."),
        ),
    },
    {
        "id": "content",
        "name": "Content",
        "capabilities": (
            ("Outline Studio", "skill", ("note_get", "note_write"), "Create a strong outline from my notes and current objective."),
            ("Document Reader", "skill", ("read_file",), "Extract the key information and actions from a document."),
            ("Content Cascade", "agent", ("note_get", "write_file"), "Turn one core idea into a coherent set of reusable content assets."),
            ("Draft Review", "skill", ("read_file", "edit_file"), "Review a draft for structure, clarity, and alignment with its purpose."),
            ("Publishing Brief", "manual", ("note_write",), "Capture a publishing brief for an idea I want to develop."),
        ),
    },
    {
        "id": "community",
        "name": "Community",
        "capabilities": (
            ("Community Pulse", "agent", ("web_search", "note_search"), "Summarize recurring questions and emerging themes in my community context."),
            ("Q&A Digest", "routine", ("inbox_list", "note_write"), "Create a digest of questions that deserve a response."),
            ("Member Onboarding", "skill", ("note_get", "write_file"), "Draft a clear onboarding path from my existing context."),
            ("Comment Triage", "manual", ("browser_read",), "Help me triage visible comments and choose which ones deserve a response."),
        ),
    },
    {
        "id": "agency",
        "name": "Agency",
        "capabilities": (
            ("Client Brief", "skill", ("note_search", "write_file"), "Build a client brief from the relevant saved context."),
            ("Scope Builder", "agent", ("note_search", "write_file", "task_create"), "Turn a client objective into scope, milestones, and open questions."),
            ("Weekly Status", "routine", ("task_list", "brief"), "Prepare a concise weekly client status update."),
            ("Deliverable QA", "agent", ("read_file", "check_syntax"), "Review a deliverable against its stated requirements."),
            ("Renewal Brief", "skill", ("chat_search", "note_search"), "Prepare a renewal brief using outcomes, risks, and next opportunities."),
        ),
    },
    {
        "id": "sales",
        "name": "Sales",
        "capabilities": (
            ("Lead Research", "agent", ("web_search", "web_fetch"), "Research a lead and identify credible reasons to engage."),
            ("Follow-up Cadence", "routine", ("reminder_add", "inbox_capture"), "Design a respectful follow-up cadence and capture the actions."),
            ("Proposal Draft", "skill", ("note_search", "write_file"), "Draft a proposal grounded in the client's actual objective."),
            ("Pipeline Review", "manual", ("task_list", "inbox_list"), "Review my active opportunities and identify the next decision for each."),
        ),
    },
    {
        "id": "finance",
        "name": "Finance",
        "capabilities": (
            ("Receipt Capture", "manual", ("inbox_capture",), "Capture a finance document or receipt for later review."),
            ("Document Audit", "skill", ("read_file", "grep"), "Audit a finance document and flag inconsistencies or missing information."),
            ("Monthly Review", "routine", ("note_search", "reminder_list"), "Run a monthly finance review using my locally available records."),
            ("Subscription Audit", "agent", ("note_search", "inbox_list"), "Find subscription commitments in my local context and prepare an audit list."),
        ),
    },
    {
        "id": "ops",
        "name": "Ops / Custom",
        "custom": True,
        "capabilities": (
            ("Skill Creator", "manual", ("skill",), "Help me design a reusable Cagentic skill for this workflow."),
            ("Routine Manager", "skill", ("routine_list", "routine_update"), "Review and improve my proactive routine schedule."),
            ("Connector Health", "routine", ("calendar_connection_list", "email_connection_list", "mcp_list_servers"), "Check my connections and tell me which ones need attention."),
            ("Tool Discovery", "skill", ("tool_search",), "Find the best available tools for the workflow I describe."),
            ("Delegated Task", "agent", ("agent_call", "task_create"), "Delegate a bounded task and report its result."),
        ),
    },
)


def _capability(
    spec: tuple[str, str, tuple[str, ...], str],
    available_tools: set[str],
    active_tools: set[str],
) -> dict[str, Any]:
    name, kind, tools, prompt = spec
    present = [tool for tool in tools if tool in available_tools]
    return {
        "id": name.casefold().replace(" ", "-").replace("/", "-"),
        "name": name,
        "kind": kind,
        "tools": list(tools),
        "available": len(present) == len(tools),
        "active": all(tool in active_tools for tool in tools),
        "available_tools": present,
        "prompt": prompt,
    }


def build_architecture(
    *,
    available_tools: Iterable[str],
    active_tools: Iterable[str],
    routines: list[dict] | None = None,
    integrations: list[dict] | None = None,
    installed_skills: Iterable[str] = (),
    model: str = "",
) -> dict[str, Any]:
    """Return the live four-layer OS architecture used by the gateway."""
    available = set(available_tools)
    active = set(active_tools)
    routine_items = list(routines or [])
    connection_items = list(integrations or [])
    skill_items = sorted({str(name).strip() for name in installed_skills if str(name).strip()})
    branches = []
    for branch in BRANCHES:
        capabilities = [
            _capability(spec, available, active) for spec in branch["capabilities"]
        ]
        if branch["id"] == "ops":
            capabilities.extend(
                {
                    "id": "installed-" + name.casefold().replace(" ", "-"),
                    "name": name,
                    "kind": "skill",
                    "tools": ["skill"],
                    "available": "skill" in available,
                    "active": "skill" in active,
                    "available_tools": ["skill"] if "skill" in available else [],
                    "prompt": f"Use my installed {name} skill for the next task.",
                    "installed": True,
                }
                for name in skill_items[:30]
            )
        branches.append(
            {
                "id": branch["id"],
                "name": branch["name"],
                "foundation": bool(branch.get("foundation")),
                "custom": bool(branch.get("custom")),
                "capabilities": capabilities,
                "available": sum(1 for item in capabilities if item["available"]),
            }
        )

    mode_counts = {mode["id"]: 0 for mode in MODES}
    for branch in branches:
        for capability in branch["capabilities"]:
            mode_counts[capability["kind"]] += 1
    mode_counts["routine"] += len(routine_items)

    return {
        "conductor": {
            "name": "Cagentic",
            "role": "The conductor",
            "model": model or "local model",
        },
        "modes": [{**mode, "count": mode_counts[mode["id"]]} for mode in MODES],
        "branches": branches,
        "automation": {
            "enabled_routines": len(
                [item for item in routine_items if item.get("enabled", True)]
            ),
            "triggers": ("on demand", "scheduled", "proactive monitor", "delegated"),
        },
        "integrations": connection_items,
        "status": {
            "available_tools": len(available),
            "active_tools": len(active),
            "capabilities": sum(len(item["capabilities"]) for item in branches),
            "routines": len(routine_items),
            "integrations": len(connection_items),
            "installed_skills": len(skill_items),
        },
    }
