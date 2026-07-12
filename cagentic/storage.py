"""Small transactional SQLite JSON store with non-destructive migration."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .config import config_dir

_LOCK = threading.RLock()
_MIGRATED: set[tuple[str, str]] = set()


def database_path() -> Path:
    root = config_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root / "state.sqlite3"


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(database_path(), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS objects (
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(namespace, key)
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS objects_by_updated ON objects(namespace, updated_at DESC)"
    )
    return db


def put(namespace: str, key: str, value: object, updated_at: float = 0) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    with _LOCK, _connect() as db:
        db.execute(
            """INSERT INTO objects(namespace,key,payload,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(namespace,key) DO UPDATE SET
                 payload=excluded.payload, updated_at=excluded.updated_at""",
            (namespace, key, payload, updated_at),
        )


def get(namespace: str, key: str):
    with _LOCK, _connect() as db:
        row = db.execute(
            "SELECT payload FROM objects WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


def delete(namespace: str, key: str) -> bool:
    with _LOCK, _connect() as db:
        cur = db.execute("DELETE FROM objects WHERE namespace=? AND key=?", (namespace, key))
        return cur.rowcount > 0


def list_values(namespace: str) -> list[object]:
    with _LOCK, _connect() as db:
        rows = db.execute(
            "SELECT payload FROM objects WHERE namespace=? ORDER BY updated_at DESC, key",
            (namespace,),
        ).fetchall()
    out = []
    for (payload,) in rows:
        try:
            out.append(json.loads(payload))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def search_values(namespace: str, query: str, limit: int = 50) -> list[object]:
    pattern = f"%{query.casefold()}%"
    with _LOCK, _connect() as db:
        rows = db.execute(
            """SELECT payload FROM objects
               WHERE namespace=? AND lower(payload) LIKE ?
               ORDER BY updated_at DESC LIMIT ?""",
            (namespace, pattern, max(1, min(limit, 500))),
        ).fetchall()
    out = []
    for (payload,) in rows:
        try:
            out.append(json.loads(payload))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def migrate_json_files(namespace: str, paths: Iterable[Path]) -> int:
    """Import legacy JSON only when its key is absent; never remove source files."""
    marker = (str(database_path()), namespace)
    with _LOCK:
        if marker in _MIGRATED:
            return 0
    imported = 0
    with _LOCK, _connect() as db:
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            key = str(value.get("id") or path.stem) if isinstance(value, dict) else path.stem
            exists = db.execute(
                "SELECT 1 FROM objects WHERE namespace=? AND key=?", (namespace, key)
            ).fetchone()
            if exists is not None:
                continue
            updated = float(value.get("updated_at") or 0) if isinstance(value, dict) else 0
            db.execute(
                "INSERT INTO objects(namespace,key,payload,updated_at) VALUES(?,?,?,?)",
                (namespace, key, json.dumps(value, ensure_ascii=False), updated),
            )
            imported += 1
        _MIGRATED.add(marker)
    return imported
