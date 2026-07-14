"""Unified personal inbox with local capture and standards-based email ingestion."""

from __future__ import annotations

import imaplib
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any

from . import storage


ITEM_NS = "os_inbox_items"
CONNECTION_NS = "os_inbox_connections"
ITEM_STATUSES = {"new", "read", "done", "archived"}
ITEM_KINDS = {"capture", "email", "task", "message", "document"}
_SYNC_LOCK = threading.RLock()


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def _match(namespace: str, item_id: str) -> dict | None:
    if not item_id:
        return None
    exact = storage.get(namespace, item_id)
    if isinstance(exact, dict):
        return exact
    matches = [
        item
        for item in storage.list_values(namespace)
        if isinstance(item, dict) and str(item.get("id", "")).startswith(item_id)
    ]
    return matches[0] if len(matches) == 1 else None


@dataclass
class InboxItem:
    id: str
    title: str
    summary: str = ""
    kind: str = "capture"
    source: str = "cagentic"
    external_id: str = ""
    sender: str = ""
    url: str = ""
    status: str = "new"
    priority: int = 0
    tags: list[str] = field(default_factory=list)
    received_at: float = field(default_factory=time.time)
    snoozed_until: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


def create_item(
    title: str,
    *,
    summary: str = "",
    kind: str = "capture",
    source: str = "cagentic",
    external_id: str = "",
    sender: str = "",
    url: str = "",
    priority: Any = 0,
    tags: list[str] | None = None,
    received_at: Any = None,
) -> dict:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("inbox item title is required")
    clean_kind = str(kind or "capture").lower()
    if clean_kind not in ITEM_KINDS:
        raise ValueError(f"invalid inbox item kind: {clean_kind}")
    clean_source = str(source or "cagentic")[:120]
    clean_external = str(external_id or "")[:500]
    if clean_external:
        existing = next(
            (
                item
                for item in storage.list_values(ITEM_NS)
                if isinstance(item, dict)
                and item.get("source") == clean_source
                and item.get("external_id") == clean_external
            ),
            None,
        )
        if existing:
            # Preserve local read/done state while refreshing provider metadata.
            return update_item(
                str(existing["id"]),
                title=clean_title,
                summary=summary,
                sender=sender,
                url=url,
                priority=priority,
                tags=tags or [],
                received_at=received_at,
            ) or existing
    now = time.time()
    try:
        received = float(received_at) if received_at is not None else now
    except (TypeError, ValueError):
        received = now
    item = InboxItem(
        id=_new_id("x"),
        title=clean_title[:300],
        summary=str(summary or "").strip()[:5000],
        kind=clean_kind,
        source=clean_source,
        external_id=clean_external,
        sender=str(sender or "").strip()[:500],
        url=str(url or "").strip()[:2000],
        priority=max(0, min(3, int(priority or 0))),
        tags=[str(tag)[:80] for tag in (tags or [])][:20],
        received_at=received,
        created_at=now,
        updated_at=now,
    )
    storage.put(ITEM_NS, item.id, item.to_dict(), item.updated_at)
    return item.to_dict()


def list_items(
    *,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    now: float | None = None,
) -> list[dict]:
    now = float(now or time.time())
    items = [item for item in storage.list_values(ITEM_NS) if isinstance(item, dict)]
    if status:
        items = [item for item in items if item.get("status") == status]
    elif not include_archived:
        items = [item for item in items if item.get("status") != "archived"]
    items = [
        item
        for item in items
        if item.get("snoozed_until") is None or float(item.get("snoozed_until") or 0) <= now
    ]
    status_order = {"new": 0, "read": 1, "done": 2, "archived": 3}
    items.sort(
        key=lambda item: (
            status_order.get(str(item.get("status")), 4),
            -int(item.get("priority") or 0),
            -float(item.get("received_at") or item.get("created_at") or 0),
        )
    )
    return items[: max(1, min(500, int(limit)))]


