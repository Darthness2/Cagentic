"""Project folders for grouping conversation sessions.

Each project is JSON at ~/.config/cagentic/projects/<id>.json with:
    {id, name, color, created_at, updated_at, chats: [session_id, ...]}
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
from .storage import atomic_write_json, read_json


def projects_dir() -> Path:
    d = config_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(project_id: str) -> Path:
    if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id):
        raise ValueError("invalid project id")
    return projects_dir() / f"{project_id}.json"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def create(name: str, color: str | None = None) -> dict[str, Any]:
    now = int(time.time())
    proj = {
        "id": new_id(),
        "name": name.strip() or "Untitled Project",
        "color": color or "#f0a87a",
        "system_prompt": "",
        "context": "",
        "created_at": now,
        "updated_at": now,
        "chats": [],
    }
    save(proj)
    return proj


def save(proj: dict) -> Path:
    proj["updated_at"] = int(time.time())
    p = _path(proj["id"])
    d = p.parent
    data = json.dumps(proj, indent=2)
    # Unique temp name + lock so concurrent savers can't clobber each other's
    # temp file or race the replace.
    with _SAVE_LOCK:
        fd, tmp_name = tempfile.mkstemp(dir=str(d), prefix=f".{proj['id']}.", suffix=".tmp")
        try:
            # fchmod is POSIX-only; on Windows it raises AttributeError (not
            # OSError), which the handler below wouldn't catch — leaking the fd
            # and temp file.
            if hasattr(os, "fchmod"):
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, p)
        except OSError:
            logger.warning("project save failed for %s", p, exc_info=True)
            try:
                os.unlink(tmp_name)
            except OSError:
                logger.warning("could not clean up temp file %s", tmp_name, exc_info=True)
            raise
    storage.put("projects", proj["id"], proj, proj["updated_at"])
    return p


def load(project_id: str) -> dict | None:
    try:
        p = _path(project_id)
    except ValueError:
        return None
    storage.migrate_json_files("projects", projects_dir().glob("*.json"))
    stored = storage.get("projects", project_id)
    if isinstance(stored, dict):
        return stored
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete(project_id: str) -> bool:
    try:
        p = _path(project_id)
    except ValueError:
        return False
    deleted = storage.delete("projects", project_id)
    if p.exists():
        p.unlink()
        return True
    return deleted


def list_all() -> list[dict]:
    storage.migrate_json_files("projects", projects_dir().glob("*.json"))
    out = []
    for data in storage.list_values("projects"):
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "id": data.get("id", ""),
                "name": data.get("name", "Untitled Project"),
                "color": data.get("color", "#f0a87a"),
                "system_prompt": data.get("system_prompt", ""),
                "context": data.get("context", ""),
                "updated_at": data.get("updated_at", 0),
                "chat_count": len(data.get("chats", [])),
                "chats": data.get("chats", []),
            }
        )
    out.sort(key=lambda p: p["updated_at"], reverse=True)
    return out


def add_chat(project_id: str, chat_id: str) -> dict | None:
    proj = load(project_id)
    if proj is None:
        return None
    if chat_id not in proj.get("chats", []):
        proj.setdefault("chats", []).append(chat_id)
        save(proj)
    return proj


def remove_chat(project_id: str, chat_id: str) -> dict | None:
    proj = load(project_id)
    if proj is None:
        return None
    if chat_id in proj.get("chats", []):
        proj["chats"].remove(chat_id)
        save(proj)
    return proj


def rename(project_id: str, name: str) -> dict | None:
    proj = load(project_id)
    if proj is None:
        return None
    proj["name"] = name.strip() or "Untitled Project"
    save(proj)
    return proj


def update_config(project_id: str, system_prompt: str = "", context: str = "") -> dict | None:
    proj = load(project_id)
    if proj is None:
        return None
    proj["system_prompt"] = system_prompt
    proj["context"] = context
    save(proj)
    return proj
