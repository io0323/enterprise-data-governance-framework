"""§17 KPI API."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.models import DataAsset, Domain, KpiSnapshot
from edgf.pagination import Page, page_params, paginate
from edgf.services import kpi

router = APIRouter(prefix="/kpi", tags=["kpi"])


@router.get("")
def enterprise_kpi(db: Session = Depends(get_session)):
    k = kpi.compute(db)
    k["maturity"] = kpi.maturity_indicator(k)
    return k


@router.get("/domains")
def domain_kpis(request: Request, response: Response, page: Page = Depends(page_params()),
                db: Session = Depends(get_session)):
    domains = paginate(db, select(Domain).order_by(Domain.name), page, request, response)
    return [kpi.compute(db, d.name) for d in domains]


@router.get("/history")
def kpi_history(request: Request, response: Response, scope: Literal["enterprise", "domains"] | None = None, domain: str | None = None,
                date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                page: Page = Depends(page_params()), db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """KPI の推移 (古い順)。ドメイン別は、閲覧できる資産があるドメインか、所属するドメインだけを返す (ABAC)。"""
    stmt = select(KpiSnapshot).order_by(KpiSnapshot.day, KpiSnapshot.scope_type, KpiSnapshot.scope_name)
    if not principal.roles & authz.PRIVILEGED_READERS and "*" not in principal.domains:
        readable = select(DataAsset.domain).where(authz.readable_clause(principal), DataAsset.domain.is_not(None))
        stmt = stmt.where(or_(KpiSnapshot.scope_type == "enterprise", KpiSnapshot.scope_name.in_(readable), KpiSnapshot.scope_name.in_(sorted(principal.domains))))
    if scope == "enterprise":
        stmt = stmt.where(KpiSnapshot.scope_type == "enterprise")
    elif scope == "domains":
        stmt = stmt.where(KpiSnapshot.scope_type == "domain")
    if domain:
        stmt = stmt.where(KpiSnapshot.scope_type == "domain", KpiSnapshot.scope_name == domain)
    if date_from:
        stmt = stmt.where(KpiSnapshot.day >= date_from)
    if date_to:
        stmt = stmt.where(KpiSnapshot.day <= date_to)
    return [{"day": r.day, "scope": "enterprise" if r.scope_type == "enterprise" else r.scope_name, "scope_type": r.scope_type, "metrics": r.metrics}
            for r in paginate(db, stmt, page, request, response)]
