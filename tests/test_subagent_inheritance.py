"""What a sub-agent inherits from the engine that forked it.

`fork_subagent` used to copy `yolo` and leave every *restriction* behind, so the
one permissive flag propagated while `dry_run`, the project deny rules, plan mode
and the enabled tool groups all reset to their permissive defaults. That is the
wrong direction for all four: a sub-agent forked from a `--dry-run` session could
write files, and `permissions.can_use_tool` documents `dry_run` as a gate that
not even yolo opens.

These tests pin the direction rather than the mechanism — a sub-agent may be
allowed less than its parent, never more.
"""

from __future__ import annotations

from pathlib import Path

from cagentic import subagent
from cagentic.permissions import can_use_tool
from cagentic.state import AppState


class _FakeEngine:
    """Captures the AppState fork_subagent builds, without running a turn."""

    captured: list[AppState] = []

    def __init__(self, *, state, **_kwargs):
        _FakeEngine.captured.append(state)
        self.messages = [{"role": "system", "content": "sys"}]

    def submit_message(self, _prompt):
        return iter(())


def fork_state(parent_state: AppState, monkeypatch) -> AppState:
    """Fork a sub-agent off `parent_state` and return the child's state."""
    _FakeEngine.captured = []
    monkeypatch.setattr("cagentic.engine.QueryEngine", _FakeEngine)

    class _Parent:
        state = parent_state
        client = object()
        model = "test"
        temperature = 0.0
        config: dict = {}

    subagent.fork_subagent(_Parent(), "go")
    assert _FakeEngine.captured, "fork_subagent never built a child engine"
    return _FakeEngine.captured[0]


def parent_state(**extra) -> AppState:
    return AppState(workspace=Path.cwd(), home=Path.home(), **extra)


def test_dry_run_reaches_the_child(monkeypatch):
    child = fork_state(parent_state(dry_run=True), monkeypatch)
    assert child.dry_run is True


def test_plan_mode_reaches_the_child(monkeypatch):
    child = fork_state(parent_state(plan_mode=True), monkeypatch)
    assert child.plan_mode is True


def test_deny_rules_reach_the_child(monkeypatch):
    rules = {"deny": ["run_bash"], "allow": []}
    child = fork_state(parent_state(permission_rules=rules), monkeypatch)
    assert child.permission_rules == rules


def test_tool_groups_reach_the_child(monkeypatch):
    child = fork_state(parent_state(tool_groups={"files"}), monkeypatch)
    assert child.tool_groups == {"files"}


def test_the_child_gets_a_fresh_approval_cache(monkeypatch):
    # Restrictions carry; a record of answers already given does not.
    child = fork_state(parent_state(permissions={"run_bash": "always"}), monkeypatch)
    assert child.permissions == {}


def test_inherited_state_is_copied_not_shared(monkeypatch):
    rules = {"deny": ["run_bash"], "allow": []}
    parent = parent_state(permission_rules=rules, tool_groups={"files"})
    child = fork_state(parent, monkeypatch)

    child.permission_rules["deny"] = []
    child.tool_groups.add("shell")

    assert parent.permission_rules["deny"] == ["run_bash"]
    assert parent.tool_groups == {"files"}


def test_a_deny_rule_still_bites_inside_a_yolo_subagent(monkeypatch):
    """The case that motivated all of this.

    yolo was the one flag that *was* inherited, so a parent running yolo with a
    standing project deny rule forked a child that had the yolo and none of the
    rule.
    """
    parent = parent_state(yolo=True, permission_rules={"deny": ["run_bash"], "allow": []})
    child = fork_state(parent, monkeypatch)

    assert child.yolo is True
    ok, _reason = can_use_tool("run_bash", {"command": "rm -rf /"}, child, None)
    assert ok is False
