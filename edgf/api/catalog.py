"""§6 Metadata / §7 Data Catalog / §4 Ownership / §10 Lifecycle API."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from edgf.db import get_session
from edgf.deps import get_actor, get_asset_or_404
from edgf.enums import LIFECYCLE_TRANSITIONS, Certification, LifecycleState
from edgf.models import DataAsset, Domain, GlossaryTerm, utcnow
from edgf.schemas import (AssetCreate, AssetOut, AssetUpdate, CertificationChange, DomainIn, DomainOut,
                          LegalHoldChange, LifecycleChange, OwnershipChange)
from edgf.services import audit
from edgf.services.certification import certification_blockers

router = APIRouter(tags=["catalog"])


# ---------------- Domains (§15) ----------------
@router.post("/domains", response_model=DomainOut, status_code=201)
def create_domain(body: DomainIn, db: Session = Depends(get_session), actor: str = Depends(get_actor)):
    if db.get(Domain, body.name):
        raise HTTPException(409, "domain already exists")
    d = Domain(**body.model_dump())
    db.add(d)
    db.flush()
    audit.record(db, actor, "domain.create", "domain", d.name, body.model_dump())
    return d


@router.get("/domains", response_model=list[DomainOut])
def list_domains(db: Session = Depends(get_session)):
    return db.scalars(select(Domain).order_by(Domain.name)).all()


# ---------------- Assets ----------------
def _validate_domain(db: Session, domain: str | None) -> None:
    if domain and not db.get(Domain, domain):
        raise HTTPException(422, f"unknown domain: {domain}")


@router.post("/assets", response_model=AssetOut, status_code=201)
def register_asset(body: AssetCreate, db: Session = Depends(get_session), actor: str = Depends(get_actor)):
    """Discovery/登録。Metadata First: 分類・機密度は必須 (§6.3)。"""
    if db.scalar(select(DataAsset).where(DataAsset.qualified_name == body.qualified_name)):
        raise HTTPException(409, f"already registered: {body.qualified_name}")
    _validate_domain(db, body.domain)
    data = body.model_dump(by_alias=False, mode="json")
    if body.contains_pii is False and any(c.pii for c in body.schema_):
        data["contains_pii"] = True  # カラムPIIフラグから自動推定 (§12 PII Inventory)
    asset = DataAsset(**data)
    db.add(asset)
    db.flush()
    audit.record(db, actor, "asset.register", "asset", asset.id, {"qualified_name": asset.qualified_name})
    return asset


@router.get("/assets", response_model=list[AssetOut])
def search_assets(
    q: str | None = Query(None, description="名前/説明/タグ/用語の横断検索"),
    domain: str | None = None, data_class: str | None = None, sensitivity: str | None = None,
    owner: str | None = None, certification: str | None = None, lifecycle_state: str | None = None,
    is_cde: bool | None = None, orphan: bool | None = Query(None, description="Owner未割当 (Unknown Owner) のみ"),
    include_deleted: bool = False, limit: int = Query(100, le=1000), offset: int = 0,
    db: Session = Depends(get_session),
):
    stmt = select(DataAsset)
    if q:
        like = f"%{q}%"
        term_assets = select(DataAsset.id).join(DataAsset.terms).where(
            or_(GlossaryTerm.name.ilike(like), GlossaryTerm.definition.ilike(like)))
        stmt = stmt.where(or_(DataAsset.name.ilike(like), DataAsset.qualified_name.ilike(like),
                              DataAsset.description.ilike(like), DataAsset.id.in_(term_assets)))
    for col, val in (("domain", domain), ("data_class", data_class), ("sensitivity", sensitivity),
                     ("owner", owner), ("certification", certification), ("lifecycle_state", lifecycle_state),
                     ("is_cde", is_cde)):
        if val is not None:
            stmt = stmt.where(getattr(DataAsset, col) == val)
    if orphan is True:
        stmt = stmt.where(or_(DataAsset.owner.is_(None), DataAsset.owner == ""))
    if not include_deleted and lifecycle_state is None:
        stmt = stmt.where(DataAsset.lifecycle_state != LifecycleState.DELETED.value)
    results = db.scalars(stmt.order_by(DataAsset.qualified_name).offset(offset).limit(limit)).all()
    if q:  # tags は JSON のため DB 非依存にアプリ側で補完
        tag_hits = [a for a in db.scalars(select(DataAsset)).all()
                    if q.lower() in [t.lower() for t in (a.tags or [])] and a not in results]
        results = list(results) + tag_hits
    return results


@router.get("/assets/retention-due", response_model=list[AssetOut])
def retention_due(db: Session = Depends(get_session)):
    """§10.2 保持期限到達資産 (Legal Hold中は除外)。"""
    now = utcnow()
    assets = db.scalars(select(DataAsset).where(
        DataAsset.retention_days.is_not(None), DataAsset.legal_hold.is_(False),
        DataAsset.lifecycle_state != LifecycleState.DELETED.value)).all()
    return [a for a in assets if a.created_at + timedelta(days=a.retention_days) <= now]


@router.get("/assets/by-name/{qualified_name}", response_model=AssetOut)
def get_asset_by_name(qualified_name: str, db: Session = Depends(get_session)):
    a = db.scalar(select(DataAsset).where(DataAsset.qualified_name == qualified_name))
    if not a:
        raise HTTPException(404, "asset not found")
    return a


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: str, db: Session = Depends(get_session)):
    return get_asset_or_404(db, asset_id)


@router.patch("/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: str, body: AssetUpdate, db: Session = Depends(get_session), actor: str = Depends(get_actor)):
    asset = get_asset_or_404(db, asset_id)
    changes = body.model_dump(exclude_unset=True, by_alias=False, mode="json")
    if "domain" in changes:
        _validate_domain(db, changes["domain"])
    for k, v in changes.items():
        setattr(asset, k, v)
    # 認証済み資産の構造・分類変更は再認証を要求する
    if asset.certification == Certification.CERTIFIED.value and set(changes) & {"schema_", "data_class", "sensitivity"}:
        asset.certification = Certification.DRAFT.value
        changes["certification_reset"] = True
    db.flush()
    audit.record(db, actor, "asset.update", "asset", asset.id, changes)
    return asset


@router.put("/assets/{asset_id}/ownership", response_model=AssetOut)
def change_ownership(asset_id: str, body: OwnershipChange, db: Session = Depends(get_session), actor: str = Depends(get_actor)):
    """§4 Owner/Steward/Custodian の割当。Ownerは単一 (共同Owner禁止)。"""
    asset = get_asset_or_404(db, asset_id)
    before = {"owner": asset.owner, "steward": asset.steward, "custodian": asset.custodian}
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(asset, k, v)
    db.flush()
    audit.record(db, actor, "asset.ownership_change", "asset", asset.id, {"before": before, "after": body.model_dump(exclude_unset=True)})
    return asset


@router.post("/assets/{asset_id}/lifecycle", response_model=AssetOut)
def change_lifecycle(asset_id: str, body: LifecycleChange, db: Session = Depends(get_session), actor: str = Depends(get_actor)):
    asset = get_asset_or_404(db, asset_id)
    current = LifecycleState(asset.lifecycle_state)
    if body.target_state not in LIFECYCLE_TRANSITIONS[current]:
        raise HTTPException(409, f"invalid transition {current.value} -> {body.target_state.value}")
    if body.target_state == LifecycleState.DELETED and asset.legal_hold:
        raise HTTPException(409, "legal hold中のため削除できません (§10.2)")
    asset.lifecycle_state = body.target_state.value
    if body.target_state == LifecycleState.ARCHIVED:
        asset.archived_at = utcnow()
    if body.target_state in (LifecycleState.DEPRECATED, LifecycleState.ARCHIVED, LifecycleState.DELETED):
        if asset.certification == Certification.CERTIFIED.value:
            asset.certification = Certification.DEPRECATED.value
    db.flush()
    # 物理削除は Data Plane が実行する。Control Plane は削除指示と証跡を記録する。
    action = "lifecycle.delete_requested" if body.target_state == LifecycleState.DELETED else "lifecycle.change"
    audit.record(db, actor, action, "asset", asset.id, {"from": current.value, "to": body.target_state.value, "reason": body.reason})
    return asset


@router.post("/assets/{asset_id}/legal-hold", response_model=AssetOut)
def set_legal_hold(asset_id: str, body: LegalHoldChange, db: Session = Depends(get_session), actor: str = Depends(get_actor)):
    asset = get_asset_or_404(db, asset_id)
    asset.legal_hold = body.legal_hold
    db.flush()
    audit.record(db, actor, "asset.legal_hold", "asset", asset.id, body.model_dump())
    return asset


@router.post("/assets/{asset_id}/refreshed", response_model=AssetOut)
def mark_refreshed(asset_id: str, db: Session = Depends(get_session), actor: str = Depends(get_actor)):
    """Data Plane からの更新通知 (Operational Metadata / Freshness)。"""
    asset = get_asset_or_404(db, asset_id)
    asset.last_refreshed_at = utcnow()
    db.flush()
    return asset


@router.get("/assets/{asset_id}/certification/check")
def check_certification(asset_id: str, db: Session = Depends(get_session)):
    asset = get_asset_or_404(db, asset_id)
    blockers = certification_blockers(db, asset)
    return {"asset_id": asset_id, "certifiable": not blockers, "blockers": blockers}


@router.post("/assets/{asset_id}/certification", response_model=AssetOut)
def change_certification(asset_id: str, body: CertificationChange, db: Session = Depends(get_session), actor: str = Depends(get_actor)):
    asset = get_asset_or_404(db, asset_id)
    if body.certification == Certification.CERTIFIED:
        if actor not in (asset.owner, asset.steward):
            raise HTTPException(403, "CertificationはOwnerまたはStewardのみ実行できます")
        blockers = certification_blockers(db, asset)
        if blockers:
            raise HTTPException(409, {"message": "certification blocked", "blockers": blockers})
    asset.certification = body.certification.value
    db.flush()
    audit.record(db, actor, "asset.certification", "asset", asset.id, body.model_dump(mode="json"))
    return asset
