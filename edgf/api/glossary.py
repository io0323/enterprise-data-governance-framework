"""§6.2 Business Glossary API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.models import GlossaryTerm
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import TermIn, TermOut
from edgf.services import audit

router = APIRouter(prefix="/glossary", tags=["glossary"])


def _out(db: Session, principal: Principal, t: GlossaryTerm) -> TermOut:
    # 閲覧できない資産 (ABAC) は紐付け先から除外する
    return TermOut(id=t.id, name=t.name, definition=t.definition, steward=t.steward, synonyms=t.synonyms or [],
                   asset_ids=[a.id for a in t.assets if authz.can_read(db, principal, a)])


def _get(db: Session, term_id: str) -> GlossaryTerm:
    t = db.get(GlossaryTerm, term_id)
    if not t:
        raise HTTPException(404, "term not found")
    return t


@router.post("/terms", response_model=TermOut, status_code=201)
def create_term(body: TermIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "glossary.write", entity_type="term", entity_id=body.name)
    if db.scalar(select(GlossaryTerm).where(GlossaryTerm.name == body.name)):
        raise HTTPException(409, "term already exists (用語は唯一の定義を持つ)")
    t = GlossaryTerm(**body.model_dump())
    db.add(t)
    db.flush()
    audit.record(db, principal.audit_actor, "glossary.create", "term", t.id, body.model_dump())
    return _out(db, principal, t)


@router.get("/terms", response_model=list[TermOut])
def list_terms(request: Request, response: Response, q: str | None = None,
               page: Page = Depends(page_params()), db: Session = Depends(get_session),
               principal: Principal = Depends(get_principal)):
    stmt = select(GlossaryTerm).order_by(GlossaryTerm.name)
    if q:
        stmt = stmt.where(GlossaryTerm.name.ilike(f"%{q}%") | GlossaryTerm.definition.ilike(f"%{q}%"))
    return [_out(db, principal, t) for t in paginate(db, stmt, page, request, response)]


@router.get("/terms/{term_id}", response_model=TermOut)
def get_term(term_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _out(db, principal, _get(db, term_id))


@router.put("/terms/{term_id}/assets/{asset_id}", response_model=TermOut)
def link_asset(term_id: str, asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """用語 → データ資産マッピング (§6.2 必須)。"""
    t = _get(db, term_id)
    a = load_asset(db, principal, asset_id, "glossary.link")
    if a not in t.assets:
        t.assets.append(a)
        db.flush()
        audit.record(db, principal.audit_actor, "glossary.link", "term", t.id, {"asset_id": a.id})
    return _out(db, principal, t)


@router.delete("/terms/{term_id}/assets/{asset_id}", response_model=TermOut)
def unlink_asset(term_id: str, asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    t = _get(db, term_id)
    a = load_asset(db, principal, asset_id, "glossary.link")
    if a in t.assets:
        t.assets.remove(a)
        db.flush()
        audit.record(db, principal.audit_actor, "glossary.unlink", "term", t.id, {"asset_id": a.id})
    return _out(db, principal, t)
