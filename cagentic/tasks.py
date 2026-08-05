"""Persistent task graph + task IDs.

Tasks are JSON files at ~/.config/cagentic/tasks/<id>.json. The model calls
task_create / task_update / task_get / task_list / task_delete tools to
maintain a persistent record of what it's working on. Each task can carry
an optional `worktree` (directory) for sub-projects.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from . import storage
from .config import config_dir


class TaskKind(str, Enum):
    BASH = "b"
    TOOL = "t"
    AGENT = "a"  # local_agent / sub-agent
    REMOTE = "r"
    DREAM = "d"  # background reflection
    FLOW = "f"


VALID_STATUS = {"pending", "active", "done", "blocked", "failed", "cancelled"}


_ID_RX = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidTaskId(ValueError):
    """Raised for an id that isn't a plain token."""


def _safe_id(task_id: str) -> str:
    """Validate an id before it is used to build a filename.

    Task ids arrive from the model (task_get/task_output take one verbatim).
    Generated ids are hex tokens, so anything with a path separator, a "..", or
    a glob character is a probe rather than a real id — and left unchecked it
    would escape the tasks directory or make the prefix glob match at random.
    """
    if not task_id or not _ID_RX.match(task_id):
        raise InvalidTaskId(f"invalid task id: {task_id!r}")
    return task_id


def new_id(kind: TaskKind | str = TaskKind.TOOL) -> str:
    if isinstance(kind, TaskKind):
        kind = kind.value
    return f"{kind}{secrets.token_hex(4)}"


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    status: str = "pending"
    deps: list[str] = field(default_factory=list)
    parent_id: str | None = None
    kind: str = "t"
    result: str = ""
    worktree: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def short(self) -> str:
        title = self.title if len(self.title) < 60 else self.title[:57] + "…"
        wt = f"  [wt:{self.worktree}]" if self.worktree else ""
        return f"{self.id}  [{self.status:<8}]  {title}{wt}"


class TaskGraph:
    """File-backed CRUD for Task objects."""

    def __init__(self, root: Path | None = None) -> None:
        self._sqlite = root is None
        self.root = root or (config_dir() / "tasks")
        self.root.mkdir(parents=True, exist_ok=True)
        if self._sqlite:
            storage.migrate_json_files("tasks", self.root.glob("*.json"))

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
            raise ValueError("invalid task id")
        return self.root / f"{task_id}.json"

    def create(
        self,
        title: str,
        *,
        description: str = "",
        deps: list[str] | None = None,
        parent_id: str | None = None,
        kind: TaskKind | str = TaskKind.TOOL,
        worktree: str | None = None,
    ) -> Task:
        task = Task(
            id=new_id(kind),
            title=title,
            description=description,
            deps=list(deps or []),
            parent_id=parent_id,
            kind=kind.value if isinstance(kind, TaskKind) else str(kind),
            worktree=worktree,
        )
        self._write(task)
        return task

    def get(self, task_id: str) -> Task | None:
        try:
            p = self._path(task_id)
        except ValueError:
            return None
        if self._sqlite:
            exact = storage.get("tasks", task_id)
            if isinstance(exact, dict):
                try:
                    return Task(**exact)
                except TypeError:
                    return None
            matches = [
                value
                for value in storage.list_values("tasks")
                if isinstance(value, dict) and str(value.get("id", "")).startswith(task_id)
            ]
            if len(matches) == 1:
                try:
                    return Task(**matches[0])
                except TypeError:
                    return None
            return None
        if not p.exists():
            # Allow id prefixes ("t12ab..." → "t12ab*").
            for q in sorted(self.root.glob(f"{safe}*.json")):
                p = q
                break
            else:
                return None
        try:
            return Task(**json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, UnicodeDecodeError):
            return None

    def update(self, task_id: str, **changes) -> Task | None:
        task = self.get(task_id)
        if not task:
            return None
        if "status" in changes and changes["status"] not in VALID_STATUS:
            raise ValueError(f"invalid status: {changes['status']}")
        for k, v in changes.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.updated_at = time.time()
        self._write(task)
        return task

    def list(self, *, status: str | None = None, parent_id: str | None = None) -> list[Task]:
        out: list[Task] = []
        if self._sqlite:
            for value in storage.list_values("tasks"):
                if not isinstance(value, dict):
                    continue
                try:
                    task = Task(**value)
                except TypeError:
                    continue
                if status and task.status != status:
                    continue
                if parent_id and task.parent_id != parent_id:
                    continue
                out.append(task)
            return out
        for p in sorted(self.root.glob("*.json")):
            try:
                t = Task(**json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError, TypeError, UnicodeDecodeError):
                continue
            if status and t.status != status:
                continue
            if parent_id and t.parent_id != parent_id:
                continue
            out.append(t)
        out.sort(key=lambda t: t.updated_at, reverse=True)
        return out

    def delete(self, task_id: str) -> bool:
        task = self.get(task_id)
        if not task:
            return False
        deleted = storage.delete("tasks", task.id) if self._sqlite else False
        try:
            self._path(task.id).unlink()
            return True
        except OSError:
            return deleted

    def _write(self, task: Task) -> None:
        p = self._path(task.id)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(task.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(p)
        if self._sqlite:
            storage.put("tasks", task.id, task.to_dict(), task.updated_at)
