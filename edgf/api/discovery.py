"""§7 Discovery: Data Plane からの資産一覧の受け取りと、差分の参照、破壊的なスキーマの変化の反映."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.errors import ProblemError
from edgf.models import DataAsset, DiscoveredAsset, DiscoverySource, SchemaChangeProposal
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import DiscoveredAssetOut, DiscoverySnapshotIn, SchemaChangeOut
from edgf.services import discovery
from edgf.services.discovery_rules import rule_for

router = APIRouter(prefix="/discovery", tags=["discovery"])


@router.post("/snapshots", status_code=201)
def push_snapshot(body: DiscoverySnapshotIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """ソースの全量の資産一覧を受け取り、前回との差分を返す。ソースのドメインのドメイン内 custodian と admin が送れる。"""
    rule = rule_for(body.source)
    if rule is None:
        raise ProblemError(422, f"ソース {body.source} は policies/discovery の対応表にありません", type_="discovery-source-unknown")
    authz.require(db, principal, "discovery.push", domain=rule.domain, entity_type="discovery_source", entity_id=body.source)
    assets = [a.model_dump(by_alias=False) | {"schema": [c.model_dump() for c in a.schema_]} for a in body.assets]
    return discovery.ingest(db, principal.audit_actor, rule, body.source, assets)


def _scope(db: Session, principal: Principal, action: str = "discovery.read"):
    return authz.require_scope(db, principal, action, DiscoverySource.domain)


@router.get("/sources")
def list_sources(request: Request, response: Response, page: Page = Depends(page_params()), db: Session = Depends(get_session),
                 principal: Principal = Depends(get_principal)):
    stmt = select(DiscoverySource).where(_scope(db, principal)).order_by(DiscoverySource.namespace)
    sources = paginate(db, stmt, page, request, response)
    counts = discovery.source_summary(db, [s.namespace for s in sources])
    return [{"namespace": s.namespace, "domain": s.domain, "steward": s.steward, "last_snapshot_at": s.last_snapshot_at,
             "present_count": counts[s.namespace]["present"], "missing_count": counts[s.namespace]["missing"]} for s in sources]


@router.get("/assets", response_model=list[DiscoveredAssetOut])
def list_discovered(request: Request, response: Response, namespace: str | None = None, status: str | None = None, registered: bool | None = None,
                    page: Page = Depends(page_params()), db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """検出された資産。registered=false で未登録の資産だけを返す。"""
    stmt = select(DiscoveredAsset).join(DiscoverySource, DiscoverySource.namespace == DiscoveredAsset.namespace).where(_scope(db, principal))
    if namespace:
        stmt = stmt.where(DiscoveredAsset.namespace == namespace)
    if status:
        stmt = stmt.where(DiscoveredAsset.status == status)
    catalog = select(DataAsset.qualified_name).where(DataAsset.lifecycle_state != "deleted")
    if registered is True:
        stmt = stmt.where(DiscoveredAsset.qualified_name.in_(catalog))
    elif registered is False:
        stmt = stmt.where(DiscoveredAsset.qualified_name.not_in(catalog))
    rows = paginate(db, stmt.order_by(DiscoveredAsset.namespace, DiscoveredAsset.qualified_name), page, request, response)
    known = set(db.scalars(catalog.where(DataAsset.qualified_name.in_([r.qualified_name for r in rows])))) if rows else set()
    return [DiscoveredAssetOut.model_validate(r).model_copy(update={"registered": r.qualified_name in known}) for r in rows]


@router.get("/schema-changes", response_model=list[SchemaChangeOut])
def list_schema_changes(request: Request, response: Response, status: str | None = "pending", page: Page = Depends(page_params()),
                        db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """保留している破壊的なスキーマの変化。閲覧できる資産のものだけを返す。"""
    stmt = select(SchemaChangeProposal).join(DataAsset, DataAsset.id == SchemaChangeProposal.asset_id) \
        .where(authz.readable_clause(principal), authz.require_scope(db, principal, "discovery.read"))
    if status:
        stmt = stmt.where(SchemaChangeProposal.status == status)
    return paginate(db, stmt.order_by(SchemaChangeProposal.created_at), page, request, response)


@router.post("/schema-changes/{proposal_id}/apply", response_model=SchemaChangeOut)
def apply_schema_change(proposal_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """破壊的な変化を確認して Catalog に反映する (対象資産の Steward / Owner)。"""
    proposal = db.get(SchemaChangeProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, f"schema change not found: {proposal_id}")
    asset = load_asset(db, principal, proposal.asset_id, "discovery.apply_breaking")
    if proposal.status != "pending":
        raise HTTPException(409, f"この変化は {proposal.status} です")
    discovery.apply_proposal(db, principal.audit_actor, proposal, asset)
    return proposal
