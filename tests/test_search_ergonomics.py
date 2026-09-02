"""grep context/filter/mode options, and glob's mtime order.

grep could only ever answer "every matching line in full". No surrounding
context, so the model re-read whole files to see what a hit was inside; no file
filter, so a search for a Python symbol also dredged up its own name out of
lockfiles and minified bundles; no cheap mode, so "which files mention X" cost
the text of every hit.

Both search branches are tested — ripgrep when it is installed, and the pure
Python fallback with `rg` hidden. They have to agree: an answer that changes
depending on what happens to be installed is worse than either answer alone,
and the existing `--no-require-git` comment in `t_grep` exists because that bug
has already happened once here.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

import cagentic.tools as tools
from cagentic.state import AppState
from cagentic.tools import ToolContext, t_glob, t_grep


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / "code.py").write_text("import os\nTARGET = 1\nprint(TARGET)\ntrailer\n")
    (root / "notes.txt").write_text("TARGET appears here as well\n")
    return root


@pytest.fixture()
def ctx(tree: Path) -> ToolContext:
    return ToolContext(root=tree, state=AppState(workspace=tree, home=tree))


@pytest.fixture(params=["ripgrep", "fallback"])
def either_backend(request, monkeypatch):
    """Run the test once per search implementation."""
    if request.param == "fallback":
        real = shutil.which
        monkeypatch.setattr(shutil, "which", lambda n: None if n == "rg" else real(n))
    elif shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    return request.param


def rels(out: str, root: Path) -> list[str]:
    return [ln.replace(f"{root}/", "") for ln in out.splitlines()]


def test_context_includes_surrounding_lines(ctx, tree, either_backend):
    out = rels(t_grep({"pattern": "TARGET = 1", "context": 1}, ctx), tree)
    body = "\n".join(out)
    assert "import os" in body, "the line before the hit should be included"
    assert "print(TARGET)" in body, "the line after the hit should be included"


def test_no_context_by_default(ctx, tree, either_backend):
    body = "\n".join(rels(t_grep({"pattern": "TARGET = 1"}, ctx), tree))
    assert "import os" not in body


def test_before_and_after_are_separately_controllable(ctx, tree, either_backend):
    before = "\n".join(rels(t_grep({"pattern": "TARGET = 1", "before": 1}, ctx), tree))
    assert "import os" in before
    assert "print(TARGET)" not in before

    after = "\n".join(rels(t_grep({"pattern": "TARGET = 1", "after": 1}, ctx), tree))
    assert "print(TARGET)" in after
    assert "import os" not in after


def test_glob_restricts_which_files_are_searched(ctx, tree, either_backend):
    body = "\n".join(rels(t_grep({"pattern": "TARGET", "glob": "*.py"}, ctx), tree))
    assert "code.py" in body
    assert "notes.txt" not in body


def test_files_with_matches_lists_filenames_only(ctx, tree, either_backend):
    out = rels(t_grep({"pattern": "TARGET", "mode": "files_with_matches"}, ctx), tree)
    assert sorted(out) == ["code.py", "notes.txt"]


def test_count_reports_per_file_totals(ctx, tree, either_backend):
    out = sorted(rels(t_grep({"pattern": "TARGET", "mode": "count"}, ctx), tree))
    assert out == ["code.py:2", "notes.txt:1"]


def test_an_unknown_mode_falls_back_to_content(ctx, tree, either_backend):
    body = "\n".join(rels(t_grep({"pattern": "TARGET", "mode": "nonsense"}, ctx), tree))
    assert "TARGET = 1" in body


def test_context_is_capped_rather_than_unbounded(ctx, tree, either_backend):
    # A model asking for context=100000 should not get the whole repository.
    out = t_grep({"pattern": "TARGET = 1", "context": 10**6}, ctx)
    assert out.count("\n") < 50


def test_glob_sorts_by_name_by_default(tmp_path):
    root = tmp_path.resolve()
    for name in ("c.py", "a.py", "b.py"):
        (root / name).write_text("x\n")
        time.sleep(0.02)
    ctx = ToolContext(root=root, state=AppState(workspace=root, home=root))
    assert t_glob({"pattern": "**/*.py"}, ctx).split() == ["a.py", "b.py", "c.py"]


def test_glob_can_sort_newest_first(tmp_path):
    root = tmp_path.resolve()
    for name in ("oldest.py", "middle.py", "newest.py"):
        (root / name).write_text("x\n")
        time.sleep(0.02)
    ctx = ToolContext(root=root, state=AppState(workspace=root, home=root))
    out = t_glob({"pattern": "**/*.py", "sort": "mtime"}, ctx).split()
    assert out == ["newest.py", "middle.py", "oldest.py"]


def test_grep_option_parsing_is_defensive():
    assert tools._grep_options({}) == (0, 0, "", "content")
    # Junk must not raise on the way to the search.
    assert tools._grep_options({"context": "lots"}) == (0, 0, "", "content")
    assert tools._grep_options({"context": -5}) == (0, 0, "", "content")
    before, after, _, _ = tools._grep_options({"context": 3})
    assert (before, after) == (3, 3)
