"""§17 KPI API."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.db import get_session
from edgf.models import Domain
from edgf.services import kpi

router = APIRouter(prefix="/kpi", tags=["kpi"])


@router.get("")
def enterprise_kpi(db: Session = Depends(get_session)):
    k = kpi.compute(db)
    k["maturity"] = kpi.maturity_indicator(k)
    return k


@router.get("/domains")
def domain_kpis(db: Session = Depends(get_session)):
    return [kpi.compute(db, d.name) for d in db.scalars(select(Domain).order_by(Domain.name))]
