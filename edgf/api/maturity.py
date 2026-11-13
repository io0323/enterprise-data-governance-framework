"""§20 ドメイン別の成熟度の評価 API (Phase 18)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.models import MaturityAssessment
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import MaturityCancelIn, MaturityCreateIn, MaturityDecisionIn, MaturityReferenceIn, MaturitySelfAssessmentIn
from edgf.services import maturity, maturity_criteria

router = APIRouter(prefix="/maturity", tags=["maturity"])


@router.get("/criteria")
def get_criteria(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """active の Policy Bundle の基準 (承認と公開を経たもの)。"""
    authz.require(db, principal, "maturity.read", entity_type="maturity", entity_id="criteria")
    return maturity_criteria.active(db).as_dict()


@router.post("/assessments", status_code=201)
def create_assessment(body: MaturityCreateIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "maturity.assessment.create", domain=body.domain, entity_type="maturity", entity_id=body.domain)
    return maturity.view(maturity.create(db, principal.audit_actor, domain=body.domain, created_by=principal.subject))


@router.get("/assessments")
def list_assessments(request: Request, response: Response, domain: str | None = None, status: str | None = None,
                     page: Page = Depends(page_params()), db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """評価の履歴 (新しい順)。認証済みの全員が参照できる。"""
    authz.require(db, principal, "maturity.read", entity_type="maturity", entity_id="*")
    stmt = select(MaturityAssessment).order_by(MaturityAssessment.created_at.desc(), MaturityAssessment.id)
    for column, value in ((MaturityAssessment.domain, domain), (MaturityAssessment.status, status)):
        if value is not None:
            stmt = stmt.where(column == value)
    return [maturity.view(row, detail=False) for row in paginate(db, stmt, page, request, response)]


@router.get("/assessments/{assessment_id}")
def get_assessment(assessment_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "maturity.read", entity_type="maturity_assessment", entity_id=assessment_id)
    return maturity.view(maturity.get(db, assessment_id))


@router.post("/assessments/{assessment_id}/evaluate")
def evaluate_assessment(assessment_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """自動の判定 (やり直しもできる。自己評価の後はできない)。"""
    row = maturity.get(db, assessment_id, lock=True)
    authz.require(db, principal, "maturity.assessment.create", domain=row.domain, entity_type="maturity_assessment", entity_id=row.id)
    return maturity.view(maturity.evaluate(db, principal.audit_actor, row))


@router.post("/assessments/{assessment_id}/cancel")
def cancel_assessment(assessment_id: str, body: MaturityCancelIn, db: Session = Depends(get_session),
                      principal: Principal = Depends(get_principal)):
    row = maturity.get(db, assessment_id, lock=True)
    authz.require(db, principal, "maturity.assessment.create", domain=row.domain, entity_type="maturity_assessment", entity_id=row.id)
    return maturity.view(maturity.cancel(db, principal.audit_actor, row, reason=body.reason))


@router.put("/assessments/{assessment_id}/self-assessment")
def self_assess(assessment_id: str, body: MaturitySelfAssessmentIn, db: Session = Depends(get_session),
                principal: Principal = Depends(get_principal)):
    """対象ドメインの Owner の自己評価。手動の基準ごとに、達成したかと証拠 (https の URI と説明)。"""
    row = maturity.get(db, assessment_id, lock=True)
    authz.require(db, principal, "maturity.self_assess", domain=row.domain, entity_type="maturity_assessment", entity_id=row.id)
    items = [i.model_dump() for i in body.items]
    return maturity.view(maturity.self_assess(db, principal.audit_actor, principal, row, items))


@router.post("/assessments/{assessment_id}/decision")
def decide(assessment_id: str, body: MaturityDecisionIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Committee の判定 (承認エンジンの maturity_assessment)。上限を超えると 422、下げるときは理由が必要。"""
    row = maturity.get(db, assessment_id, lock=True)
    authz.require(db, principal, "maturity.decide", domain=row.domain, entity_type="maturity_assessment", entity_id=row.id)
    return maturity.view(maturity.decide(db, principal.audit_actor, principal, row, level=body.level, manual=body.manual,
                                         reason=body.reason))


@router.get("/references")
def list_references(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "maturity.read", entity_type="maturity", entity_id="references")
    return maturity.references(db)


@router.post("/references", status_code=201)
def set_reference(body: MaturityReferenceIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """判定済みのドメインを参照ドメイン (先行ドメイン) に指定する (Committee)。"""
    authz.require(db, principal, "maturity.reference.set", entity_type="maturity_reference", entity_id=body.domain)
    maturity.set_reference(db, principal.audit_actor, principal, body.domain, body.reason)
    return next(r for r in maturity.references(db) if r["domain"] == body.domain)


@router.delete("/references/{domain}", status_code=204)
def unset_reference(domain: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "maturity.reference.set", entity_type="maturity_reference", entity_id=domain)
    maturity.unset_reference(db, principal.audit_actor, domain)
    return Response(status_code=204)
