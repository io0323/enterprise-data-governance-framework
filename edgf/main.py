"""FastAPI application: Enterprise Data Governance Control Plane."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from edgf import __version__
from edgf.api import audit, catalog, glossary, issues, kpi, lineage, ops, policy, quality
from edgf.config import settings
from edgf.errors import PROBLEM_RESPONSES, install_problem_handlers
from edgf.observability import RequestContextMiddleware, configure_logging, setup_tracing
from edgf.services.policy import get_policies

log = logging.getLogger("edgf")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_migrate:  # 開発用。本番はデプロイ手順で `alembic upgrade head` を明示実行する
        from edgf import db, migrate
        migrate.upgrade(db.engine)
        log.info("database migrated to head (EDGF_AUTO_MIGRATE)")
    get_policies(reload=True)  # 起動時にポリシー構文を検証 (fail fast)
    log.info("edgf started", extra={"fields": {"version": __version__}})
    yield


def create_app() -> FastAPI:
    configure_logging(settings.log_level, settings.log_format)
    app = FastAPI(
        title="Enterprise Data Governance Framework",
        version=__version__,
        description="Governance Control Plane: Catalog / Ownership / Quality / Lineage / Policy as Code / Audit / KPI\n\n"
                    "エラーはすべて RFC 9457 Problem Details (`application/problem+json`) で返します。"
                    "一覧 API は `limit`/`offset` を受け付け、`X-Total-Count` と `Link` ヘッダを返します。",
        lifespan=lifespan,
    )
    install_problem_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    for r in (catalog.router, glossary.router, quality.router, issues.router,
              lineage.router, policy.router, audit.router, kpi.router):
        app.include_router(r, prefix="/api/v1", responses=PROBLEM_RESPONSES)
    app.include_router(ops.router)
    setup_tracing(app, settings.otel_enabled, settings.service_name)
    return app


app = create_app()
