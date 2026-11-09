"""§13 Data Compliance API: Policy評価・例外承認."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.models import DataAsset, PolicyBundle, PolicyException, to_naive_utc, utcnow
from edgf.pagination import Page, page_params, paginate, paginate_list
from edgf.schemas import (ExceptionDecision, ExceptionIn, ExceptionOut, PolicyBundleDetail, PolicyBundleOut, PolicyProposalIn,
                          PolicyRollbackIn)
from edgf.services import audit, issues, policy_bundles
from edgf.services.policy import evaluate_all, evaluate_asset, get_policies, read_disk_files
from edgf.services.policy_bundles import PolicyBundleConflict, PolicyBundleInvalid

router = APIRouter(prefix="/policies", tags=["policy"])

MAX_EXCEPTION_DAYS = 365  # 恒久例外禁止 (§13)


@router.get("")
def list_policies(request: Request, response: Response, page: Page = Depends(page_params()), db: Session = Depends(get_session)):
    """active の版の Policy。"""
    return paginate_list([p.as_dict() for p in get_policies(db=db)], page, request, response)


def _bundle_guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PolicyBundleInvalid as e:
        raise HTTPException(422, str(e)) from e
    except PolicyBundleConflict as e:
        raise HTTPException(409, str(e)) from e


def _proposal_response(bundle: PolicyBundle) -> dict:
    return {"proposal_version": bundle.version, "approval_id": bundle.approval_id, "status": bundle.status,
            "content_hash": bundle.content_hash}


@router.post("/reload", status_code=202)
def reload_policies(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """ディスクの内容から Policy の変更を提案する。承認と公開を経るまで、実行時の評価は変わらない。"""
    authz.require(db, principal, "policy.reload", entity_type="policy", entity_id="*")
    bundle = _bundle_guard(policy_bundles.propose_replacement, db, principal.audit_actor, files=read_disk_files(),
                           proposed_by=principal.subject, reason="ディスクの内容の再読込")
    return _proposal_response(bundle)


def _require_proposer(db: Session, principal: Principal, scope: str, domains: list[str]) -> None:
    """全社の Policy は admin と Committee だけ。全ドメイン (*) に所属する Owner でも Global は提案できない。"""
    if authz.has_global_role(principal, "policy.propose"):
        return
    if scope == "global":
        authz.deny(db, principal, "policy.propose", entity_type="policy", entity_id="*",
                   reason="Global Policy の変更は admin と Committee だけが提案できます", required=authz.PERMISSIONS["policy.propose"])
    for domain in domains:
        authz.require(db, principal, "policy.propose", domain=domain, entity_type="policy", entity_id=domain)


@router.post("/bundles", status_code=202)
def propose_bundle(body: PolicyProposalIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """提案を作る (影響評価を記録し、承認に回す)。Global の上書きや未知の check type は 422。"""
    files, scope, domains = _bundle_guard(policy_bundles.apply_changes, db, body.changes)
    _require_proposer(db, principal, scope, domains)
    bundle = _bundle_guard(policy_bundles.propose_replacement, db, principal.audit_actor, files=files, proposed_by=principal.subject,
                           reason=body.reason)
    return _proposal_response(bundle)


@router.post("/bundles/rollback", status_code=202)
def rollback_bundle(body: PolicyRollbackIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """1 つ前の版に戻す提案を作る。切り戻しにも承認が必要。"""
    if not authz.has_global_role(principal, "policy.propose"):
        authz.deny(db, principal, "policy.propose", entity_type="policy", entity_id="*", reason="切り戻しは admin と Committee だけが提案できます",
                   required=authz.PERMISSIONS["policy.propose"])
    bundle = _bundle_guard(policy_bundles.propose_rollback, db, principal.audit_actor, proposed_by=principal.subject, reason=body.reason)
    return _proposal_response(bundle)


@router.get("/bundles", response_model=list[PolicyBundleOut])
def list_bundles(request: Request, response: Response, status: str | None = None, page: Page = Depends(page_params()),
                 db: Session = Depends(get_session)):
    stmt = select(PolicyBundle).order_by(PolicyBundle.version.desc())
    if status:
        stmt = stmt.where(PolicyBundle.status == status)
    return paginate(db, stmt, page, request, response)


def _get_bundle(db: Session, version: int) -> PolicyBundle:
    bundle = db.get(PolicyBundle, version)
    if bundle is None:
        raise HTTPException(404, f"policy bundle not found: {version}")
    return bundle


@router.get("/bundles/{version}", response_model=PolicyBundleDetail)
def get_bundle(version: int, db: Session = Depends(get_session)):
    return _get_bundle(db, version)


@router.get("/bundles/{version}/diff")
def bundle_diff(version: int, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """提案時に記録した影響評価。閲覧できない資産 (ABAC) は件数だけを返す。"""
    bundle = _get_bundle(db, version)
    result = policy_bundles.diff_view(db, bundle)
    for key in ("newly_failing", "newly_passing"):
        items = result["impact"].get(key, [])
        visible = [i for i in items if (a := db.get(DataAsset, i["asset_id"])) is None or authz.can_read(db, principal, a)]
        result["impact"][key] = visible
        result["impact"][f"{key}_hidden_count"] = len(items) - len(visible)
    return result


@router.post("/bundles/{version}/publish", response_model=PolicyBundleOut)
def publish_bundle(version: int, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """承認済みの版を公開する。旧版は superseded になる。提案の後に active の版が変わっていれば 409。"""
    authz.require(db, principal, "policy.publish", entity_type="policy_bundle", entity_id=str(version))
    bundle = db.get(PolicyBundle, version, with_for_update=True)
    if bundle is None:
        raise HTTPException(404, f"policy bundle not found: {version}")
    _bundle_guard(policy_bundles.publish, db, principal.audit_actor, bundle, principal.subject)
    return bundle


@router.get("/evaluate/assets/{asset_id}")
def evaluate_one(asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id)
    res = evaluate_asset(db, asset)
    return {"asset_id": asset.id, "qualified_name": asset.qualified_name,
            "compliant": all(r["status"] != "fail" for r in res), "results": res}


@router.post("/evaluate")
def evaluate(open_issues: bool = False, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "policy.evaluate_all", entity_type="policy", entity_id="*")
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
    load_asset(db, principal, body.asset_id, "exception.request")
    if body.policy_id not in {p.id for p in get_policies(db=db)}:
        raise HTTPException(422, f"unknown policy: {body.policy_id}")
    expires = to_naive_utc(body.expires_at)
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
                    page: Page = Depends(page_params()), db: Session = Depends(get_session),
                    principal: Principal = Depends(get_principal)):
    readable = select(DataAsset.id).where(authz.readable_clause(principal))
    stmt = select(PolicyException).where(PolicyException.asset_id.in_(readable)) \
        .order_by(PolicyException.created_at.desc(), PolicyException.id)
    if status:
        stmt = stmt.where(PolicyException.status == status)
    return paginate(db, stmt, page, request, response)


@router.post("/exceptions/{exception_id}/decision", response_model=ExceptionOut)
def decide_exception(exception_id: str, body: ExceptionDecision, db: Session = Depends(get_session),
                     principal: Principal = Depends(get_principal)):
    ex = db.get(PolicyException, exception_id)
    if not ex:
        raise HTTPException(404, "exception not found")
    authz.require(db, principal, "exception.decide", entity_type="exception", entity_id=ex.id)
    if ex.status != "pending":
        raise HTTPException(409, f"already decided: {ex.status}")
    if principal.subject == ex.requested_by:
        authz.deny(db, principal, "exception.decide", entity_type="exception", entity_id=ex.id,
                   reason="申請者本人は承認できません (職務分離)")
    ex.status = "approved" if body.approve else "rejected"
    ex.approved_by = principal.subject
    db.flush()
    audit.record(db, principal.audit_actor, "exception.decision", "exception", ex.id, body.model_dump())
    return ex
