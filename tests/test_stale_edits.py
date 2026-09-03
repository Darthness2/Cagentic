"""Refusing to edit a file that changed underneath the agent.

Every edit path rewrites the whole file from text the model is holding in
context. If somebody edits that file in their editor while a turn is running,
the next edit silently discards their work — the agent has no idea anything
moved. `_stale_file_error` compares the file's fingerprint against the one
recorded when we last read or wrote it.

Both halves of the same question — whether we actually know what the file says:
a file never read is refused, and so is one that changed since we read it. What
is not refused is creating a file, which is neither.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cagentic.coding_tools import t_multi_edit
from cagentic.state import AppState
from cagentic.tools import (
    ToolContext,
    t_edit_file,
    t_read_file,
    t_replace_lines,
    t_write_file,
)


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    state = AppState(workspace=tmp_path, home=tmp_path)
    return ToolContext(root=tmp_path, state=state, read_cache={})


@pytest.fixture()
def sample(tmp_path: Path) -> Path:
    p = tmp_path / "sample.py"
    p.write_text("alpha = 1\nbeta = 2\ngamma = 3\n")
    return p


def touch_externally(p: Path, text: str) -> None:
    """Stand in for the user saving the file in their editor mid-turn."""
    p.write_text(text)


def test_edit_after_an_external_change_is_refused(ctx, sample):
    t_read_file({"path": "sample.py"}, ctx)
    touch_externally(sample, "alpha = 1\nbeta = 99\ngamma = 3\nDELTA = 4\n")

    out = t_edit_file(
        {"path": "sample.py", "old_string": "alpha = 1", "new_string": "alpha = 0"}, ctx
    )
    assert out.startswith("ERROR:")
    assert "changed on disk" in out
    # And the other person's work is still there.
    assert "DELTA = 4" in sample.read_text()


def test_write_after_an_external_change_is_refused(ctx, sample):
    t_read_file({"path": "sample.py"}, ctx)
    touch_externally(sample, "totally different\n")

    out = t_write_file({"path": "sample.py", "content": "clobbered\n"}, ctx)
    assert out.startswith("ERROR:")
    assert sample.read_text() == "totally different\n"


def test_replace_lines_after_an_external_change_is_refused(ctx, sample):
    t_read_file({"path": "sample.py"}, ctx)
    touch_externally(sample, "one\ntwo\nthree\nfour\nfive\n")

    out = t_replace_lines({"path": "sample.py", "start": 1, "end": 1, "new_content": "X"}, ctx)
    assert out.startswith("ERROR:")


def test_multi_edit_after_an_external_change_is_refused(ctx, sample):
    t_read_file({"path": "sample.py"}, ctx)
    touch_externally(sample, "alpha = 1\nbeta = 2\ngamma = 3\nEXTRA = 4\n")

    out = t_multi_edit(
        {"path": "sample.py", "edits": [{"old_string": "alpha = 1", "new_string": "alpha = 0"}]},
        ctx,
    )
    assert out.startswith("ERROR:")


def test_an_unchanged_file_edits_normally(ctx, sample):
    t_read_file({"path": "sample.py"}, ctx)
    out = t_edit_file(
        {"path": "sample.py", "old_string": "beta = 2", "new_string": "beta = 20"}, ctx
    )
    assert out.startswith("OK:")
    assert "beta = 20" in sample.read_text()


def test_our_own_edits_do_not_trip_the_guard(ctx, sample):
    """The obvious false positive: an edit changes the file, so the second edit
    of a turn must not be refused because of the first."""
    t_read_file({"path": "sample.py"}, ctx)
    assert t_edit_file(
        {"path": "sample.py", "old_string": "alpha = 1", "new_string": "alpha = 0"}, ctx
    ).startswith("OK:")
    assert t_edit_file(
        {"path": "sample.py", "old_string": "beta = 2", "new_string": "beta = 20"}, ctx
    ).startswith("OK:")
    assert t_edit_file(
        {"path": "sample.py", "old_string": "gamma = 3", "new_string": "gamma = 30"}, ctx
    ).startswith("OK:")
    assert sample.read_text() == "alpha = 0\nbeta = 20\ngamma = 30\n"


def test_a_file_we_never_read_is_refused(ctx, sample):
    """`replace_lines` is the sharp version of this — it splices by line number
    and cannot notice the numbers refer to a file it never opened."""
    out = t_edit_file(
        {"path": "sample.py", "old_string": "alpha = 1", "new_string": "alpha = 0"}, ctx
    )
    assert out.startswith("ERROR:")
    assert "read sample.py before editing" in out
    assert sample.read_text() == "alpha = 1\nbeta = 2\ngamma = 3\n"


def test_reading_it_first_is_all_it_takes(ctx, sample):
    t_read_file({"path": "sample.py"}, ctx)
    out = t_edit_file(
        {"path": "sample.py", "old_string": "alpha = 1", "new_string": "alpha = 0"}, ctx
    )
    assert out.startswith("OK:")


def test_writing_a_file_counts_as_knowing_it(ctx, tmp_path):
    """A file the agent created this session needs no separate read — it has
    the contents by construction."""
    assert t_write_file({"path": "made.py", "content": "x = 1\n"}, ctx).startswith("OK:")
    out = t_edit_file({"path": "made.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    assert out.startswith("OK:")
    assert (tmp_path / "made.py").read_text() == "x = 2\n"


def test_the_requirement_survives_across_turns(ctx, sample):
    """file_versions is deliberately not reset per turn: having read a file in
    turn one, the agent should not be made to re-read it in turn two."""
    t_read_file({"path": "sample.py"}, ctx)
    ctx.state.update(files_read=set())  # what the engine clears between turns
    out = t_edit_file(
        {"path": "sample.py", "old_string": "beta = 2", "new_string": "beta = 20"}, ctx
    )
    assert out.startswith("OK:")


def test_every_edit_path_enforces_it(ctx, sample):
    from cagentic.coding_tools import t_multi_edit

    assert t_edit_file(
        {"path": "sample.py", "old_string": "alpha = 1", "new_string": "a"}, ctx
    ).startswith("ERROR:")
    assert t_replace_lines(
        {"path": "sample.py", "start_line": 1, "end_line": 1, "new_content": "X"}, ctx
    ).startswith("ERROR:")
    assert t_multi_edit(
        {"path": "sample.py", "edits": [{"old_string": "alpha = 1", "new_string": "a"}]}, ctx
    ).startswith("ERROR:")
    assert t_write_file({"path": "sample.py", "content": "clobber\n"}, ctx).startswith("ERROR:")


def test_creating_a_new_file_is_never_blocked(ctx, tmp_path):
    out = t_write_file({"path": "brand_new.txt", "content": "hello\n"}, ctx)
    assert out.startswith("OK:")
    assert (tmp_path / "brand_new.txt").read_text() == "hello\n"
