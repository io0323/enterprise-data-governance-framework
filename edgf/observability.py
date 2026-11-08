"""§19 Observability: 構造化ログ (JSON)・request_id 伝播・任意の OpenTelemetry.

- すべてのログ行に request_id を付与し、レスポンスヘッダ X-Request-ID で返す。
  クライアント/上流ゲートウェイが X-Request-ID を付与していればそれを引き継ぐ。
- OpenTelemetry は EDGF_OTEL_ENABLED=true かつ extra `otel` 導入時のみ有効 (未導入なら警告して継続)。
  エクスポート先は OTel 標準の環境変数 (OTEL_EXPORTER_OTLP_ENDPOINT 等) で指定する (製品非依存)。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_RID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")  # ログインジェクション防止

request_id_var: ContextVar[str | None] = ContextVar("edgf_request_id", default=None)

try:  # pragma: no cover - otel 導入時のみ
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover
    _otel_trace = None

_HANDLER_MARK = "_edgf_handler"


def current_request_id(request: Request | None = None) -> str | None:
    if request is not None:
        rid = request.scope.get("state", {}).get("request_id")
        if rid:
            return rid
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    """1行1JSON。`extra={"fields": {...}}` で任意の構造化フィールドを追加できる。"""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            out["request_id"] = rid
        if _otel_trace is not None:  # pragma: no cover - otel 導入時のみ
            ctx = _otel_trace.get_current_span().get_span_context()
            if ctx.is_valid:
                out["trace_id"] = format(ctx.trace_id, "032x")
                out["span_id"] = format(ctx.span_id, "016x")
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for k, v in fields.items():
                out.setdefault(k, v)
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json", stream: Any = None) -> logging.Logger:
    """edgf.* ロガーを設定する (冪等)。root ロガーは変更しない。"""
    logger = logging.getLogger("edgf")
    for h in list(logger.handlers):
        if getattr(h, _HANDLER_MARK, False):
            logger.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stdout)
    setattr(handler, _HANDLER_MARK, True)
    handler.setFormatter(JsonFormatter() if fmt == "json"
                         else logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


class RequestContextMiddleware:
    """request_id の採番/引継ぎとアクセスログ (純 ASGI: contextvar がエンドポイントまで伝播する)。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.log = logging.getLogger("edgf.access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(REQUEST_ID_HEADER.lower().encode(), b"").decode("latin-1")
        rid = incoming if _VALID_RID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        token = request_id_var.set(rid)
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", rid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            fields = {"method": scope.get("method"), "path": scope.get("path"), "status": status,
                      "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
            actor = scope.get("state", {}).get("actor")  # edgf.deps.get_principal が設定
            if actor:
                fields["actor"] = actor
            self.log.info("request completed", extra={"fields": fields})
            request_id_var.reset(token)


def setup_tracing(app: Any, enabled: bool, service_name: str = "edgf") -> bool:
    """OpenTelemetry を任意で有効化する。成功時 True。"""
    log = logging.getLogger("edgf.observability")
    if not enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("EDGF_OTEL_ENABLED=true ですが OpenTelemetry が未導入です (pip install '.[otel]')。トレーシングは無効です")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))  # pragma: no cover
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # pragma: no cover
    trace.set_tracer_provider(provider)  # pragma: no cover
    FastAPIInstrumentor.instrument_app(app)  # pragma: no cover
    log.info("OpenTelemetry tracing enabled", extra={"fields": {"service_name": service_name}})  # pragma: no cover
    return True  # pragma: no cover
