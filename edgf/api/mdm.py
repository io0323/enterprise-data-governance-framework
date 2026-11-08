"""§8 Master Data Management API.

Golden Record の値は Data Plane (MDM Hub) の責務で、Control Plane には永続化しない (§18.2)。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.errors import ProblemError
from edgf.models import DataAsset, MdmRuleApproval
from edgf.pagination import Page, page_params, paginate_list
from edgf.services import audit, mdm_rules
from edgf.services.mdm_rules import Entity, MdmRuleError

router = APIRouter(prefix="/mdm", tags=["mdm"])


def _entity(entity_type: str) -> Entity:
    entity = mdm_rules.get_entities().get(entity_type)
    if entity is None:
        raise HTTPException(404, f"unknown entity type: {entity_type}")
    return entity


def _master_asset(db: Session, entity: Entity) -> DataAsset:
    """Metadata First (§6.3): 対応する master 資産が Catalog に無ければ MDM の操作を受け付けない。"""
    asset = mdm_rules.find_asset(db, entity)
    if asset is None:
        raise ProblemError(409, f"エンティティ {entity.entity_type} に対応する master 資産 {entity.asset} が Catalog にありません",
                           type_="mdm-asset-missing")
    return asset


def _entity_view(db: Session, entity: Entity, asset: DataAsset | None) -> dict:
    approval = mdm_rules.approval_of(db, entity)
    return {**entity.as_dict(), "asset_id": asset.id if asset else None, "asset_registered": asset is not None,
            "approved_by": approval.approved_by if approval else None,
            "approved_at": approval.approved_at if approval else None}


# ---------------- エンティティ定義 (9a) ----------------
@router.get("/entities")
def list_entities(request: Request, response: Response, page: Page = Depends(page_params()),
                  db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """エンティティ定義の一覧 (生成ロジックの公開: §8.1)。閲覧できない資産に対応するものは除外する。"""
    authz.require(db, principal, "mdm.read", entity_type="mdm", entity_id="*")
    views = []
    for entity in mdm_rules.get_entities().values():
        asset = mdm_rules.find_asset(db, entity)
        if asset is None or authz.can_read(db, principal, asset):
            views.append(_entity_view(db, entity, asset))
    return paginate_list(views, page, request, response)


@router.post("/entities/reload")
def reload_entities(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "mdm.rule.write", entity_type="mdm", entity_id="*")
    try:
        entities = mdm_rules.get_entities(reload=True)
    except MdmRuleError as e:
        raise HTTPException(422, str(e)) from e
    audit.record(db, principal.audit_actor, "mdm.rule_reload", "mdm", "*",
                 {"rules": {e.entity_type: e.rule_hash for e in entities.values()}})
    return {"loaded": len(entities)}


@router.get("/entities/{entity_type}")
def get_entity(entity_type: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    entity = _entity(entity_type)
    asset = mdm_rules.find_asset(db, entity)
    if asset is not None:
        authz.require_read(db, principal, asset)
    return _entity_view(db, entity, asset)


@router.post("/entities/{entity_type}/approve")
def approve_entity_rules(entity_type: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Golden Record の生成規則を対象資産の Owner が承認する (§8.2)。ルールを変更すると再承認が必要になる。"""
    entity = _entity(entity_type)
    asset = load_asset(db, principal, _master_asset(db, entity).id, "mdm.rule.approve")
    approval = mdm_rules.approval_of(db, entity)
    if approval is None:
        approval = MdmRuleApproval(entity_type=entity.entity_type, rule_hash=entity.rule_hash,
                                   approved_by=principal.subject)
        db.add(approval)
        db.flush()
        audit.record(db, principal.audit_actor, "mdm.rule_approve", "asset", asset.id,
                     {"entity_type": entity.entity_type, "rule_hash": entity.rule_hash})
    return _entity_view(db, entity, asset)
