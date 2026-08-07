"""Background proactive monitoring and a persistent notification inbox."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import inbox, integrations, personal_os, routines, storage

_log = logging.getLogger(__name__)
_NS = "os_notifications"


@dataclass
class Notification:
    id: str
    title: str
    body: str
    severity: str = "attention"
    action_prompt: str = ""
    fingerprint: str = ""
    read: bool = False
    dismissed: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _new_id() -> str:
    return "n" + secrets.token_hex(4)


def _match(notification_id: str) -> dict | None:
    if not notification_id:
        return None
    exact = storage.get(_NS, notification_id)
    if isinstance(exact, dict):
        return exact
    matches = [
        item
        for item in storage.list_values(_NS)
        if isinstance(item, dict) and str(item.get("id", "")).startswith(notification_id)
    ]
    return matches[0] if len(matches) == 1 else None


def create_notification(
    title: str,
    body: str,
    *,
    severity: str = "attention",
    action_prompt: str = "",
    fingerprint: str = "",
    expires_at: float | None = None,
    now: float | None = None,
) -> dict | None:
    """Persist one notification, returning None when its fingerprint exists."""
    now = float(now or time.time())
    if fingerprint:
        known = {
            str(item.get("fingerprint"))
            for item in storage.list_values(_NS)
            if isinstance(item, dict) and item.get("fingerprint")
        }
        if fingerprint in known:
            return None
    item = Notification(
        id=_new_id(),
        title=str(title or "Cagentic noticed something")[:160],
        body=str(body or "")[:12000],
        severity=str(severity or "attention"),
        action_prompt=str(action_prompt or "")[:2000],
        fingerprint=str(fingerprint or "")[:160],
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
    ).to_dict()
    storage.put(_NS, str(item["id"]), item, now)
    return item


def list_notifications(*, include_dismissed: bool = False, limit: int = 50) -> list[dict]:
    now = time.time()
    items = [item for item in storage.list_values(_NS) if isinstance(item, dict)]
    if not include_dismissed:
        items = [item for item in items if not item.get("dismissed")]
    items = [
        item
        for item in items
        if item.get("expires_at") is None or float(item.get("expires_at") or 0) >= now
    ]
    items.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    return items[: max(1, min(200, int(limit)))]


def unread_count() -> int:
    return len([item for item in list_notifications(limit=200) if not item.get("read")])


def mark_read(notification_id: str, *, read: bool = True) -> dict | None:
    item = _match(notification_id)
    if item is None:
        return None
    item["read"] = bool(read)
    item["updated_at"] = time.time()
    storage.put(_NS, str(item["id"]), item, float(item["updated_at"]))
    return item


def mark_all_read() -> int:
    changed = 0
    for item in list_notifications(limit=200):
        if not item.get("read") and mark_read(str(item["id"])):
            changed += 1
    return changed


def dismiss(notification_id: str) -> bool:
    item = _match(notification_id)
    if item is None:
        return False
    item["dismissed"] = True
    item["read"] = True
    item["updated_at"] = time.time()
    storage.put(_NS, str(item["id"]), item, float(item["updated_at"]))
    return True


def _fingerprint(insight: dict, now: float) -> str:
    # A daily bucket allows persistent conditions to resurface tomorrow while
    # preventing a one-minute monitor loop from spamming the same alert.
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    payload = "|".join(
        (
            day,
            str(insight.get("id") or ""),
            str(insight.get("title") or ""),
            str(insight.get("body") or ""),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def scan(
    config: dict | None = None,
    *,
    now: float | None = None,
    browser_connected: bool = False,
) -> list[dict]:
    """Turn actionable briefing insights into durable, deduplicated alerts."""
    now = float(now or time.time())
    briefing = personal_os.briefing(config or {}, now=now, browser_connected=browser_connected)
    known = {
        str(item.get("fingerprint"))
        for item in storage.list_values(_NS)
        if isinstance(item, dict) and item.get("fingerprint")
    }
    created: list[dict] = []
    for insight in briefing.get("insights") or []:
        if insight.get("severity") not in ("urgent", "attention"):
            continue
        fingerprint = _fingerprint(insight, now)
        if fingerprint in known:
            continue
        item = create_notification(
            str(insight.get("title") or "Cagentic noticed something"),
            str(insight.get("body") or ""),
            severity=str(insight.get("severity") or "attention"),
            action_prompt=str(insight.get("action_prompt") or ""),
            fingerprint=fingerprint,
            expires_at=now + 2 * 86400,
            now=now,
        )
        if item is None:
            continue
        created.append(item)
        known.add(fingerprint)
    return created


def _desktop_notify(item: dict) -> None:
    title = str(item.get("title") or "Cagentic")[:120]
    body = str(item.get("body") or "")[:500]
    try:
        if sys.platform == "darwin":
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["notify-send", title, body],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError):
        _log.debug("desktop notification delivery failed", exc_info=True)


class ProactiveMonitor:
    """Runs low-cost local checks and due calendar sync in one daemon thread."""

    def __init__(
        self,
        config: dict,
        *,
        browser_connected: Callable[[], bool] | None = None,
        on_notification: Callable[[dict], None] | None = None,
        on_routine: Callable[[dict], str] | None = None,
    ) -> None:
        raw_proactive_cfg = config.get("proactive")
        proactive_cfg = raw_proactive_cfg if isinstance(raw_proactive_cfg, dict) else {}
        self.config = config
        raw_enabled = proactive_cfg.get("enabled", True)
        self.enabled = raw_enabled if isinstance(raw_enabled, bool) else True
        try:
            raw_interval = proactive_cfg.get("interval", 60)
            interval = 0 if isinstance(raw_interval, bool) else int(raw_interval)
        except (TypeError, ValueError):
            interval = 0
        self.interval = max(30, min(3600, interval or 60))
        raw_notifications = proactive_cfg.get("desktop_notifications", True)
        self.desktop_notifications = (
            raw_notifications if isinstance(raw_notifications, bool) else True
        )
        self.browser_connected = browser_connected or (lambda: False)
        self.on_notification = on_notification
        self.on_routine = on_routine
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cagentic-proactive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None

    def tick(self) -> dict:
        created = scan(
            self.config,
            browser_connected=bool(self.browser_connected()),
        )
        for item in created:
            if self.desktop_notifications:
                _desktop_notify(item)
            if self.on_notification:
                try:
                    self.on_notification(item)
                except Exception:
                    _log.warning("proactive notification callback failed", exc_info=True)
        sync_results = integrations.sync_due()
        email_results = inbox.sync_due_email()
        routine_results: list[dict] = []
        for routine in routines.due_routines():
            output = ""
            error = ""
            try:
                output = (
                    self.on_routine(routine)
                    if self.on_routine
                    else routines.fallback_output(routine)
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                output = routines.fallback_output(routine)
                _log.warning("proactive routine failed", exc_info=True)
            completed = routines.mark_run(str(routine["id"]), output=output, error=error)
            notice = create_notification(
                str(routine.get("name") or "Proactive routine"),
                output,
                severity="attention" if error else "info",
                action_prompt="Help me act on this proactive briefing.",
                fingerprint=f"routine:{routine['id']}:{completed.get('last_run_key') if completed else ''}",
                expires_at=time.time() + 7 * 86400,
            )
            if notice:
                created.append(notice)
                if self.desktop_notifications:
                    _desktop_notify(notice)
                if self.on_notification:
                    try:
                        self.on_notification(notice)
                    except Exception:
                        _log.warning("proactive notification callback failed", exc_info=True)
            routine_results.append(
                {"id": routine["id"], "ok": not bool(error), "error": error or None}
            )
        return {
            "notifications": created,
            "sync_results": sync_results,
            "email_results": email_results,
            "routine_results": routine_results,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                _log.warning("proactive monitor tick failed", exc_info=True)
            if self._stop.wait(self.interval):
                break
