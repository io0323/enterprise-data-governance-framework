"""§13 Evidence / §19 Auditability API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.db import get_session
from edgf.models import AuditEvent
from edgf.services.audit import verify_chain

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events")
def list_events(entity_id: str | None = None, action: str | None = None, actor: str | None = None,
                limit: int = Query(100, le=1000), db: Session = Depends(get_session)):
    stmt = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
    for col, val in (("entity_id", entity_id), ("action", action), ("actor", actor)):
        if val:
            stmt = stmt.where(getattr(AuditEvent, col) == val)
    return [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action, "entity_type": e.entity_type,
             "entity_id": e.entity_id, "payload": e.payload, "hash": e.hash} for e in db.scalars(stmt)]


@router.get("/verify")
def verify(db: Session = Depends(get_session)):
    return verify_chain(db)
