"""Phase 3b regressions — per-project configuration under `.cagentic/`.

Everything used to live in `~/.config/cagentic/`, so a team could not check
agent configuration into version control: prompts, skills and approvals were
per-machine and invisible to everyone else on the repo.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cagentic.project_scope import (
    MAX_BODY_BYTES,
    command_summary,
    discover_commands,
    find_skill,
    list_skills,
    render_command,
)


class _Repo(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._cfg = tempfile.TemporaryDirectory()
        import os

        os.environ["XDG_CONFIG_HOME"] = self._cfg.name

    def tearDown(self) -> None:
        for d in (self._tmp, self._cfg):
            try:
                d.cleanup()
            except (OSError, PermissionError):
                pass

    def command(self, name: str, body: str) -> Path:
        d = self.root / ".cagentic" / "commands"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.md"
        p.write_text(body, encoding="utf-8")
        return p

    def project_skill(self, name: str, body: str) -> Path:
        d = self.root / ".cagentic" / "skills"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.md"
        p.write_text(body, encoding="utf-8")
        return p

    def user_skill(self, name: str, body: str) -> Path:
        from cagentic.config import config_dir

        d = config_dir() / "skills"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.md"
        p.write_text(body, encoding="utf-8")
        return p


class TestDiscovery(_Repo):
    def test_no_project_dir_is_not_an_error(self) -> None:
        self.assertEqual(discover_commands(self.root), {})
        self.assertEqual(list_skills(self.root), [])

    def test_commands_are_found_by_stem(self) -> None:
        self.command("review", "Review $ARGUMENTS carefully.")
        self.assertEqual(set(discover_commands(self.root)), {"review"})

    def test_unusable_names_are_skipped(self) -> None:
        """A filename that can't be typed as a slash command is not a command."""
        d = self.root / ".cagentic" / "commands"
        d.mkdir(parents=True, exist_ok=True)
        (d / "has space.md").write_text("x", encoding="utf-8")
        (d / "-leading.md").write_text("x", encoding="utf-8")
        (d / "ok.md").write_text("x", encoding="utf-8")
        self.assertEqual(set(discover_commands(self.root)), {"ok"})

    def test_oversized_bodies_are_skipped(self) -> None:
        """A stray binary in commands/ must not be pasted into the context."""
        self.command("huge", "x" * (MAX_BODY_BYTES + 1))
        self.assertEqual(discover_commands(self.root), {})

    def test_non_markdown_is_ignored(self) -> None:
        d = self.root / ".cagentic" / "commands"
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.txt").write_text("not a command", encoding="utf-8")
        self.assertEqual(discover_commands(self.root), {})


class TestRendering(unittest.TestCase):
    def test_arguments_are_substituted(self) -> None:
        out = render_command("Review $ARGUMENTS for bugs.", "src/app.py")
        self.assertEqual(out, "Review src/app.py for bugs.")

    def test_arguments_are_appended_when_the_template_ignores_them(self) -> None:
        """Otherwise `/review src/x.py` would silently drop the path."""
        out = render_command("Review the diff.", "src/x.py")
        self.assertEqual(out, "Review the diff.\n\nsrc/x.py")

    def test_no_arguments_leaves_the_template_alone(self) -> None:
        self.assertEqual(render_command("Review the diff.", ""), "Review the diff.")
        self.assertEqual(render_command("Check $ARGUMENTS now.", ""), "Check  now.")

    def test_front_matter_is_stripped(self) -> None:
        body = "---\ndescription: Review a file\n---\nReview $ARGUMENTS."
        self.assertEqual(render_command(body, "a.py"), "Review a.py.")

    def test_bare_description_line_is_stripped(self) -> None:
        body = "description: Review a file\nReview it."
        self.assertEqual(render_command(body, ""), "Review it.")


class TestSummaries(unittest.TestCase):
    def test_explicit_description_wins(self) -> None:
        body = "---\ndescription: Run the release checklist\n---\nDo the thing."
        self.assertEqual(command_summary(body), "Run the release checklist")

    def test_falls_back_to_the_first_prose_line(self) -> None:
        body = "# Release\n\n```bash\nmake\n```\nShip the current build."
        self.assertEqual(command_summary(body), "Ship the current build.")

    def test_empty_body_still_yields_something(self) -> None:
        self.assertEqual(command_summary(""), "project command")


class TestSkillPrecedence(_Repo):
    def test_project_skill_shadows_the_users(self) -> None:
        """A repo shipping its own review process should beat whatever the
        individual developer happens to have installed globally."""
        self.user_skill("review", "GLOBAL")
        self.project_skill("review", "PROJECT")
        found = find_skill(self.root, "review")
        self.assertIsNotNone(found)
        self.assertEqual(found.read_text(encoding="utf-8"), "PROJECT")

    def test_user_skill_is_used_when_the_project_has_none(self) -> None:
        self.user_skill("review", "GLOBAL")
        self.assertEqual(find_skill(self.root, "review").read_text(encoding="utf-8"), "GLOBAL")

    def test_listing_labels_the_origin_and_dedupes(self) -> None:
        self.user_skill("review", "GLOBAL")
        self.user_skill("only-global", "G")
        self.project_skill("review", "PROJECT")
        self.assertEqual(
            list_skills(self.root), [("only-global", "user"), ("review", "project")]
        )

    def test_unknown_and_unsafe_names_return_nothing(self) -> None:
        self.assertIsNone(find_skill(self.root, "nope"))
        self.assertIsNone(find_skill(self.root, "../../etc/passwd"))
        self.assertIsNone(find_skill(self.root, ""))


class TestBuiltinsCannotBeShadowed(unittest.TestCase):
    def test_project_commands_never_override_a_builtin(self) -> None:
        """A repo defining `quit.md` must not be able to take over /quit."""
        from cagentic.cli import _BUILTIN_COMMAND_NAMES

        for name in ("quit", "yolo", "rules", "rewind", "init", "help"):
            self.assertIn(name, _BUILTIN_COMMAND_NAMES, name)

    def test_the_builtin_set_is_derived_from_the_catalog(self) -> None:
        """Hand-maintaining it would let it drift as commands are added."""
        from cagentic.cli import _BUILTIN_COMMAND_NAMES
        from cagentic.prompt import COMMAND_GROUPS

        catalog = {n.lstrip("/") for _s, e in COMMAND_GROUPS for n, _a, _h in e}
        self.assertTrue(catalog <= _BUILTIN_COMMAND_NAMES)


class TestSkillToolUsesProjectScope(_Repo):
    def test_the_skill_tool_finds_a_project_skill(self) -> None:
        from cagentic.state import AppState
        from cagentic.tools import ToolContext, t_skill

        self.project_skill("house-style", "Always use tabs in this repo.")
        state = AppState(workspace=self.root, home=self.root)
        ctx = ToolContext(root=self.root, state=state)
        self.assertIn("house-style", t_skill({"op": "list"}, ctx))
        self.assertIn("tabs", t_skill({"op": "get", "name": "house-style"}, ctx))


class TestProjectFacts(_Repo):
    def test_names_the_build_files_and_test_dir(self) -> None:
        from cagentic.cli import _project_facts

        (self.root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (self.root / "src").mkdir()
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("", encoding="utf-8")

        facts = _project_facts(self.root)
        self.assertIn("pyproject.toml", facts)
        self.assertIn("src/", facts)
        self.assertIn("test_a.py", facts)

    def test_empty_workspace_does_not_raise(self) -> None:
        from cagentic.cli import _project_facts

        self.assertTrue(_project_facts(self.root))


if __name__ == "__main__":
    unittest.main()
