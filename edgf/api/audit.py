"""§13 Evidence / §19 Auditability API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.models import AuditAnchor, AuditEvent
from edgf.pagination import Page, page_params, paginate
from edgf.services.audit_anchor import anchoring_in_use, verify_all

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events")
def list_events(request: Request, response: Response, entity_id: str | None = None, action: str | None = None,
                actor: str | None = None, request_id: str | None = None, page: Page = Depends(page_params()),
                db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """監査ログは全資産の操作内容を含むため Committee / admin のみ参照できる (§13, §19 Security)。"""
    authz.require(db, principal, "audit.read", entity_type="audit", entity_id="*")
    stmt = select(AuditEvent).order_by(AuditEvent.seq.desc())
    for col, val in (("entity_id", entity_id), ("action", action), ("actor", actor), ("request_id", request_id)):
        if val:
            stmt = stmt.where(getattr(AuditEvent, col) == val)
    return [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action, "entity_type": e.entity_type,
             "entity_id": e.entity_id, "payload": e.payload, "request_id": e.request_id, "hash": e.hash} for e in paginate(db, stmt, page, request, response)]


@router.get("/verify")
def verify(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """チェーンの検証と、最新のアンカーとの照合。

    アンカーの詳細は audit.read を持つ利用者にだけ返す。アンカリングを使っていなければ、応答は従来と同じ。
    """
    result = verify_all(db)
    if not authz.is_allowed(principal, "audit.read") or not anchoring_in_use(result):
        return {k: result[k] for k in ("valid", "events_checked", "broken_at_seq")}
    return result


@router.get("/anchors")
def list_anchors(request: Request, response: Response, page: Page = Depends(page_params()), db: Session = Depends(get_session),
                 principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "audit.read", entity_type="audit", entity_id="anchors")
    stmt = select(AuditAnchor).order_by(AuditAnchor.seq.desc())
    return [{"seq": a.seq, "hash": a.hash, "anchored_at": a.anchored_at, "key_id": a.key_id, "signature": a.signature,
             "sinks": {name: {k: v for k, v in result.items() if k != "token"} for name, result in a.sinks.items()}}
            for a in paginate(db, stmt, page, request, response)]
