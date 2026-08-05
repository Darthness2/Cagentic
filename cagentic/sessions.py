"""Saved conversation sessions on disk.

Each session is JSON at ~/.config/cagentic/sessions/<id>.json with:
    {id, title, model, created_at, updated_at, messages: [...]}
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from .config import config_dir
from .storage import atomic_write_json, fmt_ago, read_json


def sessions_dir() -> Path:
    d = config_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(session_id: str) -> Path:
    return sessions_dir() / f"{session_id}.json"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def make(model: str, title: str | None = None, project_id: str | None = None) -> dict[str, Any]:
      now = int(time.time())
      return {
          "id": new_id(),
          "title": title or "untitled",
          "model": model,
          "project_id": project_id or "",
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
    return atomic_write_json(_path(session["id"]), session)


def load(session_id: str) -> dict | None:
    return read_json(_path(session_id), None)


def delete(session_id: str) -> bool:
    p = _path(session_id)
    if p.exists():
        p.unlink()
        return True
    return False


def list_all() -> list[dict]:
    out = []
    for p in sessions_dir().glob("*.json"):
        data = read_json(p, None)
        if not isinstance(data, dict):
            continue
        out.append({
            "id": data.get("id", p.stem),
            "title": data.get("title", "untitled"),
            "model": data.get("model", "?"),
            "project_id": data.get("project_id", ""),
            "updated_at": data.get("updated_at", 0),
            "turns": sum(1 for m in data.get("messages", []) if m.get("role") == "user"),
        })
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


def fmt_time(ts: int) -> str:
    """Relative time for the /sessions table. Kept as the module's own name
    since callers import it from here; the ladder itself lives in storage."""
    return fmt_ago(ts)
