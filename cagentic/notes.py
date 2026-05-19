"""Persistent notes — a markdown knowledge base the assistant can read/write.

Notes live as plain .md files at ~/.config/cagentic/notes/<name>.md so they
work outside Cagentic too (open in any editor, sync via iCloud/Drive, etc.).
Names are slugified to keep them filesystem-safe. The assistant uses
note_write / note_get / note_list / note_search to remember things between
sessions without you having to repeat yourself.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import config_dir


def notes_dir() -> Path:
    d = config_dir() / "notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


_SLUG_RX = re.compile(r"[^a-z0-9._-]+")


def slugify(name: str) -> str:
    """Map a free-form note name to a safe filename stem."""
    s = name.strip().lower()
    s = _SLUG_RX.sub("-", s).strip("-")
    return s or "untitled"


def _path(name: str) -> Path:
    return notes_dir() / f"{slugify(name)}.md"


@dataclass
class Note:
    name: str
    path: Path
    body: str
    updated_at: float

    def short(self) -> str:
        first = ""
        for ln in self.body.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                first = ln
                break
        size = len(self.body)
        return f"{self.name:<24}  {size:>5}B  {first[:60]}"


def write(name: str, body: str, *, append: bool = False) -> Note:
    """Write or overwrite a note. If `append`, prepend a timestamped entry."""
    p = _path(name)
    existing = p.read_text(errors="replace") if p.exists() else ""
    if append and existing:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        new_body = f"## {stamp}\n{body.strip()}\n\n{existing}"
    elif append:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        new_body = f"# {name}\n\n## {stamp}\n{body.strip()}\n"
    else:
        new_body = body if body.endswith("\n") else body + "\n"
    p.write_text(new_body, encoding="utf-8")
    return Note(name=p.stem, path=p, body=new_body, updated_at=p.stat().st_mtime)


def get(name: str) -> Note | None:
    p = _path(name)
    if not p.exists():
        return None
    body = p.read_text(errors="replace")
    return Note(name=p.stem, path=p, body=body, updated_at=p.stat().st_mtime)


def delete(name: str) -> bool:
    p = _path(name)
    if not p.exists():
        return False
    p.unlink()
    return True


def list_all() -> list[Note]:
    out: list[Note] = []
    for p in notes_dir().glob("*.md"):
        try:
            body = p.read_text(errors="replace")
        except OSError:
            continue
        out.append(Note(name=p.stem, path=p, body=body, updated_at=p.stat().st_mtime))
    out.sort(key=lambda n: n.updated_at, reverse=True)
    return out


def search(query: str, *, limit: int = 50) -> list[tuple[Note, list[str]]]:
    """Substring search across note bodies. Returns (note, matching_lines)."""
    q = query.lower().strip()
    if not q:
        return []
    out: list[tuple[Note, list[str]]] = []
    for n in list_all():
        hits = [ln for ln in n.body.splitlines() if q in ln.lower()]
        if hits:
            out.append((n, hits[:5]))
        if len(out) >= limit:
            break
    return out
