"""§5 Data Quality API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_asset_or_404, get_principal, load_asset
from edgf.models import QualityRule, QualityRun
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import QualityRuleIn, QualityRuleOut, QualityRunIn, QualityRunOut
from edgf.services import audit, issues
from edgf.services.quality import evaluate, latest_run, validate_rule

router = APIRouter(prefix="/quality", tags=["quality"])


@router.post("/assets/{asset_id}/rules", response_model=QualityRuleOut, status_code=201)
def add_rule(asset_id: str, body: QualityRuleIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id, "quality.rule_write")
    try:
        validate_rule(body.rule_type, body.column, body.params)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    rule = QualityRule(asset_id=asset.id, **body.model_dump(mode="json"))
    db.add(rule)
    db.flush()
    audit.record(db, principal.audit_actor, "quality.rule_add", "asset", asset.id, {"rule_id": rule.id, "name": rule.name})
    return rule


@router.get("/assets/{asset_id}/rules", response_model=list[QualityRuleOut])
def list_rules(asset_id: str, request: Request, response: Response, page: Page = Depends(page_params()),
               db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    load_asset(db, principal, asset_id)
    stmt = select(QualityRule).where(QualityRule.asset_id == asset_id).order_by(QualityRule.name, QualityRule.id)
    return paginate(db, stmt, page, request, response)


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    rule = db.get(QualityRule, rule_id)
    if not rule:
        raise HTTPException(404, "rule not found")
    asset = get_asset_or_404(db, rule.asset_id)
    authz.require_read(db, principal, asset)
    authz.require(db, principal, "quality.rule_write", asset=asset)
    audit.record(db, principal.audit_actor, "quality.rule_delete", "asset", rule.asset_id, {"rule_id": rule.id})
    db.delete(rule)


@router.post("/assets/{asset_id}/runs", response_model=QualityRunOut, status_code=201)
def run_quality(asset_id: str, body: QualityRunIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """品質検査を実行し、閾値割れルールは Issue を自動起票する (Continuous Data Quality)。"""
    asset = load_asset(db, principal, asset_id, "quality.run")
    rules = db.scalars(select(QualityRule).where(QualityRule.asset_id == asset.id)).all()
    if not rules:
        raise HTTPException(409, "品質ルールが未定義です")
    result = evaluate(list(rules), body.records)
    run = QualityRun(asset_id=asset.id, record_count=len(body.records), **result)
    db.add(run)
    db.flush()
    opened: list[str] = []
    for r in result["results"]:
        if r["breached"]:
            issue = issues.open_issue(
                db, principal.audit_actor, asset=asset, kind="quality", severity=r["severity"], source_ref=f"rule:{r['rule_id']}",
                title=f"[DQ] {asset.qualified_name}: {r['name']} pass_rate={r['pass_rate']:.4f} < {r['threshold']}",
                description=f"dimension={r['dimension']} failed_samples(index)={r['failed_samples']}")
            if issue:
                opened.append(issue.id)
    audit.record(db, principal.audit_actor, "quality.run", "asset", asset.id, {"run_id": run.id, "score": run.score})
    out = QualityRunOut.model_validate(run)
    out.issues_opened = opened
    return out


@router.get("/assets/{asset_id}/runs", response_model=list[QualityRunOut])
def list_runs(asset_id: str, request: Request, response: Response, page: Page = Depends(page_params(20)),
              db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    load_asset(db, principal, asset_id)
    stmt = select(QualityRun).where(QualityRun.asset_id == asset_id).order_by(QualityRun.executed_at.desc(), QualityRun.id)
    return paginate(db, stmt, page, request, response)


@router.get("/assets/{asset_id}/score")
def current_score(asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    load_asset(db, principal, asset_id)
    run = latest_run(db, asset_id)
    return {"asset_id": asset_id, "score": run.score if run else None,
            "dimension_scores": run.dimension_scores if run else {}, "executed_at": run.executed_at if run else None}