def update_item(item_id: str, **changes: Any) -> dict | None:
    item = _match(ITEM_NS, item_id)
    if item is None:
        return None
    for key, limit in (
        ("title", 300),
        ("summary", 5000),
        ("sender", 500),
        ("url", 2000),
    ):
        if key in changes:
            value = str(changes[key] or "").strip()[:limit]
            if key == "title" and not value:
                raise ValueError("inbox item title is required")
            item[key] = value
    if "status" in changes:
        status = str(changes["status"] or "read").lower()
        if status not in ITEM_STATUSES:
            raise ValueError(f"invalid inbox status: {status}")
        item["status"] = status
    if "priority" in changes:
        item["priority"] = max(0, min(3, int(changes["priority"] or 0)))
    if "tags" in changes:
        item["tags"] = [str(tag)[:80] for tag in (changes["tags"] or [])][:20]
    if "snoozed_until" in changes:
        value = changes["snoozed_until"]
        item["snoozed_until"] = float(value) if value not in (None, "") else None
    if "received_at" in changes and changes["received_at"] is not None:
        item["received_at"] = float(changes["received_at"])
    item["updated_at"] = time.time()
    storage.put(ITEM_NS, str(item["id"]), item, float(item["updated_at"]))
    return item


def delete_item(item_id: str) -> bool:
    item = _match(ITEM_NS, item_id)
    return bool(item and storage.delete(ITEM_NS, str(item["id"])))


def unread_count() -> int:
    return len([item for item in list_items(limit=500) if item.get("status") == "new"])


def create_email_connection(
    name: str,
    host: str,
    username: str,
    password: str,
    *,
    port: Any = 993,
    use_ssl: bool = True,
    folder: str = "INBOX",
    auto_sync: bool = True,
    sync_interval: Any = 900,
    max_messages: Any = 50,
) -> dict:
    clean_name = str(name or "").strip()
    clean_host = str(host or "").strip()
    if not clean_name or not clean_host or not username:
        raise ValueError("connection name, IMAP host, and username are required")
    interval = max(300, min(86400, int(sync_interval or 900)))
    now = time.time()
    connection = {
        "id": _new_id("m"),
        "name": clean_name[:120],
        "kind": "imap",
        "host": clean_host[:500],
        "port": max(1, min(65535, int(port or (993 if use_ssl else 143)))),
        "use_ssl": bool(use_ssl),
        "username": str(username)[:500],
        "password": str(password or "")[:2000],
        "folder": str(folder or "INBOX")[:300],
        "auto_sync": bool(auto_sync),
        "sync_interval": interval,
        "max_messages": max(1, min(200, int(max_messages or 50))),
        "status": "not_synced",
        "last_synced_at": None,
        "last_attempt_at": None,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }
    storage.put(CONNECTION_NS, str(connection["id"]), connection, now)
    return _public_connection(connection)


def _public_connection(connection: dict) -> dict:
    last_error = str(connection.get("last_error") or "")
    if last_error:
        detail = last_error[:160]
    elif connection.get("last_synced_at"):
        detail = "Last synced " + time.strftime(
            "%b %d at %I:%M %p", time.localtime(float(connection["last_synced_at"]))
        ).replace(" 0", " ")
    else:
        detail = "Ready to sync"
    return {
        "id": connection.get("id"),
        "name": connection.get("name"),
        "kind": "Email",
        "adapter": "imap",
        "managed": True,
        "connected": connection.get("status") == "synced",
        "status": connection.get("status", "not_synced"),
        "detail": detail,
        "endpoint": f"{connection.get('host')}:{connection.get('port')}",
        "auto_sync": bool(connection.get("auto_sync", True)),
        "sync_interval": int(connection.get("sync_interval") or 900),
        "last_synced_at": connection.get("last_synced_at"),
        "last_error": last_error[:300],
        "created_at": connection.get("created_at"),
        "updated_at": connection.get("updated_at"),
    }


def list_email_connections() -> list[dict]:
    items = [item for item in storage.list_values(CONNECTION_NS) if isinstance(item, dict)]
    items.sort(key=lambda item: str(item.get("name") or "").casefold())
    return [_public_connection(item) for item in items]


def delete_email_connection(connection_id: str, *, remove_items: bool = False) -> bool:
    connection = _match(CONNECTION_NS, connection_id)
    if connection is None:
        return False
    if remove_items:
        source = f"email:{connection['id']}"
        for item in list_items(include_archived=True, limit=500):
            if item.get("source") == source:
                delete_item(str(item["id"]))
    return storage.delete(CONNECTION_NS, str(connection["id"]))


