"""FastAPI application: Enterprise Data Governance Control Plane."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from edgf import __version__
from edgf.api import audit, catalog, glossary, issues, kpi, lineage, policy, quality
from edgf.db import init_db
from edgf.services.policy import get_policies


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    get_policies(reload=True)  # 起動時にポリシー構文を検証 (fail fast)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Enterprise Data Governance Framework",
        version=__version__,
        description="Governance Control Plane: Catalog / Ownership / Quality / Lineage / Policy as Code / Audit / KPI",
        lifespan=lifespan,
    )
    for r in (catalog.router, glossary.router, quality.router, issues.router,
              lineage.router, policy.router, audit.router, kpi.router):
        app.include_router(r, prefix="/api/v1")

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
