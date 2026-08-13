"""Phase 3e regressions — the non-interactive surface.

`-p` plus `--json` was the whole automation story: no way to resume a
conversation, no way to grant permissions without a human at the prompt, and a
single JSON blob only at the end. These are the flags that let Cagentic be
driven from a script or CI.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cagentic import cli
from cagentic.agent import Agent
from cagentic.engine import Message, event_payload


class _NullClient:
    pass


class TestEventPayload(unittest.TestCase):
    """Shared with the gateway's SSE mapping — two hand-written versions would
    drift the first time an event kind changed."""

    def test_kind_is_always_present(self) -> None:
        self.assertEqual(
            event_payload(Message("delta", {"text": "hi"})), {"kind": "delta", "text": "hi"}
        )

    def test_task_id_is_promoted_when_set(self) -> None:
        out = event_payload(Message("tool_call", {"name": "grep"}, task_id="t7"))
        self.assertEqual(out["task_id"], "t7")

    def test_absent_task_id_is_omitted_rather_than_null(self) -> None:
        self.assertNotIn("task_id", event_payload(Message("delta", {"text": "x"})))

    def test_inline_images_become_a_count(self) -> None:
        """Base64 screenshots would swamp a line-oriented stream."""
        out = event_payload(Message("tool_result", {"ok": True, "images": ["AAAA", "BBBB"]}))
        self.assertEqual(out["images"], 2)

    def test_every_payload_is_json_serialisable(self) -> None:
        for kind in ("delta", "assistant", "tool_call", "error", "done", "compact"):
            json.dumps(event_payload(Message(kind, {"text": "x"})), default=str)


class TestRuleParsing(unittest.TestCase):
    def test_comma_separated_rules(self) -> None:
        self.assertEqual(
            cli._split_rules("run_bash(git status*), read_file ,write_file"),
            ["run_bash(git status*)", "read_file", "write_file"],
        )

    def test_blank_and_none_are_empty(self) -> None:
        self.assertEqual(cli._split_rules(""), [])
        self.assertEqual(cli._split_rules(None), [])
        self.assertEqual(cli._split_rules(" , , "), [])


class _AgentCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg = tempfile.TemporaryDirectory()
        import os

        os.environ["XDG_CONFIG_HOME"] = self._cfg.name
        self.root = Path(self._tmp.name)
        self.agent = Agent(_NullClient(), "test-model", self.root)

    def tearDown(self) -> None:
        for d in (self._tmp, self._cfg):
            try:
                d.cleanup()
            except (OSError, PermissionError):
                pass

    def opts(self, **kw) -> cli.RuntimeOptions:
        return cli.RuntimeOptions(mode="run", **kw)


class TestPermissionModes(_AgentCase):
    """A script has nobody to answer y/n, so --permission-mode must reach the
    same states the slash commands set interactively."""

    def test_yolo(self) -> None:
        cli._apply_automation_options(self.agent, self.opts(permission_mode="yolo"))
        self.assertTrue(self.agent.state.yolo)

    def test_accept_edits(self) -> None:
        cli._apply_automation_options(self.agent, self.opts(permission_mode="accept-edits"))
        self.assertEqual(self.agent.state.approval_mode, "accept_edits")

    def test_plan(self) -> None:
        cli._apply_automation_options(self.agent, self.opts(permission_mode="plan"))
        self.assertTrue(self.agent.state.plan_mode)

    def test_ask_is_the_explicit_safe_state(self) -> None:
        self.agent.state.update(yolo=True, approval_mode="accept_edits")
        cli._apply_automation_options(self.agent, self.opts(permission_mode="ask"))
        self.assertFalse(self.agent.state.yolo)
        self.assertEqual(self.agent.state.approval_mode, "ask")

    def test_no_mode_changes_nothing(self) -> None:
        before = (self.agent.state.yolo, self.agent.state.approval_mode)
        cli._apply_automation_options(self.agent, self.opts())
        self.assertEqual((self.agent.state.yolo, self.agent.state.approval_mode), before)


class TestRuleFlags(_AgentCase):
    def test_allowed_tools_become_allow_rules(self) -> None:
        cli._apply_automation_options(
            self.agent, self.opts(allowed_tools="run_bash(git status*),read_file")
        )
        self.assertIn("run_bash(git status*)", self.agent.state.permission_rules["allow"])

    def test_disallowed_tools_become_deny_rules(self) -> None:
        cli._apply_automation_options(self.agent, self.opts(disallowed_tools="run_bash(rm*)"))
        self.assertIn("run_bash(rm*)", self.agent.state.permission_rules["deny"])

    def test_the_rules_actually_gate_a_call(self) -> None:
        """The flag is only worth anything if the gate honours it."""
        from cagentic.permissions import can_use_tool

        cli._apply_automation_options(
            self.agent,
            self.opts(allowed_tools="run_bash(git status*)", disallowed_tools="run_bash(rm*)"),
        )
        deny_all = lambda *a, **k: "no"  # noqa: E731
        ok, _ = can_use_tool("run_bash", {"command": "git status"}, self.agent.state, deny_all)
        self.assertTrue(ok)
        ok, why = can_use_tool(
            "run_bash", {"command": "rm -rf /"}, self.agent.state, lambda *a, **k: "yes"
        )
        self.assertFalse(ok)
        self.assertIn("denied by rule", why)

    def test_existing_config_rules_are_kept(self) -> None:
        self.agent.state.update(permission_rules={"allow": ["read_file"], "deny": []})
        cli._apply_automation_options(self.agent, self.opts(allowed_tools="glob"))
        self.assertEqual(sorted(self.agent.state.permission_rules["allow"]), ["glob", "read_file"])


class TestAppendSystemPrompt(_AgentCase):
    def test_the_text_reaches_the_system_prompt(self) -> None:
        cli._apply_automation_options(
            self.agent, self.opts(append_system_prompt="Always answer in French.")
        )
        self.assertIn("Always answer in French.", self.agent.engine.messages[0]["content"])

    def test_it_appends_rather_than_replacing(self) -> None:
        """The base prompt carries the tool contracts the model needs at all."""
        cli._apply_automation_options(self.agent, self.opts(append_system_prompt="Extra."))
        content = self.agent.engine.messages[0]["content"]
        self.assertIn("Extra.", content)
        self.assertIn("Cagentic", content)

    def test_an_existing_suffix_survives(self) -> None:
        self.agent.engine.system_suffix = "FIRST"
        cli._apply_automation_options(self.agent, self.opts(append_system_prompt="SECOND"))
        self.assertIn("FIRST", self.agent.engine.system_suffix)
        self.assertIn("SECOND", self.agent.engine.system_suffix)


class TestResume(_AgentCase):
    def _save(self, title: str, text: str) -> dict:
        from cagentic import sessions

        data = sessions.make("test-model")
        data["title"] = title
        data["messages"] = [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "noted"},
        ]
        sessions.save(data)
        return data

    def test_no_flags_resumes_nothing(self) -> None:
        self.assertIsNone(cli._resume_session(self.agent, self.opts()))

    def test_continue_loads_the_most_recent(self) -> None:
        self._save("older", "first thing")
        newest = self._save("newer", "second thing")
        loaded = cli._resume_session(self.agent, self.opts(continue_last=True))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["id"], newest["id"])
        self.assertTrue(any("second thing" in str(m.get("content")) for m in self.agent.messages))

    def test_resume_by_id(self) -> None:
        target = self._save("target", "the one I want")
        loaded = cli._resume_session(self.agent, self.opts(resume_id=target["id"]))
        self.assertEqual(loaded["id"], target["id"])

    def test_an_unknown_id_reports_instead_of_starting_blank(self) -> None:
        self._save("something", "x")
        self.assertIsNone(cli._resume_session(self.agent, self.opts(resume_id="no-such-session")))

    def test_resuming_keeps_writing_to_the_same_session(self) -> None:
        """Otherwise the resumed conversation silently forks in two."""
        target = self._save("target", "keep me")
        cli._resume_session(self.agent, self.opts(resume_id=target["id"]))
        self.assertEqual(self.agent.engine.session_id, target["id"])

    def test_nothing_saved_is_a_warning_not_a_crash(self) -> None:
        self.assertIsNone(cli._resume_session(self.agent, self.opts(continue_last=True)))


class TestFlagsAreRegistered(unittest.TestCase):
    def test_every_new_flag_appears_in_help(self) -> None:
        import click.testing

        result = click.testing.CliRunner().invoke(cli.cli, ["--help"])
        for flag in (
            "--continue",
            "--resume",
            "--allowed-tools",
            "--disallowed-tools",
            "--permission-mode",
            "--append-system-prompt",
            "stream-json",
        ):
            self.assertIn(flag, result.output, flag)

    def test_stream_json_reserves_stdout_for_events(self) -> None:
        """Start-up is chatty (port clashes, capability warnings). Any of that
        on stdout corrupts the stream the caller pipes into jq."""
        import contextlib
        import io
        import sys

        from cagentic import cli as cli_mod

        captured_out, captured_err = io.StringIO(), io.StringIO()
        seen: list[cli_mod.RuntimeOptions] = []

        def fake_runtime(options):
            # Whatever start-up prints lands wherever stdout points *now*.
            print("noisy start-up warning")
            seen.append(options)
            print(json.dumps({"kind": "done"}), file=options.stream_sink, flush=True)
            return 0

        original = cli_mod._run_runtime
        cli_mod._run_runtime = fake_runtime
        try:
            with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                code = cli_mod._run_stream_json(cli_mod.RuntimeOptions(mode="run", prompt="hi"))
        finally:
            cli_mod._run_runtime = original
            del sys

        self.assertEqual(code, 0)
        self.assertTrue(seen[0].stream_json)
        # stdout: only the event. stderr: the warning.
        self.assertEqual(captured_out.getvalue().strip(), '{"kind": "done"}')
        self.assertIn("noisy start-up warning", captured_err.getvalue())

    def test_stream_json_without_a_prompt_is_a_usage_error(self) -> None:
        """It emits per-turn events; there is no turn without a prompt."""
        self.assertEqual(cli.main(["--format", "stream-json"]), 2)


if __name__ == "__main__":
    unittest.main()
