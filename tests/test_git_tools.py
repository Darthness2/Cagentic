"""Local git tools against real repositories.

These shell out to git rather than mocking it, because the thing worth testing
is the parsing of git's actual output — porcelain status codes, an empty diff,
a repo with no commits yet — and a mock would only assert that the author
remembered what git prints.

The scope boundary is itself a tested property: nothing here can reach a remote
or rewrite history, so a mistaken call cannot lose committed work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cagentic.git_tools import t_git_commit, t_git_diff, t_git_log, t_git_status
from cagentic.state import AppState
from cagentic.tools import ToolContext

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not installed",
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("original\n")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-qm", "initial commit")
    return tmp_path


@pytest.fixture()
def ctx(repo: Path) -> ToolContext:
    return ToolContext(root=repo, state=AppState(workspace=repo, home=repo))


def test_status_on_a_clean_tree(ctx):
    out = t_git_status({}, ctx)
    assert "working tree clean" in out
    assert "branch: main" in out


def test_status_separates_staged_unstaged_and_untracked(ctx, repo):
    (repo / "tracked.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    (repo / "staged.txt").write_text("staged\n")
    git(repo, "add", "staged.txt")

    out = t_git_status({}, ctx)
    assert "staged.txt" in out.split("not staged")[0]
    assert "tracked.txt" in out
    assert "new.txt" in out
    assert "untracked" in out


def test_diff_shows_unstaged_changes(ctx, repo):
    (repo / "tracked.txt").write_text("rewritten\n")
    out = t_git_diff({}, ctx)
    assert "-original" in out
    assert "+rewritten" in out


def test_diff_staged_is_a_separate_view(ctx, repo):
    (repo / "tracked.txt").write_text("rewritten\n")
    git(repo, "add", "tracked.txt")

    assert t_git_diff({}, ctx) == "(no unstaged changes)"
    assert "+rewritten" in t_git_diff({"staged": True}, ctx)


def test_diff_reports_no_changes_rather_than_empty_output(ctx):
    assert t_git_diff({}, ctx) == "(no unstaged changes)"


def test_log_lists_commits(ctx):
    assert "initial commit" in t_git_log({}, ctx)


def test_commit_records_staged_work(ctx, repo):
    (repo / "tracked.txt").write_text("v2\n")
    git(repo, "add", "tracked.txt")

    out = t_git_commit({"message": "Update tracked file"}, ctx)
    assert out.startswith("OK: committed")
    assert "Update tracked file" in t_git_log({}, ctx)
    assert "working tree clean" in t_git_status({}, ctx)


def test_commit_with_all_stages_tracked_changes(ctx, repo):
    (repo / "tracked.txt").write_text("v3\n")
    out = t_git_commit({"message": "Sweep tracked changes", "all": True}, ctx)
    assert out.startswith("OK: committed")


def test_commit_with_all_does_not_sweep_up_untracked_files(ctx, repo):
    """`git add -u`, not `-A` — this is how build output gets committed."""
    (repo / "tracked.txt").write_text("v4\n")
    (repo / "junk.log").write_text("noise\n")

    t_git_commit({"message": "Only the tracked one", "all": True}, ctx)

    assert "junk.log" in t_git_status({}, ctx)  # still untracked
    listing = subprocess.run(
        ["git", "show", "--name-only", "--pretty=", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    assert "junk.log" not in listing


def test_commit_can_stage_named_paths(ctx, repo):
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    t_git_commit({"message": "Just a", "paths": ["a.txt"]}, ctx)

    listing = subprocess.run(
        ["git", "show", "--name-only", "--pretty=", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    assert "a.txt" in listing
    assert "b.txt" not in listing


def test_commit_with_nothing_staged_says_so(ctx):
    out = t_git_commit({"message": "empty"}, ctx)
    assert out.startswith("ERROR:")
    assert "nothing staged" in out


def test_commit_requires_a_message(ctx, repo):
    (repo / "tracked.txt").write_text("v5\n")
    git(repo, "add", "tracked.txt")
    assert t_git_commit({"message": "   "}, ctx).startswith("ERROR: missing argument")


def test_outside_a_repository_every_tool_explains_itself(tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    ctx = ToolContext(root=plain, state=AppState(workspace=plain, home=plain))
    for tool in (t_git_status, t_git_diff, t_git_log):
        out = tool({}, ctx)
        assert out.startswith("ERROR:")
        assert "not inside a git repository" in out


def test_a_repo_with_no_commits_is_not_an_error(tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", "-b", "main")
    ctx = ToolContext(root=fresh, state=AppState(workspace=fresh, home=fresh))
    assert t_git_log({}, ctx) == "(no commits yet)"


def test_the_read_tools_never_prompt(repo):
    from cagentic.permissions import READ_ONLY, can_use_tool

    state = AppState(workspace=repo, home=repo)
    for name in ("git_status", "git_diff", "git_log"):
        assert name in READ_ONLY
        ok, _ = can_use_tool(name, {}, state, None)
        assert ok is True, f"{name} should not need approval"


def test_committing_is_gated(repo):
    from cagentic.permissions import READ_ONLY, can_use_tool

    assert "git_commit" not in READ_ONLY
    state = AppState(workspace=repo, home=repo)
    ok, _ = can_use_tool("git_commit", {"message": "x"}, state, lambda *a: "no")
    assert ok is False


def test_no_tool_here_can_reach_a_remote_or_rewrite_history():
    """The scope boundary is the safety property, so assert it directly."""
    import inspect

    from cagentic import git_tools

    source = inspect.getsource(git_tools)
    for forbidden in ("push", "fetch", "rebase", "reset", "--force", "checkout"):
        assert f'"{forbidden}"' not in source, f"git {forbidden} must stay in run_bash"