def _header_received_at(message, fallback: float) -> float:
    raw = message.get("Date")
    if not raw:
        return fallback
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return fallback


def sync_email_connection(connection_id: str, *, imap_factory=None) -> dict:
    with _SYNC_LOCK:
        return _sync_email_connection(connection_id, imap_factory=imap_factory)


def _sync_email_connection(connection_id: str, *, imap_factory=None) -> dict:
    connection = _match(CONNECTION_NS, connection_id)
    if connection is None:
        raise ValueError("email connection not found")
    connection["last_attempt_at"] = time.time()
    connection["status"] = "syncing"
    connection["last_error"] = ""
    storage.put(CONNECTION_NS, str(connection["id"]), connection, time.time())
    client = None
    imported = updated = 0
    try:
        factory = imap_factory or (imaplib.IMAP4_SSL if connection.get("use_ssl", True) else imaplib.IMAP4)
        if imap_factory is None:
            client = factory(
                str(connection["host"]), int(connection["port"]), timeout=15
            )
        else:
            client = factory(str(connection["host"]), int(connection["port"]))
        if not connection.get("use_ssl", True) and hasattr(client, "starttls"):
            client.starttls()
        client.login(str(connection["username"]), str(connection.get("password") or ""))
        status, _ = client.select(str(connection.get("folder") or "INBOX"), readonly=True)
        if status != "OK":
            raise RuntimeError("could not open the configured mailbox")
        status, data = client.uid("search", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("mailbox search failed")
        uids = (data[0] if data else b"").split()
        uids = uids[-int(connection.get("max_messages") or 50) :]
        source = f"email:{connection['id']}"
        for uid in reversed(uids):
            status, fetched = client.uid(
                "fetch",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)] RFC822.SIZE)",
            )
            if status != "OK":
                continue
            raw_header = next(
                (part[1] for part in fetched or [] if isinstance(part, tuple) and isinstance(part[1], bytes)),
                None,
            )
            if not raw_header:
                continue
            message = BytesParser(policy=policy.default).parsebytes(raw_header)
            external_id = str(message.get("Message-ID") or uid.decode(errors="replace"))
            existed = any(
                item.get("source") == source and item.get("external_id") == external_id
                for item in storage.list_values(ITEM_NS)
                if isinstance(item, dict)
            )
            sender = str(message.get("From") or "Unknown sender")
            subject = str(message.get("Subject") or "(no subject)")
            create_item(
                subject,
                summary=f"Email from {sender}",
                kind="email",
                source=source,
                external_id=external_id,
                sender=sender,
                tags=["email"],
                received_at=_header_received_at(message, time.time()),
            )
            if existed:
                updated += 1
            else:
                imported += 1
        connection["status"] = "synced"
        connection["last_synced_at"] = time.time()
        connection["last_error"] = ""
    except Exception as exc:
        connection["status"] = "error"
        connection["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
    connection["updated_at"] = time.time()
    storage.put(
        CONNECTION_NS,
        str(connection["id"]),
        connection,
        float(connection["updated_at"]),
    )
    return {
        "ok": connection["status"] == "synced",
        "error": connection["last_error"] or None,
        "connection": _public_connection(connection),
        "imported": imported,
        "updated": updated,
    }


def due_email_connections(now: float | None = None) -> list[dict]:
    now = float(now or time.time())
    due = []
    for item in storage.list_values(CONNECTION_NS):
        if not isinstance(item, dict) or not item.get("auto_sync", True):
            continue
        if now - float(item.get("last_attempt_at") or 0) >= int(item.get("sync_interval") or 900):
            due.append(item)
    return due


def sync_due_email() -> list[dict]:
    return [sync_email_connection(str(item["id"])) for item in due_email_connections()]


def system_context(limit: int = 12) -> str:
    items = list_items(limit=limit)
    if not items:
        return "=== UNIFIED INBOX ===\nNo active inbox items."
    lines = ["=== UNIFIED INBOX ===", f"{unread_count()} unread items:"]
    for item in items:
        lines.append(
            f"- [{item.get('kind', 'item')}/{item.get('status', 'new')}] "
            f"{item.get('title', '')}" + (f" — {item.get('sender')}" if item.get("sender") else "")
        )
    return "\n".join(lines)
