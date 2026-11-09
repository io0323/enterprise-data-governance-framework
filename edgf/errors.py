"""§19 統一エラーモデル: RFC 9457 Problem Details for HTTP APIs.

全 API のエラーは `application/problem+json` で次の形式を返す:
    {"type", "title", "status", "detail", "instance", "request_id", ...拡張メンバ}
- type: `urn:edgf:problem:<slug>`(エラー種別の安定識別子。クライアントはこれで分岐する)
- 拡張メンバ: 422 の `errors`、Certification の `blockers`、/ready の `checks` など
"""
from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from edgf.observability import REQUEST_ID_HEADER, current_request_id

PROBLEM_JSON = "application/problem+json"
PROBLEM_TYPE_PREFIX = "urn:edgf:problem:"

log = logging.getLogger("edgf.errors")


class ProblemDetail(BaseModel):
    """OpenAPI 表示用のスキーマ (RFC 9457 §3)。"""
    model_config = ConfigDict(extra="allow")
    type: str = Field(description="エラー種別の URI (urn:edgf:problem:<slug>)")
    title: str = Field(description="エラー種別の要約 (HTTP ステータスの名称)")
    status: int
    detail: str | None = Field(default=None, description="この発生に固有の説明")
    instance: str | None = Field(default=None, description="発生したリクエストのパス")
    request_id: str | None = Field(default=None, description="X-Request-ID (ログ・監査との突合用)")


PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"description": f"Problem Details (RFC 9457) - {code}",
           "content": {PROBLEM_JSON: {"schema": ProblemDetail.model_json_schema()}}}
    for code in ("4XX", "5XX")
}


def _slug(status: int) -> str:
    try:
        return HTTPStatus(status).phrase.lower().replace(" ", "-").replace("'", "")
    except ValueError:
        return "error"


def _title(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


class ProblemError(HTTPException):
    """拡張メンバ付きで Problem を返すための例外。

    raise ProblemError(409, "certification blocked", type_="certification-blocked", blockers=[...])
    """

    def __init__(self, status: int, detail: str, *, type_: str | None = None, title: str | None = None,
                 headers: dict[str, str] | None = None, **extensions: Any) -> None:
        super().__init__(status_code=status, detail=detail, headers=headers)
        self.type_ = type_
        self.title = title
        self.extensions = extensions


def problem_response(request: Request, status: int, detail: str | None = None, *, type_: str | None = None,
                     title: str | None = None, extensions: dict[str, Any] | None = None,
                     headers: dict[str, str] | None = None) -> JSONResponse:
    rid = current_request_id(request)
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_PREFIX + (type_ or _slug(status)),
        "title": title or _title(status),
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "request_id": rid,
    }
    for k, v in (extensions or {}).items():
        if k not in body:  # 標準メンバは上書きさせない
            body[k] = v
    resp = JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)
    if rid:
        resp.headers[REQUEST_ID_HEADER] = rid
    return resp


MAX_JSON_DEPTH = 64  # API の入力で意味のある入れ子はこれより十分に浅い。深いと応答の直列化で失敗する
_JSON_TOKEN = re.compile(rb'["\\\[\]{}]')


class _JsonDepth:
    """JSON の入れ子の深さを、パースせずに数える (受信の断片にまたがってもよい)。"""

    def __init__(self) -> None:
        self.depth = 0
        self.in_string = False
        self.escaped = False  # 断片の末尾がエスケープの途中だった

    def feed(self, chunk: bytes) -> int:
        start = 1 if self.escaped and chunk else 0
        self.escaped = self.escaped and not chunk
        deepest = self.depth
        position = start
        while True:
            m = _JSON_TOKEN.search(chunk, position)
            if m is None:
                return deepest
            token = m.group()
            position = m.end()
            if token == b"\\":
                if self.in_string:
                    if position >= len(chunk):
                        self.escaped = True
                    position += 1
            elif token == b'"':
                self.in_string = not self.in_string
            elif not self.in_string:
                self.depth += 1 if token in (b"[", b"{") else -1
                deepest = max(deepest, self.depth)


class BodyLimitMiddleware:
    """本文の大きさと JSON の入れ子を制限する。Content-Length を信用せず、受信した量を数える (chunked でも超えられない)。"""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _too_large(self) -> ProblemError:
        return ProblemError(413, f"リクエストの本文が上限 ({self.max_bytes} バイト) を超えています", type_="payload-too-large")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            error = self._too_large()
            await problem_response(Request(scope), 413, error.detail, type_=error.type_)(scope, receive, send)
            return
        received = 0
        content_type = dict(scope.get("headers") or []).get(b"content-type", b"").lower()
        depth = _JsonDepth() if b"json" in content_type else None

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise self._too_large()  # 本文を読む FastAPI の中で発生するので、通常の例外処理で Problem になる
                if depth is not None and depth.feed(message.get("body", b"")) > MAX_JSON_DEPTH:
                    raise ProblemError(422, f"JSON の入れ子が上限 ({MAX_JSON_DEPTH} 段) を超えています", type_="json-too-deep")
            return message

        await self.app(scope, counting_receive, send)


async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc, ProblemError):
        return problem_response(request, exc.status_code, exc.detail, type_=exc.type_, title=exc.title,
                                extensions=exc.extensions, headers=exc.headers)
    detail, ext = exc.detail, {}
    if isinstance(detail, dict):  # 旧形式 HTTPException(409, {"message": ..., ...}) との互換
        ext = {k: v for k, v in detail.items() if k != "message"}
        detail = detail.get("message")
    return problem_response(request, exc.status_code, None if detail is None else str(detail),
                            extensions=ext, headers=getattr(exc, "headers", None))


async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
              for e in exc.errors()]
    return problem_response(request, 422, "リクエストの検証に失敗しました", type_="validation-error",
                            extensions={"errors": errors})


async def _integrity(request: Request, exc: IntegrityError) -> JSONResponse:
    # SQL や制約名などの内部情報はレスポンスに含めない (ログのみ)
    log.warning("integrity constraint violated", extra={"fields": {"error": str(exc.orig)}})
    return problem_response(request, 409, "整合性制約に違反したため処理できませんでした (同時更新の可能性があります)",
                            type_="integrity-conflict")


async def _data_error(request: Request, exc: DataError) -> JSONResponse:
    # 列の長さの超過や NUL 文字など。SQLite では起きず PostgreSQL でだけ起きる入力の問題 (値は応答にもログにも出さない)
    log.warning("invalid value rejected by database", extra={"fields": {"error": type(exc.orig).__name__}})
    return problem_response(request, 422, "データベースに保存できない値が含まれています (長さの超過や制御文字など)",
                            type_="invalid-value")


async def _operational(request: Request, exc: OperationalError) -> JSONResponse:
    # デッドロックや接続断など、時間をおけば回復する DB の障害。SQL などの内部情報は返さない
    sqlstate = getattr(exc.orig, "sqlstate", None)
    log.warning("database operation failed", extra={"fields": {"error": type(exc.orig).__name__, "sqlstate": sqlstate}})
    return problem_response(request, 503, "データベースが処理できませんでした。時間をおいて再試行してください",
                            type_="database-unavailable", headers={"Retry-After": "1"})


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled exception", extra={"fields": {"path": request.url.path}})
    return problem_response(request, 500, "内部エラーが発生しました。request_id を添えて管理者に連絡してください",
                            type_="internal-error")


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, _integrity)  # type: ignore[arg-type]
    app.add_exception_handler(DataError, _data_error)  # type: ignore[arg-type]
    app.add_exception_handler(OperationalError, _operational)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled)
