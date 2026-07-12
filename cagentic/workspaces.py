"""Canonical, allow-listed workspace browsing and selection."""

from __future__ import annotations

import os
from pathlib import Path


class WorkspaceError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class WorkspacePolicy:
    def __init__(self, roots: list[str | Path]):
        unique: dict[str, Path] = {}
        for raw in roots:
            try:
                root = Path(raw).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if root.is_dir() and os.access(root, os.R_OK | os.X_OK):
                unique[str(root)] = root
        self.roots = list(unique.values())

    @classmethod
    def from_config(cls, config: dict, initial: Path) -> "WorkspacePolicy":
        env = os.environ.get("CAGENTIC_WORKSPACE_ROOTS")
        configured = (config.get("gateway") or {}).get("workspace_roots")
        if env is not None:
            roots: list[str | Path] = [p for p in env.split(os.pathsep) if p]
        elif configured:
            roots = configured if isinstance(configured, list) else [configured]
        else:
            roots = [initial, Path.home() / "Projects", Path.home() / "Developer"]
        return cls(roots)

    def validate(self, raw: object) -> Path:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise WorkspaceError("path must be a non-empty absolute path")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceError("path must be absolute")
        if ".." in candidate.parts:
            raise WorkspaceError("path traversal is not allowed")
        try:
            path = candidate.resolve(strict=True)
        except FileNotFoundError:
            raise WorkspaceError("path does not exist", 404)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(f"invalid or inaccessible path: {exc}")
        if not any(_within(path, root) for root in self.roots):
            raise WorkspaceError("path is outside the allowed workspace roots", 403)
        if not path.is_dir():
            raise WorkspaceError("path is not a directory")
        if not os.access(path, os.R_OK | os.X_OK):
            raise WorkspaceError("path is not readable", 403)
        return path

    def browse(self, raw: str) -> dict:
        if raw == "":
            return {
                "path": "",
                "folders": [{"name": p.name or str(p), "path": str(p)} for p in self.roots],
            }
        path = self.validate(raw)
        folders: dict[str, dict] = {}
        try:
            children = list(path.iterdir())
        except PermissionError:
            raise WorkspaceError("path is not readable", 403)
        except OSError as exc:
            raise WorkspaceError(f"could not browse path: {exc}")
        for child in children:
            try:
                canonical = child.resolve(strict=True)
                if not child.is_dir() or not any(_within(canonical, r) for r in self.roots):
                    continue
                if not os.access(canonical, os.R_OK | os.X_OK):
                    continue
            except (OSError, RuntimeError):
                continue
            folders[str(canonical)] = {"name": child.name, "path": str(canonical)}
        result = {
            "path": str(path),
            "parent": str(path.parent),
            "folders": sorted(
                folders.values(),
                key=lambda item: (item["name"].casefold(), item["name"]),
            ),
        }
        return result
