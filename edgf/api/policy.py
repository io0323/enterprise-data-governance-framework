"""§13 Data Compliance API: Policy評価・例外承認."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.db import get_session
from edgf.auth import Principal
from edgf.deps import get_asset_or_404, get_principal
from edgf.models import DataAsset, PolicyException, utcnow
from edgf.pagination import Page, page_params, paginate, paginate_list
from edgf.schemas import ExceptionDecision, ExceptionIn, ExceptionOut
from edgf.services import audit, issues
from edgf.services.policy import PolicyLoadError, evaluate_all, evaluate_asset, get_policies

router = APIRouter(prefix="/policies", tags=["policy"])

MAX_EXCEPTION_DAYS = 365  # 恒久例外禁止 (§13)


@router.get("")
def list_policies(request: Request, response: Response, page: Page = Depends(page_params())):
    return paginate_list([p.as_dict() for p in get_policies()], page, request, response)


@router.post("/reload")
def reload_policies(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    try:
        ps = get_policies(reload=True)
    except PolicyLoadError as e:
        raise HTTPException(422, str(e)) from e
    audit.record(db, principal.audit_actor, "policy.reload", "policy", "*", {"count": len(ps), "ids": [p.id for p in ps]})
    return {"loaded": len(ps)}


@router.get("/evaluate/assets/{asset_id}")
def evaluate_one(asset_id: str, db: Session = Depends(get_session)):
    asset = get_asset_or_404(db, asset_id)
    res = evaluate_asset(db, asset)
    return {"asset_id": asset.id, "qualified_name": asset.qualified_name,
            "compliant": all(r["status"] != "fail" for r in res), "results": res}


@router.post("/evaluate")
def evaluate(open_issues: bool = False, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """全資産の自動監査。open_issues=true で違反を Issue 化 (重複起票なし)。"""
    report = evaluate_all(db)
    opened = 0
    if open_issues:
        for a in report["assets"]:
            asset = db.get(DataAsset, a["asset_id"])
            for v in a["violations"]:
                if issues.open_issue(db, principal.audit_actor, asset=asset, kind="policy", severity=v["severity"],
                                     source_ref=f"policy:{v['policy_id']}",
                                     title=f"[POLICY] {a['qualified_name']}: {v['policy_id']} {v['name']}",
                                     description=v["message"]):
                    opened += 1
    audit.record(db, principal.audit_actor, "policy.evaluate", "policy", "*",
                 {"compliance_rate": report["compliance_rate"], "issues_opened": opened})
    report["issues_opened"] = opened
    return report


@router.post("/exceptions", response_model=ExceptionOut, status_code=201)
def request_exception(body: ExceptionIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    get_asset_or_404(db, body.asset_id)
    if body.policy_id not in {p.id for p in get_policies()}:
        raise HTTPException(422, f"unknown policy: {body.policy_id}")
    expires = body.expires_at.replace(tzinfo=None)
    now = utcnow()
    if expires <= now:
        raise HTTPException(422, "expires_at must be in the future")
    if expires > now + timedelta(days=MAX_EXCEPTION_DAYS):
        raise HTTPException(422, f"例外期間は最大{MAX_EXCEPTION_DAYS}日です (恒久例外禁止)")
    ex = PolicyException(policy_id=body.policy_id, asset_id=body.asset_id, reason=body.reason,
                         risk_assessment=body.risk_assessment, requested_by=principal.subject, expires_at=expires)
    db.add(ex)
    db.flush()
    audit.record(db, principal.audit_actor, "exception.request", "exception", ex.id, body.model_dump(mode="json"))
    return ex


@router.get("/exceptions", response_model=list[ExceptionOut])
def list_exceptions(request: Request, response: Response, status: str | None = None,
                    page: Page = Depends(page_params()), db: Session = Depends(get_session)):
    stmt = select(PolicyException).order_by(PolicyException.created_at.desc(), PolicyException.id)
    if status:
        stmt = stmt.where(PolicyException.status == status)
    return paginate(db, stmt, page, request, response)


@router.post("/exceptions/{exception_id}/decision", response_model=ExceptionOut)
def decide_exception(exception_id: str, body: ExceptionDecision, db: Session = Depends(get_session),
                     principal: Principal = Depends(get_principal)):
    ex = db.get(PolicyException, exception_id)
    if not ex:
        raise HTTPException(404, "exception not found")
    if ex.status != "pending":
        raise HTTPException(409, f"already decided: {ex.status}")
    if principal.subject == ex.requested_by:
        raise HTTPException(403, "申請者本人は承認できません (職務分離)")
    ex.status = "approved" if body.approve else "rejected"
    ex.approved_by = principal.subject
    db.flush()
    audit.record(db, principal.audit_actor, "exception.decision", "exception", ex.id, body.model_dump())
    return ex
