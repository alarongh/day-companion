"""Fetch transports and a persistent Telegram outbox for the Workers runtime."""

import asyncio
import base64
import json
import time
from collections import defaultdict
from contextvars import ContextVar

import httpx
from workers import Request, fetch

from companion.delivery import NOTICE_CONTEXT
from companion.telegram import BufferedInputFile, Message, encode

IMMEDIATE_REPLIES = ContextVar("immediate_replies", default=False)


class FetchTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        try:
            async with asyncio.timeout(8):
                response = await fetch(
                    str(request.url),
                    method=request.method,
                    headers=dict(request.headers),
                    body=request.content if request.content else None,
                )
                content = await response.bytes()
            headers = dict(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(
                response.status,
                headers=headers,
                content=content,
                request=request,
            )
        except Exception as exc:
            raise httpx.RequestError(
                "External service is unavailable: " + type(exc).__name__, request=request
            ) from None


async def telegram(token, method, payload, files=None):
    async with asyncio.timeout(8):
        return await _telegram(token, method, payload, files)


async def _telegram(token, method, payload, files=None):
    if files:
        # Serialize before fetch: streaming FormData can reach Telegram as an
        # unknown-length body, which it rejects with an empty HTTP 400 response.
        fields = {
            key: json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
            for key, value in payload.items()
            if key not in files
        }
        uploads = {
            key: (file["name"], base64.b64decode(file["data"]), "application/octet-stream")
            for key, file in files.items()
        }
        multipart = httpx.Request("POST", "https://example.invalid", data=fields, files=uploads)
        request = Request(
            f"https://api.telegram.org/bot{token}/{method}",
            method="POST",
            headers={"Content-Type": multipart.headers["content-type"]},
            body=multipart.read(),
        )
        response = await fetch(request)
    else:
        response = await fetch(
            f"https://api.telegram.org/bot{token}/{method}",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload),
        )
    return response.status, await response.text()


class TelegramSession:
    deferred_notices = True

    def __init__(self, store, token=None):
        self.store = store
        self.token = token
        self.delivery_locks = defaultdict(asyncio.Lock)
        self.notice_handler = None
        self.active_outbox = set()

    async def flush(self, uid=None):
        if uid is None:
            users = [
                row[0]
                for row in self.store.db.execute(
                    "SELECT DISTINCT uid FROM cloud_outbox WHERE next_at<=? AND failed=0 LIMIT 8",
                    (time.time(),),
                )
            ]
            await asyncio.gather(*(self.flush(user) for user in users))
            return
        # Interactive handlers already hold the user's configuration lock.
        # Do not wait on a scheduler flush which may be waiting for that lock.
        if IMMEDIATE_REPLIES.get():
            await flush_outbox(
                self.store, self.token, uid=uid, interactive_only=True, active=self.active_outbox
            )
        else:
            async with self.delivery_locks[uid]:
                await flush_outbox(
                    self.store,
                    self.token,
                    uid=uid,
                    notice_handler=self.notice_handler,
                    active=self.active_outbox,
                )

    async def enqueue(self, method, values):
        data, files = {}, {}
        for key, value in values.items():
            if value is None:
                continue
            if isinstance(value, BufferedInputFile):
                data[key] = "attach://" + key
                files[key] = {"name": value.filename, "data": base64.b64encode(value.data).decode()}
            else:
                data[key] = encode(value)
        if NOTICE_CONTEXT.get():
            data["_notice"] = NOTICE_CONTEXT.get()
        self.store.db.execute(
            "INSERT INTO cloud_outbox(uid,method,payload,files,next_at,created) VALUES (?,?,?,?,?,?)",
            (int(data["chat_id"]), method, json.dumps(data), json.dumps(files), time.time(), time.time()),
        )
        if self.token and IMMEDIATE_REPLIES.get():
            await self.flush(int(data["chat_id"]))
        return Message(
            message_id=1, chat={"id": int(data["chat_id"]), "type": "private"}, text=data.get("text")
        )


async def flush_outbox(store, token, uid=None, notice_handler=None, interactive_only=False, active=None):
    condition = " AND uid=?" if uid is not None else ""
    if interactive_only:
        condition += " AND json_extract(payload, '$._notice') IS NULL"
    args = (time.time(), uid) if uid is not None else (time.time(),)
    rows = store.db.execute(
        "SELECT id,uid,method,payload,files,attempts FROM cloud_outbox WHERE next_at<=? AND failed=0"
        + condition
        + " ORDER BY id LIMIT 20",
        args,
    ).fetchall()

    async def deliver(row):
        ident, uid, method, payload, files, attempts = row
        try:
            data = json.loads(payload)
            metadata = data.pop("_notice", None)
            if metadata:
                if notice_handler is None:
                    raise RuntimeError("Scheduled delivery validator is not initialized")
                text = await notice_handler(uid, metadata)
                if text is None:
                    store.db.execute("DELETE FROM cloud_outbox WHERE id=?", (ident,))
                    return
                data["text"] = text
            _, raw = await telegram(token, method, data, json.loads(files))
            result = json.loads(raw)
            if result.get("ok"):
                store.db.execute("DELETE FROM cloud_outbox WHERE id=?", (ident,))
                return
            retry = result.get("parameters", {}).get("retry_after")
            permanent = result.get("error_code") in (400, 401, 403) and retry is None
            if result.get("error_code") == 403 and store.exists(uid):
                p = store.profile(uid)
                p.notifications = False
                store.save_profile(uid, p)
        except Exception:
            retry = None
            permanent = False
        attempts += 1
        store.db.execute(
            "UPDATE cloud_outbox SET attempts=?,next_at=?,failed=? WHERE id=?",
            (
                attempts,
                time.time() + int(retry or min(600, 15 * 2 ** min(attempts, 5))),
                int(permanent or attempts >= 10),
                ident,
            ),
        )

    active = active if active is not None else set()
    for row in rows:
        ident = row[0]
        if ident in active:
            continue
        active.add(ident)
        try:
            await deliver(row)
        finally:
            active.discard(ident)
