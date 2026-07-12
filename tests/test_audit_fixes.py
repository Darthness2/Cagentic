from __future__ import annotations

from types import SimpleNamespace

from cagentic import projects, reminders, sessions
from cagentic.background import BackgroundExecutor
from cagentic.browser import BrowserBridge
from cagentic.emailer import send_verification
from cagentic.coding_tools import t_enter_worktree
from cagentic.coordinator import tick
from cagentic.tasks import TaskGraph
from cagentic.teams import TeamRegistry
from cagentic.tools import ToolContext, t_set_workspace


def test_background_bash_with_task_graph(tmp_path):
    tasks = TaskGraph(root=tmp_path / "tasks")
    executor = BackgroundExecutor(tasks)
    job_id = executor.submit_bash("printf audit-ok", tmp_path)
    job = executor.wait(job_id, timeout=5)
    assert job is not None
    assert job.status == "done"
    assert "audit-ok" in job.result
    task = tasks.get(job.task_id or "")
    assert task is not None
    assert task.status == "done"


def test_session_ids_cannot_escape_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    outside = tmp_path / "outside.json"
    outside.write_text('{"id": "outside"}')
    assert sessions.load("../../outside") is None
    assert sessions.delete("../../outside") is False
    assert outside.exists()


def test_other_persistent_ids_cannot_escape_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    outside = tmp_path / "outside.json"
    outside.write_text('{"id": "outside"}')
    assert projects.load("../../outside") is None
    assert projects.delete("../../outside") is False
    tasks = TaskGraph(root=tmp_path / "tasks")
    assert tasks.get("../../outside") is None
    registry = TeamRegistry(root=tmp_path / "teams")
    try:
        registry.create_team("../../outside")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe team name was accepted")
    registry.add_teammate("safe", "réviseur")
    assert registry.get_teammate("safe", "réviseur") is not None


def test_reminder_prefix_must_be_unique(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    first = reminders.add("first")
    second = reminders.add("second")
    first.id = "rabc1111"
    second.id = "rabc2222"
    reminders._save_all([first, second])
    assert reminders.update("rabc", status="done") is None
    assert reminders.delete("rabc") is False
    assert len(reminders.list_all(include_done=True)) == 2


def test_pinned_boundary_blocks_workspace_and_worktree_escape(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = ToolContext(root=project, workspace_boundary=project, yolo=True)
    assert t_set_workspace({"path": str(outside)}, ctx).startswith("ERROR: pinned")
    assert t_enter_worktree({"path": str(outside)}, ctx).startswith("ERROR: pinned")
    child = project / "child"
    assert t_set_workspace({"path": str(child), "create": True}, ctx).startswith("OK")
    assert ctx.root == child.resolve()


def test_browser_timeout_does_not_deadlock_or_keep_late_results():
    bridge = BrowserBridge()
    bridge._server = object()
    result = bridge.send("read", timeout=0.01)
    assert result["ok"] is False
    bridge._deliver_result(1, True, {"late": True})
    assert bridge._results == {}


def test_browser_result_delivery_round_trip():
    bridge = BrowserBridge()
    bridge._pending.add(7)
    bridge._deliver_result(7, True, {"title": "ok"})
    assert bridge._results[7] == {"ok": True, "result": {"title": "ok"}}


def test_invalid_smtp_port_returns_error():
    cfg = {"email": {"smtp_host": "example.test", "smtp_port": "invalid"}}
    assert send_verification(cfg, "user@example.test", "123456") == (
        "email.smtp_port must be an integer"
    )


def test_coordinator_marks_subagent_errors_failed(tmp_path, monkeypatch):
    import cagentic.coordinator as coordinator

    tasks = TaskGraph(root=tmp_path / "tasks")
    task = tasks.create("audit task")
    teams = TeamRegistry(root=tmp_path / "teams")
    teams.add_teammate("audit", "worker")
    engine = SimpleNamespace(teams=teams, task_graph=tasks)
    monkeypatch.setattr(
        coordinator, "fork_subagent", lambda *_args, **_kwargs: "sub-agent error: boom"
    )
    results = tick(engine, team="audit", auto_claim=True, max_per_teammate=0)
    assert len(results) == 1
    assert tasks.get(task.id).status == "failed"
