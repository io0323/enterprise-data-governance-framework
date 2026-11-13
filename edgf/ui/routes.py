"""UI の配信: 静的ファイル、/ui/config.json、セキュリティヘッダ.

ファイルにデータは含まれないので認証は不要 (データは REST API が認証と ABAC で守る)。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from edgf.config import Settings
from edgf.errors import problem_response

STATIC_DIR = Path(__file__).resolve().parent / "static"
MEDIA_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
               ".json": "application/json; charset=utf-8"}
PERMISSIONS_POLICY = "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"


def issuer_of(s: Settings) -> str:
    return s.ui_oidc_issuer or s.oidc_issuer


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def content_security_policy(s: Settings) -> str:
    """スクリプトは自身の配信だけ (インラインと eval なし)。通信先は自身と IdP の issuer だけ。"""
    origin = _origin(issuer_of(s))
    connect = f"'self' {origin}" if origin else "'self'"
    return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
            f"connect-src {connect}; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")


def security_headers(s: Settings) -> dict[str, str]:
    return {"Content-Security-Policy": content_security_policy(s), "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "Permissions-Policy": PERMISSIONS_POLICY, "Cache-Control": "no-cache"}


def build_router(s: Settings) -> APIRouter:
    router = APIRouter(tags=["ui"], include_in_schema=False)
    headers = security_headers(s)

    def serve(path: str, request: Request) -> Response:
        target = (STATIC_DIR / path).resolve()
        media = MEDIA_TYPES.get(target.suffix)
        if media is None or not target.is_relative_to(STATIC_DIR) or not target.is_file():
            return problem_response(request, 404, "not found", headers=headers)
        return FileResponse(target, media_type=media, headers=headers)

    @router.get("/ui")
    def root() -> Response:
        return RedirectResponse("/ui/", status_code=307, headers=headers)

    @router.get("/ui/")
    def index(request: Request) -> Response:
        return serve("index.html", request)

    @router.get("/ui/config.json")
    def config() -> Response:
        """ログインが使えるか (Phase 17)。ログインはサーバが処理するので、OIDC の設定はブラウザに渡さない。"""
        from edgf.ui.bff import ui_enabled  # 循環 import 回避

        return JSONResponse({"configured": ui_enabled(s)}, headers=headers)

    @router.get("/ui/{path:path}")
    def static(path: str, request: Request) -> Response:
        return serve(path, request)

    return router
