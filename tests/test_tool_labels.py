"""Human phrasing for tool activity, and the panels setting."""

from __future__ import annotations

from cagentic.tool_labels import describe_tool, is_known_tool, label_pair
from cagentic.tools import all_tool_schemas


def test_every_shipped_tool_has_curated_phrasing():
    names = {(s.get("function") or {}).get("name", "") for s in all_tool_schemas(None)}
    missing = sorted(n for n in names if n and not is_known_tool(n))

    # A new tool should arrive with a phrase; the fallback exists for tools that
    # appear at runtime, not for ones shipped in this repo.
    assert missing == []


def test_identifiers_become_english():
    assert describe_tool("list_dir") == "Listed directory"
    assert describe_tool("run_bash") == "Ran command"
    assert describe_tool("web_search") == "Searched the web"
    assert describe_tool("write_file") == "Wrote file"


def test_repeats_collapse_into_a_count():
    # The case that prompted this: four directory listings on four lines.
    assert describe_tool("list_dir", 4) == "Listed 4 directories"
    assert describe_tool("read_file", 2) == "Read 2 files"
    assert describe_tool("run_bash", 3) == "Ran 3 commands"


def test_a_single_call_never_says_one():
    for name in ("list_dir", "read_file", "grep"):
        assert "1" not in describe_tool(name, 1)
        assert describe_tool(name, 0) == describe_tool(name, 1)


def test_runtime_tools_still_read_as_english():
    # MCP servers contribute tools this table has never seen.
    assert describe_tool("weather_get") == "Read weather"
    assert describe_tool("jira_issue_create") == "Created jira issue"
    # And something with no recognisable verb still avoids the bare identifier.
    assert describe_tool("frobnicate").startswith("Used ")


def test_an_empty_name_does_not_crash_a_renderer():
    assert describe_tool("")
    assert describe_tool(None)  # type: ignore[arg-type]


def test_clients_get_both_forms_so_they_can_collapse_alone():
    one, many = label_pair("list_dir")

    assert one == "Listed directory"
    # The slot is what lets a client render a count without another round trip.
    assert "{n}" in many
    assert many.format(n=7) == "Listed 7 directories"
