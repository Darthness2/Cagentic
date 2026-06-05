"""Project folders for grouping conversation sessions.

Each project is JSON at ~/.config/cagentic/projects/<id>.json with:
    {id, name, color, created_at, updated_at, chats: [session_id, ...]}
"""
from __future__ import annotations

import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from .config import config_dir


def projects_dir() -> Path:
    d = config_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(project_id: str) -> Path:
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
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(proj, indent=2))
    tmp.replace(p)
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return p


def load(project_id: str) -> dict | None:
    p = _path(project_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def delete(project_id: str) -> bool:
    p = _path(project_id)
    if p.exists():
        p.unlink()
        return True
    return False


def list_all() -> list[dict]:
    out = []
    for p in projects_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": data.get("id", p.stem),
            "name": data.get("name", "Untitled Project"),
            "color": data.get("color", "#f0a87a"),
            "system_prompt": data.get("system_prompt", ""),
            "context": data.get("context", ""),
            "updated_at": data.get("updated_at", 0),
            "chat_count": len(data.get("chats", [])),
            "chats": data.get("chats", []),
        })
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