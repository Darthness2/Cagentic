"""Phase 3d regressions — git awareness in search and the prompt.

`grep` shells out to ripgrep when available and falls back to Python when not.
Ripgrep skips gitignored files; the fallback used a hardcoded list of directory
names. So the *same* search returned different results depending on whether the
user happened to have `rg` installed — a latent bug, not just a missing feature.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cagentic.gitignore import IgnoreMatcher, branch_label


class _Tree(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def write(self, rel: str, text: str = "x") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def ignore(self, text: str) -> None:
        (self.root / ".gitignore").write_text(text, encoding="utf-8")


class TestMatching(_Tree):
    def test_no_gitignore_ignores_nothing_but_dot_git(self) -> None:
        keep = self.write("src/app.py")
        gitdir = self.write(".git/objects/ab/cdef")
        m = IgnoreMatcher(self.root)
        self.assertFalse(m.is_ignored(keep))
        self.assertTrue(m.is_ignored(gitdir))

    def test_directory_rule_covers_its_contents(self) -> None:
        self.ignore("node_modules/\n")
        inner = self.write("node_modules/pkg/index.js")
        self.assertTrue(IgnoreMatcher(self.root).is_ignored(inner))

    def test_glob_matches_at_any_depth(self) -> None:
        self.ignore("*.log\n")
        m = IgnoreMatcher(self.root)
        self.assertTrue(m.is_ignored(self.write("debug.log")))
        self.assertTrue(m.is_ignored(self.write("deep/nested/other.log")))
        self.assertFalse(m.is_ignored(self.write("keep.txt")))

    def test_negation_wins_when_it_comes_last(self) -> None:
        self.ignore("*.log\n!important.log\n")
        m = IgnoreMatcher(self.root)
        self.assertTrue(m.is_ignored(self.write("debug.log")))
        self.assertFalse(m.is_ignored(self.write("important.log")))

    def test_anchored_rule_only_matches_at_its_own_level(self) -> None:
        self.ignore("/dist\n")
        m = IgnoreMatcher(self.root)
        self.assertTrue(m.is_ignored(self.write("dist/bundle.js")))
        self.assertFalse(m.is_ignored(self.write("packages/web/dist/bundle.js")))

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        self.ignore("# a comment\n\n*.log\n")
        m = IgnoreMatcher(self.root)
        self.assertFalse(m.is_ignored(self.write("comment")))
        self.assertTrue(m.is_ignored(self.write("x.log")))

    def test_a_nested_gitignore_only_governs_its_own_subtree(self) -> None:
        (self.root / "web").mkdir()
        (self.root / "web" / ".gitignore").write_text("build/\n", encoding="utf-8")
        m = IgnoreMatcher(self.root)
        self.assertTrue(m.is_ignored(self.write("web/build/out.js")))
        self.assertFalse(m.is_ignored(self.write("api/build/out.js")))

    def test_double_star_spans_directories(self) -> None:
        self.ignore("src/**/tmp\n")
        m = IgnoreMatcher(self.root)
        self.assertTrue(m.is_ignored(self.write("src/a/b/tmp")))

    def test_a_path_outside_the_root_is_never_ignored(self) -> None:
        self.ignore("*\n")
        self.assertFalse(IgnoreMatcher(self.root).is_ignored(Path("/etc/hosts")))


class TestSearchPathsAgree(_Tree):
    """The point of the whole module: rg and the Python fallback must return
    the same files."""

    def _grep_files(self, use_rg: bool) -> list[str]:
        import cagentic.tools as tools_mod
        from cagentic.tools import ToolContext, t_grep

        ctx = ToolContext(root=self.root)
        real_which = tools_mod.shutil.which if hasattr(tools_mod, "shutil") else shutil.which
        original = shutil.which
        if not use_rg:
            shutil.which = lambda name: None if name == "rg" else original(name)
        try:
            out = t_grep({"pattern": "NEEDLE", "path": "."}, ctx)
        finally:
            shutil.which = original
        del real_which
        return sorted(
            {line.split(":")[0].rsplit("/", 1)[-1] for line in out.splitlines() if ":" in line}
        )

    def test_both_paths_skip_the_same_ignored_files(self) -> None:
        self.ignore("node_modules/\n.venv/\n*.log\n!important.log\n")
        for rel in (
            "src/app.py",
            "node_modules/pkg/index.js",
            ".venv/lib/thing.py",
            "debug.log",
            "important.log",
        ):
            self.write(rel, "NEEDLE here")

        python_path = self._grep_files(use_rg=False)
        self.assertEqual(python_path, ["app.py", "important.log"])
        if shutil.which("rg"):
            self.assertEqual(self._grep_files(use_rg=True), python_path)


class TestGlobRespectsIgnores(_Tree):
    def test_glob_skips_ignored_directories(self) -> None:
        from cagentic.tools import ToolContext, t_glob

        self.ignore("node_modules/\n")
        self.write("src/app.py")
        self.write("node_modules/pkg/index.js")
        out = t_glob({"pattern": "**/*.py", "path": "."}, ToolContext(root=self.root))
        self.assertIn("app.py", out)
        self.assertNotIn("node_modules", out)


class TestBranchLabel(_Tree):
    def test_a_non_repository_yields_nothing(self) -> None:
        """An empty label is what makes the toolbar hide the segment entirely
        rather than showing a confusing placeholder."""
        self.assertEqual(branch_label(self.root), "")

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_a_dirty_tree_is_starred(self) -> None:
        import subprocess

        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "t@test"], capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "T"], capture_output=True
        )
        self.write("a.txt", "hello")
        subprocess.run(["git", "-C", str(self.root), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "x"], capture_output=True)

        from cagentic import gitignore

        gitignore._STATUS_CACHE.clear()
        clean = branch_label(self.root)
        self.assertTrue(clean and not clean.endswith("*"), clean)

        self.write("b.txt", "dirty now")
        gitignore._STATUS_CACHE.clear()
        self.assertTrue(branch_label(self.root).endswith("*"))


class TestToolbarShowsBranch(unittest.TestCase):
    def test_branch_appears_next_to_the_workspace(self) -> None:
        from cagentic.prompt import _toolbar_text

        text = _toolbar_text(
            {"workspace": "/tmp/proj", "branch": "main*", "model": "m"}, columns=120
        )
        self.assertIn("(main*)", text)

    def test_no_branch_means_no_parentheses(self) -> None:
        from cagentic.prompt import _toolbar_text

        text = _toolbar_text({"workspace": "/tmp/proj", "branch": "", "model": "m"}, columns=120)
        self.assertNotIn("()", text)

    def test_a_hostile_branch_name_cannot_inject_escapes(self) -> None:
        from cagentic.prompt import _toolbar_text

        text = _toolbar_text(
            {"workspace": "/tmp/p", "branch": "ma\x1b[31min\nx", "model": "m"}, columns=120
        )
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\n", text)


if __name__ == "__main__":
    unittest.main()
