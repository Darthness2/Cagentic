"""Fine-grained permission rule regressions.

The gate used to be all-or-nothing: a per-tool "always"/"never" cache, with
yolo as the only escape hatch. In practice that meant three prompts and then
`yolo`, which approves *everything* for the session — a worse posture than
being able to say "always allow `git status`, keep asking about `git push`".
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cagentic.permissions import (
    can_use_tool,
    effective_rules,
    suggest_rule,
)
from cagentic.state import AppState


def _deny(*_a, **_k) -> str:
    return "no"


def _approve(*_a, **_k) -> str:
    return "yes"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = AppState(workspace=self.root, home=self.root)

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass


class TestAllowRules(_Base):
    def test_command_prefix_rule_allows_only_its_prefix(self) -> None:
        self.state.update(permission_rules={"allow": ["run_bash(git status*)"]})
        ok, why = can_use_tool("run_bash", {"command": "git status --short"}, self.state, _deny)
        self.assertTrue(ok, why)
        ok, _ = can_use_tool("run_bash", {"command": "git push origin main"}, self.state, _deny)
        self.assertFalse(ok, "a status rule must not cover push")

    def test_bare_tool_rule_allows_the_whole_tool(self) -> None:
        self.state.update(permission_rules={"allow": ["write_file"]})
        ok, _ = can_use_tool("write_file", {"path": "anywhere.txt"}, self.state, _deny)
        self.assertTrue(ok)

    def test_path_glob_rule(self) -> None:
        self.state.update(permission_rules={"allow": ["edit_file(src/*)"]})
        ok, _ = can_use_tool("edit_file", {"path": "src/app/main.py"}, self.state, _deny)
        self.assertTrue(ok)
        ok, _ = can_use_tool("edit_file", {"path": "secrets/key.pem"}, self.state, _deny)
        self.assertFalse(ok)

    def test_rule_for_a_different_tool_does_not_leak(self) -> None:
        self.state.update(permission_rules={"allow": ["run_bash(ls*)"]})
        ok, _ = can_use_tool("write_file", {"path": "ls"}, self.state, _deny)
        self.assertFalse(ok)


class TestDenyRules(_Base):
    def test_deny_beats_yolo(self) -> None:
        """The whole point of a deny list: nothing below can switch it off."""
        self.state.update(yolo=True, permission_rules={"deny": ["run_bash(rm -rf*)"]})
        ok, why = can_use_tool("run_bash", {"command": "rm -rf /"}, self.state, _approve)
        self.assertFalse(ok)
        self.assertIn("denied by rule", why)

    def test_deny_beats_the_always_cache(self) -> None:
        self.state.permissions["run_bash"] = "always"
        self.state.update(permission_rules={"deny": ["run_bash(curl*)"]})
        ok, _ = can_use_tool("run_bash", {"command": "curl evil.example"}, self.state, _approve)
        self.assertFalse(ok)

    def test_deny_beats_an_allow_for_the_same_call(self) -> None:
        self.state.update(
            permission_rules={"allow": ["run_bash(git*)"], "deny": ["run_bash(git push*)"]}
        )
        ok, _ = can_use_tool("run_bash", {"command": "git status"}, self.state, _deny)
        self.assertTrue(ok)
        ok, _ = can_use_tool("run_bash", {"command": "git push --force"}, self.state, _approve)
        self.assertFalse(ok)


class TestPlanModeStillWins(_Base):
    def test_allow_rule_cannot_override_plan_mode(self) -> None:
        self.state.update(plan_mode=True, permission_rules={"allow": ["write_file"]})
        ok, why = can_use_tool("write_file", {"path": "a.txt"}, self.state, _approve)
        self.assertFalse(ok)
        self.assertIn("plan mode", why)

    def test_allow_rule_cannot_override_dry_run(self) -> None:
        self.state.update(dry_run=True, permission_rules={"allow": ["write_file"]})
        ok, why = can_use_tool("write_file", {"path": "a.txt"}, self.state, _approve)
        self.assertFalse(ok)
        self.assertIn("dry run", why)


class TestAcceptEditsMode(_Base):
    def test_workspace_edits_pass_without_prompting(self) -> None:
        self.state.update(approval_mode="accept_edits")
        ok, why = can_use_tool("edit_file", {"path": "notes.md"}, self.state, _deny)
        self.assertTrue(ok, why)

    def test_shell_still_asks(self) -> None:
        self.state.update(approval_mode="accept_edits")
        ok, _ = can_use_tool("run_bash", {"command": "ls"}, self.state, _deny)
        self.assertFalse(ok, "accept_edits must not wave through shell commands")

    def test_edits_outside_the_workspace_still_ask(self) -> None:
        self.state.update(approval_mode="accept_edits")
        ok, _ = can_use_tool("write_file", {"path": "../../etc/hosts"}, self.state, _deny)
        self.assertFalse(ok, "the workspace bound is what makes this mode safe")

    def test_ask_mode_is_the_default(self) -> None:
        ok, _ = can_use_tool("edit_file", {"path": "notes.md"}, self.state, _deny)
        self.assertFalse(ok)


class TestProjectRules(_Base):
    def _write_settings(self, filename: str, payload: dict) -> None:
        d = self.root / ".cagentic"
        d.mkdir(exist_ok=True)
        (d / filename).write_text(json.dumps(payload), encoding="utf-8")

    def test_workspace_settings_contribute_rules(self) -> None:
        self._write_settings("settings.json", {"permissions": {"allow": ["run_bash(pytest*)"]}})
        ok, why = can_use_tool("run_bash", {"command": "pytest -q"}, self.state, _deny)
        self.assertTrue(ok, why)

    def test_local_layer_stacks_on_the_shared_one(self) -> None:
        self._write_settings("settings.json", {"permissions": {"allow": ["run_bash(pytest*)"]}})
        self._write_settings(
            "settings.local.json", {"permissions": {"deny": ["run_bash(pytest --runslow*)"]}}
        )
        rules = effective_rules(self.state)
        self.assertIn("run_bash(pytest*)", rules["allow"])
        self.assertIn("run_bash(pytest --runslow*)", rules["deny"])
        ok, _ = can_use_tool("run_bash", {"command": "pytest --runslow"}, self.state, _approve)
        self.assertFalse(ok)

    def test_malformed_settings_do_not_break_the_gate(self) -> None:
        d = self.root / ".cagentic"
        d.mkdir(exist_ok=True)
        (d / "settings.json").write_text("{not json", encoding="utf-8")
        # Must not raise, and must fall back to prompting.
        ok, _ = can_use_tool("run_bash", {"command": "ls"}, self.state, _deny)
        self.assertFalse(ok)

    def test_edited_settings_are_picked_up_without_a_restart(self) -> None:
        """Rules resolve per call, so /cd and file edits take effect at once."""
        self._write_settings("settings.json", {"permissions": {"allow": []}})
        ok, _ = can_use_tool("run_bash", {"command": "ls"}, self.state, _deny)
        self.assertFalse(ok)
        import os
        import time

        self._write_settings("settings.json", {"permissions": {"allow": ["run_bash(ls*)"]}})
        # Bump mtime explicitly: two writes inside one filesystem timestamp
        # tick would otherwise look unchanged to the cache.
        path = self.root / ".cagentic" / "settings.json"
        os.utime(path, (time.time() + 2, time.time() + 2))
        ok, why = can_use_tool("run_bash", {"command": "ls -la"}, self.state, _deny)
        self.assertTrue(ok, why)


class TestSuggestRule(unittest.TestCase):
    def test_shell_suggestion_is_the_first_two_words(self) -> None:
        self.assertEqual(
            suggest_rule("run_bash", {"command": "git status --short"}), "run_bash(git status*)"
        )

    def test_no_suggestion_for_compound_commands(self) -> None:
        """A pipeline is too varied to generalise into a safe standing rule."""
        self.assertEqual(suggest_rule("run_bash", {"command": "cat x | sh"}), "")
        self.assertEqual(suggest_rule("run_bash", {"command": "ls && rm -rf /"}), "")

    def test_file_suggestion_scopes_to_the_directory(self) -> None:
        self.assertEqual(
            suggest_rule("edit_file", {"path": "src/app/main.py"}), "edit_file(src/app/*)"
        )

    def test_no_suggestion_for_a_bare_filename(self) -> None:
        self.assertEqual(suggest_rule("edit_file", {"path": "main.py"}), "")

    def test_mcp_suggestion_scopes_to_the_server(self) -> None:
        self.assertEqual(
            suggest_rule("mcp_call", {"server": "notion", "tool": "search"}), "mcp_call(notion/*)"
        )


if __name__ == "__main__":
    unittest.main()
