"""§19 統一エラーモデル: RFC 9457 Problem Details for HTTP APIs.

全 API のエラーは `application/problem+json` で次の形式を返す:
    {"type", "title", "status", "detail", "instance", "request_id", ...拡張メンバ}
- type: `urn:edgf:problem:<slug>`(エラー種別の安定識別子。クライアントはこれで分岐する)
- 拡張メンバ: 422 の `errors`、Certification の `blockers`、/ready の `checks` など
"""
from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

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


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled exception", extra={"fields": {"path": request.url.path}})
    return problem_response(request, 500, "内部エラーが発生しました。request_id を添えて管理者に連絡してください",
                            type_="internal-error")


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, _integrity)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled)
