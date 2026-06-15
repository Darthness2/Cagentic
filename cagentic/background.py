"""Background tasks.

A small daemon-thread executor that runs slow ops (shell commands, sub-agent
queries) without blocking the agent's main loop. Each submission returns a
task_id immediately; on completion, a notification is enqueued. The engine
drains pending notifications before each Ollama call and injects them into
the conversation as user messages so the model can react.

Two kinds of background work:
  - bash_async(cmd)   — run a shell command in a thread.
  - dream(prompt)     — fork a sub-agent in a thread; result comes back later.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .tasks import TaskGraph, TaskKind, new_id

logger = logging.getLogger(__name__)

# Cap how many background jobs may run at once so a runaway caller can't spawn
# unbounded threads/subprocesses.
MAX_INFLIGHT = 8


@dataclass
class BackgroundJob:
    id: str
    kind: str          # "bash" | "dream"
    label: str         # the cmd / prompt
    status: str = "running"  # running | done | failed
    result: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    # The concrete TaskGraph task id this job maps to (if any). Matching the
    # task to update by id avoids the bug where two jobs sharing a command
    # collided on `description == label`.
    task_id: str | None = None


class BackgroundExecutor:
    def __init__(self, tasks: TaskGraph | None = None) -> None:
        self.tasks = tasks
        self._jobs: dict[str, BackgroundJob] = {}
        self._notifications: list[dict] = []
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        # Bound concurrency; acquired before starting a worker, released in
        # the runners' finally block.
        self._slots = threading.BoundedSemaphore(MAX_INFLIGHT)

    # -- internal helpers -----------------------------------------------

    def _reap_threads(self) -> None:
        """Drop references to threads that have finished. Call under no lock;
        it takes the lock itself."""
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]

    # -- public ----------------------------------------------------------

    def submit_bash(self, command: str, cwd: Path, *, timeout: int = 600) -> str:
        if not self._slots.acquire(blocking=False):
            raise RuntimeError(
                f"too many background jobs in flight (max {MAX_INFLIGHT}); "
                f"wait for some to finish"
            )
        self._reap_threads()
        job_id = new_id(TaskKind.BASH)
        job = BackgroundJob(id=job_id, kind="bash", label=command)
        try:
            if self.tasks is not None:
                task = self.tasks.create(
                    title=f"bash: {command[:60]}",
                    kind=TaskKind.BASH, status="active",
                    description=command, worktree=str(cwd),
                )
                job.task_id = task.id
            with self._lock:
                self._jobs[job_id] = job
            t = threading.Thread(
                target=self._run_bash, args=(job_id, command, cwd, timeout), daemon=True,
            )
            t.start()
        except BaseException:
            # Never leak a semaphore slot if we failed before the worker took
            # ownership of releasing it.
            self._slots.release()
            raise
        with self._lock:
            self._threads.append(t)
        return job_id

    def submit_dream(self, prompt: str, run: Callable[[str], str]) -> str:
        """Run an arbitrary callable in the background. `run(prompt)` should
        return the final text. Used by the agent_call tool's async variant."""
        if not self._slots.acquire(blocking=False):
            raise RuntimeError(
                f"too many background jobs in flight (max {MAX_INFLIGHT}); "
                f"wait for some to finish"
            )
        self._reap_threads()
        job_id = new_id(TaskKind.DREAM)
        job = BackgroundJob(id=job_id, kind="dream", label=prompt[:120])
        try:
            with self._lock:
                self._jobs[job_id] = job
            t = threading.Thread(
                target=self._run_dream, args=(job_id, prompt, run), daemon=True,
            )
            t.start()
        except BaseException:
            self._slots.release()
            raise
        with self._lock:
            self._threads.append(t)
        return job_id

    def status(self, job_id: str) -> Optional[BackgroundJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[BackgroundJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at)

    def wait(self, job_id: str, timeout: float = 60.0) -> Optional[BackgroundJob]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            j = self.status(job_id)
            if j and j.status != "running":
                return j
            time.sleep(0.1)
        return self.status(job_id)

    def drain_notifications(self) -> list[dict]:
        with self._lock:
            out = list(self._notifications)
            self._notifications.clear()
        return out

    # -- runners ---------------------------------------------------------

    def _finish(self, job_id: str, status: str, result: str) -> None:
        task_id = None
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = status
            job.result = result
            job.finished_at = time.time()
            task_id = job.task_id
            self._notifications.append({
                "id": job_id, "kind": job.kind, "label": job.label,
                "status": status, "result": result[:2000],
            })
        # Update the concrete task by id — not by matching description, which
        # collided when two jobs shared the same command.
        if self.tasks is not None and task_id is not None:
            try:
                self.tasks.update(
                    task_id,
                    status="done" if status == "done" else "failed",
                    result=result[:2000],
                )
            except Exception:
                logger.warning("background: failed updating task %s", task_id, exc_info=True)

    def _run_bash(self, job_id: str, command: str, cwd: Path, timeout: int) -> None:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(cwd),
                capture_output=True, text=True, timeout=timeout,
            )
            ok = proc.returncode == 0
            parts = [f"exit code: {proc.returncode}"]
            if proc.stdout:
                parts.append(f"--- stdout ---\n{proc.stdout}")
            if proc.stderr:
                parts.append(f"--- stderr ---\n{proc.stderr}")
            self._finish(job_id, "done" if ok else "failed", "\n".join(parts))
        except subprocess.TimeoutExpired:
            self._finish(job_id, "failed", f"timed out after {timeout}s")
        except Exception as e:
            logger.warning("background bash job %s failed", job_id, exc_info=True)
            self._finish(job_id, "failed", f"{type(e).__name__}: {e}")
        finally:
            self._slots.release()

    def _run_dream(self, job_id: str, prompt: str, run: Callable[[str], str]) -> None:
        try:
            text = run(prompt) or ""
            self._finish(job_id, "done", text)
        except Exception as e:
            logger.warning("background dream job %s failed", job_id, exc_info=True)
            self._finish(job_id, "failed", f"{type(e).__name__}: {e}")
        finally:
            self._slots.release()
