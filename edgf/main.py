"""FastAPI application: Enterprise Data Governance Control Plane."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from edgf import __version__
from edgf.api import audit, catalog, glossary, issues, kpi, lineage, mdm, ops, policy, privacy, quality
from edgf.auth import build_authenticator
from edgf.config import Settings, settings
from edgf.deps import get_principal
from edgf.errors import PROBLEM_RESPONSES, install_problem_handlers
from edgf.observability import RequestContextMiddleware, configure_logging, setup_tracing
from edgf.services.mdm_rules import get_entities
from edgf.services.policy import get_policies
from edgf.services.subject_ref import validate_subject_key

log = logging.getLogger("edgf")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_migrate:  # 開発用。本番はデプロイ手順で `alembic upgrade head` を明示実行する
        from edgf import db, migrate
        migrate.upgrade(db.engine)
        log.info("database migrated to head (EDGF_AUTO_MIGRATE)")
    get_policies(reload=True)  # 起動時にポリシー構文を検証 (fail fast)
    get_entities(reload=True)  # MDM のエンティティ定義も同様
    log.info("edgf started", extra={"fields": {"version": __version__}})
    yield


def create_app(app_settings: Settings | None = None) -> FastAPI:
    s = app_settings or settings
    configure_logging(s.log_level, s.log_format)
    authenticator = build_authenticator(s)  # 設定不備・production での dev モードはここで停止 (fail fast)
    validate_subject_key(s)  # production で EDGF_SUBJECT_KEY が無ければ停止
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
    install_problem_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    for r in (catalog.router, glossary.router, quality.router, issues.router,
              lineage.router, policy.router, audit.router, kpi.router, privacy.router, mdm.router):
        # /api/v1 はすべて認証必須 (認可は各エンドポイントで edgf.authz が判定)
        app.include_router(r, prefix="/api/v1", responses=PROBLEM_RESPONSES, dependencies=[Depends(get_principal)])
    app.include_router(ops.router)
    setup_tracing(app, s.otel_enabled, s.service_name)
    return app


app = create_app()
