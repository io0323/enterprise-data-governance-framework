"""FastAPI application: Enterprise Data Governance Control Plane."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from edgf import __version__
from edgf.api import ai, approvals, audit, catalog, discovery, usage, glossary, issues, jobs, kpi, lineage, mdm, mesh, ops, policy, privacy, quality, refdata, reviews, webhooks
from edgf.auth import build_authenticator
from edgf.config import Settings, settings
from edgf.deps import get_principal
from edgf.errors import PROBLEM_RESPONSES, BodyLimitMiddleware, install_problem_handlers
from edgf.idempotency import IdempotencyMiddleware
from edgf.observability import RequestContextMiddleware, configure_logging, setup_tracing
from edgf.services.audit_anchor import validate_anchor_settings
from edgf.services.discovery_rules import get_rules
from edgf.services.probe import validate_probe_settings
from edgf.services.mdm_rules import get_entities
from edgf.ui import bff, routes as ui_routes
from edgf.services.policy_bundles import bootstrap_on_startup
from edgf.services.subject_ref import validate_subject_key

log = logging.getLogger("edgf")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_migrate:  # 開発用。本番はデプロイ手順で `alembic upgrade head` を明示実行する
        from edgf import db, migrate
        migrate.upgrade(db.engine)
        log.info("database migrated to head (EDGF_AUTO_MIGRATE)")
    bootstrap_on_startup()  # active の版が無いときだけディスクから取り込む。不正なら fail fast
    get_entities(reload=True)  # MDM のエンティティ定義も同様
    get_rules(reload=True)  # Discovery のソースの対応表も同様
    log.info("edgf started", extra={"fields": {"version": __version__}})
    yield


def create_app(app_settings: Settings | None = None) -> FastAPI:
    s = app_settings or settings
    configure_logging(s.log_level, s.log_format)
    authenticator = build_authenticator(s)  # 設定不備・production での dev モードはここで停止 (fail fast)
    validate_subject_key(s)  # production で EDGF_SUBJECT_KEY が無ければ停止
    validate_anchor_settings(s)  # production でアンカリングが有効なのに署名鍵が無ければ停止
    validate_probe_settings(s)  # 到達の確認が有効なのに許可するホストが無ければ停止
    bff.validate_ui_settings(s)  # production で UI を有効にしたのにクライアントの秘密が無ければ停止
    app = FastAPI(
        title="Enterprise Data Governance Framework",
        version=__version__,
        description="Governance Control Plane: Catalog / Ownership / Quality / Lineage / Policy as Code / Audit / KPI\n\n"
                    "エラーはすべて RFC 9457 Problem Details (`application/problem+json`) で返します。"
                    "一覧 API は `limit`/`offset` を受け付け、`X-Total-Count` と `Link` ヘッダを返します。",
        lifespan=lifespan,
    )
    app.state.authenticator = authenticator
    app.state.auth_mode = authenticator.mode
    app.state.settings = s
    install_problem_handlers(app)
    app.add_middleware(IdempotencyMiddleware)  # 本文の上限の内側 (上限を超えた本文は読まない)
    app.add_middleware(BodyLimitMiddleware, max_bytes=s.max_body_bytes)
    app.add_middleware(RequestContextMiddleware)  # 後に追加したものが外側になる (413 にも request_id を付ける)
    for r in (catalog.router, glossary.router, quality.router, issues.router,
              lineage.router, policy.router, audit.router, kpi.router, privacy.router, mdm.router, refdata.router, ai.router, mesh.router, approvals.router, webhooks.router, reviews.router, jobs.router,
              discovery.router, usage.router):
        # /api/v1 はすべて認証必須 (認可は各エンドポイントで edgf.authz が判定)
        app.include_router(r, prefix="/api/v1", responses=PROBLEM_RESPONSES, dependencies=[Depends(get_principal)])
    app.include_router(ops.router)
    app.state.ui_oidc = bff.OidcClient(s)  # IdP との通信 (テストでは差し替える)
    app.include_router(bff.build_router(s, app.state.ui_oidc))  # ログイン (BFF) とセッション。静的ファイルの catch-all より先に登録する
    app.include_router(ui_routes.build_router(s))  # 静的ファイルと config.json だけで、データは含まない
    setup_tracing(app, s.otel_enabled, s.service_name)
    return app


app = create_app()
