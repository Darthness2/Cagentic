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

from . import storage
from .config import config_dir
from .fmt import fmt_ago

logger = logging.getLogger(__name__)

# Serializes concurrent saves (gateway thread + REPL autosave) so the
# write-temp-then-replace dance can't interleave and corrupt a session file.
_SAVE_LOCK = threading.Lock()


def sessions_dir() -> Path:
    d = config_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(session_id: str) -> Path:
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
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
    # Sub-second precision: `list_all` orders by this, and whole seconds made
    # two saves in the same second tie — which left `--continue` picking an
    # arbitrary one of them rather than the newest. Older int values still
    # compare correctly against floats.
    session["updated_at"] = time.time()
    if session.get("title") in (None, "", "untitled"):
        session["title"] = derive_title(session.get("messages", []))
    p = _path(session["id"])
    d = p.parent
    data = json.dumps(session, indent=2)
    # Unique temp name + lock so concurrent savers can't clobber each other's
    # temp file or race the replace. Session files don't hold secrets, so the
    # default temp perms are fine; the atomic replace is what matters.
    with _SAVE_LOCK:
        fd, tmp_name = tempfile.mkstemp(dir=str(d), prefix=f".{session['id']}.", suffix=".tmp")
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
                logger.warning("could not clean up temp file %s", tmp_name, exc_info=True)
            raise
    storage.put("sessions", session["id"], session, session["updated_at"])
    return p


def load(session_id: str) -> dict | None:
    try:
        p = _path(session_id)
    except ValueError:
        return None
    storage.migrate_json_files("sessions", sessions_dir().glob("*.json"))
    stored = storage.get("sessions", session_id)
    if isinstance(stored, dict):
        stored.setdefault("project", None)
        return stored
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
    deleted = storage.delete("sessions", session_id)
    if p.exists():
        p.unlink()
        return True
    return deleted


def list_all() -> list[dict]:
    storage.migrate_json_files("sessions", sessions_dir().glob("*.json"))
    out = []
    for data in storage.list_values("sessions"):
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "id": data.get("id", ""),
                "title": data.get("title", "untitled"),
                "model": data.get("model", "?"),
                "project_id": data.get("project_id", ""),
                "project": data.get("project"),
                "updated_at": data.get("updated_at", 0),
                "turns": sum(1 for m in data.get("messages", []) if m.get("role") == "user"),
            }
        )
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


def search(query: str, limit: int = 50) -> list[dict]:
    storage.migrate_json_files("sessions", sessions_dir().glob("*.json"))
    found = []
    for data in storage.search_values("sessions", query, limit):
        if isinstance(data, dict):
            found.append(data)
    return found


def fmt_time(ts: int) -> str:
    """Relative time for the /sessions table. Kept as the module's own name
    since callers import it from here; the ladder itself lives in storage."""
    return fmt_ago(ts)
