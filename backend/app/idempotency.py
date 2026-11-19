"""Idempotent replays for the offline queue (2026-10-21).

The frontend sends `Idempotency-Key: <uuid>` on every queueable write (food entries,
caffeine/alcohol/water, naps, habits, check-ins, parse jobs) from its FIRST attempt. A
write made while offline is stored on the phone and replayed later with the same key, and
a request that timed out on a bad connection may also be retried. Either way the server
must apply it at most once: the first successful (2xx) response is stored against the key,
and any repeat gets that stored response back without touching the handler again.

Pure ASGI (not BaseHTTPMiddleware) so the request body stream is left alone. Keys are kept
14 days (pruned nightly with the WAL checkpoint). Requests without the header are untouched.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)
KEEP_DAYS = 14
_METHODS = {"POST", "PUT", "PATCH"}
_LOCKS: dict = {}


class IdempotencyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in _METHODS:
            return await self.app(scope, receive, send)
        key = None
        for k, v in scope.get("headers", []):
            if k == b"idempotency-key":
                key = v.decode("latin-1").strip()[:100]
                break
        if not key:
            return await self.app(scope, receive, send)

        # One request per key at a time: a retry that arrives while the first is still
        # running waits, then finds the stored response instead of running again.
        lock = _LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            await self._handle(scope, receive, send, key)
        if not lock.locked():
            _LOCKS.pop(key, None)

    async def _handle(self, scope, receive, send, key):
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                text("SELECT status, body, content_type FROM idempotency_keys WHERE key = :k"),
                {"k": key})).first()
        if row is not None:
            logger.info("Idempotent replay of %s %s (key %s…) — stored response returned",
                        scope["method"], scope["path"], key[:8])
            body = (row.body or "").encode()
            await send({"type": "http.response.start", "status": row.status,
                        "headers": [(b"content-type", (row.content_type or "application/json").encode()),
                                    (b"content-length", str(len(body)).encode()),
                                    (b"idempotent-replay", b"true")]})
            await send({"type": "http.response.body", "body": body})
            return

        # Buffer the response and RECORD THE KEY BEFORE ANSWERING. Forwarding first (the
        # first version) let a fast retry arrive before the key existed — verified to
        # double-log on 2026-10-21. Responses here are small JSON, so buffering is free.
        messages, status, ctype, chunks = [], None, "application/json", []

        async def capture(message):
            nonlocal status, ctype
            if message["type"] == "http.response.start":
                status = message["status"]
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-type":
                        ctype = v.decode("latin-1")
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
            messages.append(message)

        await self.app(scope, receive, capture)

        if status and 200 <= status < 300:
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(text(
                        "INSERT OR IGNORE INTO idempotency_keys (key, path, status, body, content_type, created_at) "
                        "VALUES (:k, :p, :s, :b, :c, :t)"),
                        {"k": key, "p": scope["path"], "s": status,
                         "b": b"".join(chunks).decode("utf-8", "replace"),
                         "c": ctype, "t": datetime.utcnow()})
                    await db.commit()
            except Exception as exc:          # never let bookkeeping break a write
                logger.error("Idempotency: storing key failed: %s", exc)
        for m in messages:
            await send(m)


async def prune(db) -> int:
    r = await db.execute(text("DELETE FROM idempotency_keys WHERE created_at < :t"),
                         {"t": datetime.utcnow() - timedelta(days=KEEP_DAYS)})
    await db.commit()
    return r.rowcount or 0
