"""Saved conversation sessions on disk.

Each session is JSON at ~/.config/cagentic/sessions/<id>.json with:
    {id, title, model, project, created_at, updated_at, messages: [...]}
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import config_dir

logger = logging.getLogger(__name__)

# Serializes concurrent saves (gateway thread + REPL autosave) so the
# write-temp-then-replace dance can't interleave and corrupt a session file.
_SAVE_LOCK = threading.Lock()


def sessions_dir() -> Path:
    d = config_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(session_id: str) -> Path:
    if not isinstance(session_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", session_id
    ):
        raise ValueError("invalid session id")
    return sessions_dir() / f"{session_id}.json"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def make(
    model: str,
    title: str | None = None,
    project_id: str | None = None,
    project: dict | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    return {
        "id": new_id(),
        "title": title or "untitled",
        "model": model,
        "project_id": project_id or "",
        "project": project,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def derive_title(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "user":
            # Content may be a list (tool/assistant payloads), not a str —
            # coerce so re.sub / slicing don't blow up.
            raw = m.get("content")
            text = ("" if raw is None else str(raw)).strip()
            text = re.sub(r"\s+", " ", text)
            return text[:60] if text else "untitled"
    return "untitled"


def save(session: dict) -> Path:
    session["updated_at"] = int(time.time())
    if session.get("title") in (None, "", "untitled"):
        session["title"] = derive_title(session.get("messages", []))
    p = _path(session["id"])
    d = p.parent
    data = json.dumps(session, indent=2)
    # Unique temp name + lock so concurrent savers can't clobber each other's
    # temp file or race the replace. Session files don't hold secrets, so the
    # default temp perms are fine; the atomic replace is what matters.
    with _SAVE_LOCK:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(d), prefix=f".{session['id']}.", suffix=".tmp"
        )
        try:
            # fchmod is POSIX-only; on Windows it raises AttributeError (not
            # OSError), which the handler below wouldn't catch — leaking the fd
            # and temp file. Session files hold no secrets anyway.
            if hasattr(os, "fchmod"):
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, p)
        except OSError:
            logger.warning("session save failed for %s", p, exc_info=True)
            try:
                os.unlink(tmp_name)
            except OSError:
                logger.warning(
                    "could not clean up temp file %s", tmp_name, exc_info=True
                )
            raise
    return p


def load(session_id: str) -> dict | None:
    try:
        p = _path(session_id)
    except ValueError:
        return None
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("project", None)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def delete(session_id: str) -> bool:
    try:
        p = _path(session_id)
    except ValueError:
        return False
    if p.exists():
        p.unlink()
        return True
    return False


def list_all() -> list[dict]:
    out = []
    for p in sessions_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append(
            {
                "id": data.get("id", p.stem),
                "title": data.get("title", "untitled"),
                "model": data.get("model", "?"),
                "project_id": data.get("project_id", ""),
                "project": data.get("project"),
                "updated_at": data.get("updated_at", 0),
                "turns": sum(
                    1 for m in data.get("messages", []) if m.get("role") == "user"
                ),
            }
        )
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


def fmt_time(ts: int) -> str:
    if not ts:
        return "?"
    delta = int(time.time()) - ts
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"
