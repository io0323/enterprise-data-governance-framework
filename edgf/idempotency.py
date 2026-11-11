"""Idempotency-Key のミドルウェア (edgf/services/idempotency.py の HTTP の側).

鍵は操作者ごとに区別する。操作者を特定できない要求 (認証の失敗) はそのまま通し、エンドポイントが 401 を返す。
"""
from __future__ import annotations

import re

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from edgf.auth import AuthError, AuthUnavailable
from edgf.errors import ProblemError, problem_response
from edgf.services import idempotency

HEADER = b"idempotency-key"
METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# 応答にデータの値を含む API。応答を保存すると Control Plane に値が残る (§18.2) ので、鍵があっても扱わない。
# いずれも同じ入力なら同じ結果になる冪等な操作で、SDK は鍵を付けずに再送する
NO_STORE_PATHS = (re.compile(r"^/api/v1/mdm/[^/]+/golden-records/[^/]+/compose$"),)


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in METHODS:
            await self.app(scope, receive, send)
            return
        raw = dict(scope.get("headers") or []).get(HEADER)
        if raw is None or any(p.match(scope["path"]) for p in NO_STORE_PATHS):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        key = raw.decode("latin-1")
        if not idempotency.KEY_PATTERN.match(key):
            await problem_response(request, 422, "Idempotency-Key は英数字と ._:- の 8〜200 文字で指定してください",
                                   type_="idempotency-key-invalid")(scope, receive, send)
            return
        try:
            actor = scope["app"].state.authenticator.authenticate(request).audit_actor
        except (AuthError, AuthUnavailable):
            await self.app(scope, receive, send)  # 認証の失敗はエンドポイントの 401 / 503 に任せる
            return
        body = b""
        try:
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
        except ProblemError as e:  # 本文の上限と JSON の入れ子の上限 (BodyLimitMiddleware) は受信の途中で例外になる
            await problem_response(request, e.status_code, e.detail, type_=e.type_)(scope, receive, send)
            return
        fingerprint = idempotency.request_hash(scope["method"], scope["path"], scope.get("query_string", b"").decode("latin-1"), body)
        try:
            stored = idempotency.begin(actor, key, fingerprint)
        except idempotency.KeyReused:
            await problem_response(request, 422, "この Idempotency-Key は別の内容の要求に使われています", type_="idempotency-key-reused")(scope, receive, send)
            return
        except idempotency.InProgress:
            await problem_response(request, 409, "同じ Idempotency-Key の要求を処理しています。時間をおいて再送してください",
                                   type_="idempotency-in-progress", headers={"Retry-After": "1"})(scope, receive, send)
            return
        if stored is not None:
            headers = [(b"content-type", (stored.content_type or "application/json").encode()), (b"idempotent-replayed", b"true")]
            await send({"type": "http.response.start", "status": stored.status_code, "headers": headers})
            await send({"type": "http.response.body", "body": stored.body.encode("utf-8")})
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        # 応答はいったん受け取り、記録を保存してから返す。返してから保存すると、受け取ったクライアントの
        # すぐの再送が「処理中」に見えてしまう
        status, content_type, messages = 500, None, []

        async def capture(message: Message) -> None:
            nonlocal status, content_type
            if message["type"] == "http.response.start":
                status = message["status"]
                content_type = dict(message.get("headers") or []).get(b"content-type", b"").decode("latin-1") or None
            messages.append(message)

        try:
            await self.app(scope, replay, capture)
        finally:
            body_bytes = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            idempotency.finish(actor, key, status, content_type, body_bytes)
        for message in messages:
            await send(message)
