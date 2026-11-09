"""§16 / §19 Observability: ジョブの状況と手動実行."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from edgf import authz, jobs
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.services import audit

router = APIRouter(prefix="/ops/jobs", tags=["ops"])


@router.get("")
def list_jobs(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """最後に実行した時刻と、失敗の状況。"""
    authz.require(db, principal, "jobs.read", entity_type="job", entity_id="*")
    overdue = set(jobs.overdue_jobs())
    return [{**jobs.status_of(db, job), "overdue": job.name in overdue} for job in jobs.JOBS.values()]


@router.post("/{name}/run")
def run_job(name: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """手動で実行する。他で実行中なら skipped を返す。無効のジョブも実行できる。"""
    authz.require(db, principal, "jobs.run", entity_type="job", entity_id=name)
    if name not in jobs.JOBS:
        raise HTTPException(404, f"job not found: {name}")
    outcome = jobs.run_job(name, force=True)
    audit.record(db, principal.audit_actor, "jobs.run", "job", name, {"status": outcome["status"], "count": outcome.get("count", 0)})
    return outcome
