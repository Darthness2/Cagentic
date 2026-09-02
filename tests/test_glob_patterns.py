"""Glob pattern semantics and walk cost.

`**/x` used to be handed straight to `fnmatch`, which has no concept of `**` —
to it that is just "any characters", so the pattern required a literal `/` and
silently missed `x` sitting in the root of the directory being searched. That is
the single most natural pattern a model writes, and answering "(no matches)" for
a file it can plainly see sends it into a retry loop: this was caught by an agent
burning twenty minutes re-globbing for a `pyproject.toml` in the repo root.

The walk also enumerated the entire tree — `.venv`, `.git`, `node_modules` and
all — before throwing the ignored paths away, so every glob paid for directories
it could never return.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cagentic.state import AppState
from cagentic.tools import ToolContext, t_glob


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "README.md").write_text("# hi\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "deep.py").write_text("y = 2\n")
    (tmp_path / ".gitignore").write_text("ignored_dir/\n")
    (tmp_path / "ignored_dir").mkdir()
    (tmp_path / "ignored_dir" / "hidden.py").write_text("z = 3\n")
    return tmp_path


@pytest.fixture()
def ctx(tree: Path) -> ToolContext:
    return ToolContext(root=tree, state=AppState(workspace=tree, home=tree))


def found(out: str) -> set[str]:
    return set() if out == "(no matches)" else set(out.splitlines())


def test_double_star_matches_a_file_in_the_root(ctx):
    # The regression: zero intervening directories still counts.
    assert "pyproject.toml" in found(t_glob({"pattern": "**/pyproject.toml"}, ctx))


def test_double_star_still_matches_at_depth(ctx):
    hits = found(t_glob({"pattern": "**/*.py"}, ctx))
    assert {"pkg/core.py", "pkg/sub/deep.py"} <= hits


def test_double_star_spans_root_and_depth_together(ctx, tree):
    (tree / "top.py").write_text("t = 0\n")
    hits = found(t_glob({"pattern": "**/*.py"}, ctx))
    assert {"top.py", "pkg/core.py", "pkg/sub/deep.py"} <= hits


def test_a_bare_pattern_stays_top_level_only(ctx):
    # No `**` and no `/` means "this directory", which must not start matching
    # nested files just because the walk changed.
    hits = found(t_glob({"pattern": "*.py"}, ctx))
    assert "pkg/core.py" not in hits


def test_ignored_directories_are_not_returned(ctx):
    hits = found(t_glob({"pattern": "**/*.py"}, ctx))
    assert not any(h.startswith("ignored_dir") for h in hits)


def test_ignored_directories_are_not_even_walked(ctx, tree, monkeypatch):
    """Pruning, not filtering — the point of the change was the cost."""
    import os

    from cagentic.gitignore import matcher_for

    # Warm the ignore matcher first. It does its own full walk hunting for
    # nested .gitignore files — it cannot prune on rules it has not read yet —
    # and that walk is cached, so counting it here would measure the wrong thing.
    matcher_for(tree)

    seen: list[Path] = []
    real_walk = os.walk

    def spy(top, *a, **k):
        for dirpath, dirnames, filenames in real_walk(top, *a, **k):
            seen.append(Path(dirpath))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", spy)
    t_glob({"pattern": "**/*.py"}, ctx)

    # Compare path components, not substrings: pytest names tmp_path after the
    # test, so "ignored_dir" appears in the temp directory's own name.
    walked = {part for p in seen for part in p.parts}
    assert seen, "expected the glob to walk at all"
    assert "ignored_dir" not in walked, "walked into a directory it had already decided to ignore"


def test_nested_ignored_directories_are_pruned_at_any_depth(tmp_path):
    """The ancestors_clear fast path is only sound because the walk prunes.

    It skips the per-rule ancestor probe on the assumption that a directory we
    descended into was already cleared. If pruning and the fast path ever
    disagree, a file under a deeply nested ignored directory is what leaks.
    """
    (tmp_path / ".gitignore").write_text("build/\n")
    for d in ("src/deep/build", "build"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "src" / "ok.py").write_text("")
    (tmp_path / "src" / "deep" / "build" / "buried.py").write_text("")
    (tmp_path / "build" / "top.py").write_text("")

    ctx = ToolContext(root=tmp_path, state=AppState(workspace=tmp_path, home=tmp_path))
    hits = found(t_glob({"pattern": "**/*.py"}, ctx))

    assert "src/ok.py" in hits
    assert "src/deep/build/buried.py" not in hits
    assert "build/top.py" not in hits


def test_negation_survives_the_fast_path(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n!keep.log\n")
    (tmp_path / "drop.log").write_text("")
    (tmp_path / "keep.log").write_text("")

    ctx = ToolContext(root=tmp_path, state=AppState(workspace=tmp_path, home=tmp_path))
    hits = found(t_glob({"pattern": "**/*.log"}, ctx))

    assert hits == {"keep.log"}


def test_a_huge_tree_is_bounded_rather_than_endless(tmp_path):
    """An unbounded `**/x` over a large root is what started all of this: the
    workspace defaults to $HOME when launched from the install directory, and a
    glob there ran for twenty minutes before anyone saw a result."""
    for i in range(60):
        d = tmp_path / f"d{i}"
        d.mkdir()
        for j in range(60):
            (d / f"f{j}.txt").write_text("")

    ctx = ToolContext(root=tmp_path, state=AppState(workspace=tmp_path, home=tmp_path))
    import cagentic.tools as tools_mod

    monkey = getattr(tools_mod, "t_glob")
    out = monkey({"pattern": "**/nothing_matches_this"}, ctx)
    # 3,660 entries is under the cap, so this must complete rather than truncate.
    assert out == "(no matches)"
