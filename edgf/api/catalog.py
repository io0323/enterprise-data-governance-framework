"""§6 Metadata / §7 Data Catalog / §4 Ownership / §10 Lifecycle API."""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.enums import LIFECYCLE_TRANSITIONS, Certification, LifecycleState
from edgf.errors import ProblemError
from edgf.models import AccessGrant, DataAsset, Domain, GlossaryTerm, to_naive_utc, utcnow
from edgf.pagination import Page, page_params, paginate, paginate_list
from edgf.schemas import (AccessGrantIn, AccessGrantOut, AssetCreate, AssetOut, AssetUpdate, CertificationChange,
                          DomainIn, DomainOut, LegalHoldChange, LifecycleChange, OwnershipChange)
from edgf.services import ai, ai_inherit, audit, mesh_contracts, outbox, privacy
from edgf.services.certification import certification_blockers

router = APIRouter(tags=["catalog"])

MAX_GRANT_DAYS = 365  # 恒久承認は不可 (§13 Exception と同じ上限)
# 下流の AI 資産の実効値に影響する項目 (§14)
CONTRACT_DRIFT_FIELDS = {"schema_", "sensitivity"}
INHERITANCE_FIELDS = {"sensitivity", "contains_pii", "schema_", "privacy_technique", "privacy_params", "data_class", "ai_kind"}



# ---------------- Domains (§15) ----------------
@router.post("/domains", response_model=DomainOut, status_code=201)
def create_domain(body: DomainIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "domain.create", entity_type="domain", entity_id=body.name)
    if db.get(Domain, body.name):
        raise HTTPException(409, "domain already exists")
    d = Domain(**body.model_dump())
    db.add(d)
    db.flush()
    audit.record(db, principal.audit_actor, "domain.create", "domain", d.name, body.model_dump())
    return d


@router.get("/domains", response_model=list[DomainOut])
def list_domains(request: Request, response: Response, page: Page = Depends(page_params()),
                 db: Session = Depends(get_session)):
    return paginate(db, select(Domain).order_by(Domain.name), page, request, response)


# ---------------- Assets ----------------
_LIKE_ESCAPE = "!"


def _contains(value: str) -> str:
    """LIKE のメタ文字をエスケープした部分一致パターン。"""
    for ch in (_LIKE_ESCAPE, "%", "_"):
        value = value.replace(ch, _LIKE_ESCAPE + ch)
    return f"%{value}%"


def _validate_domain(db: Session, domain: str | None) -> None:
    if domain and not db.get(Domain, domain):
        raise HTTPException(422, f"unknown domain: {domain}")


@router.post("/assets", response_model=AssetOut, status_code=201)
def register_asset(body: AssetCreate, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Discovery/登録。Metadata First: 分類・機密度は必須 (§6.3)。"""
    authz.require(db, principal, "asset.register", domain=body.domain, entity_id=body.qualified_name)
    if db.scalar(select(DataAsset).where(DataAsset.qualified_name == body.qualified_name)):
        raise HTTPException(409, f"already registered: {body.qualified_name}")
    _validate_domain(db, body.domain)
    data = body.model_dump(by_alias=False, mode="json")
    data["contains_pii"] = privacy.effective_contains_pii(data["contains_pii"], data["schema_"], data["privacy_technique"],
                                                          data["privacy_params"])  # §12
    asset = DataAsset(**data)
    db.add(asset)
    db.flush()
    audit.record(db, principal.audit_actor, "asset.register", "asset", asset.id, {"qualified_name": asset.qualified_name})
    ai_inherit.refresh(db, principal.audit_actor, [asset.qualified_name])  # 既存の Lineage の上流として登録された場合も含む
    return asset


@router.get("/assets", response_model=list[AssetOut])
def search_assets(
    request: Request, response: Response,
    q: str | None = Query(None, description="名前/説明/タグ/用語の横断検索"),
    domain: str | None = None, data_class: str | None = None, ai_kind: str | None = None, sensitivity: str | None = None,
    owner: str | None = None, certification: str | None = None, lifecycle_state: str | None = None,
    is_cde: bool | None = None, orphan: bool | None = Query(None, description="Owner未割当 (Unknown Owner) のみ"),
    include_deleted: bool = False, page: Page = Depends(page_params()),
    db: Session = Depends(get_session), principal: Principal = Depends(get_principal),
):
    """閲覧できない資産 (ABAC) は件数を含め一覧から除外する。"""
    stmt = select(DataAsset).where(authz.readable_clause(principal))
    if q:
        like = _contains(q)
        term_assets = select(DataAsset.id).join(DataAsset.terms).where(
            or_(GlossaryTerm.name.ilike(like, escape=_LIKE_ESCAPE), GlossaryTerm.definition.ilike(like, escape=_LIKE_ESCAPE)))
        # tags は JSON 配列。要素の完全一致 (大文字小文字無視) を、保存時と同じ JSON 表現の部分一致で DB 側判定する
        tag_like = _contains(json.dumps(q))
        stmt = stmt.where(or_(DataAsset.name.ilike(like, escape=_LIKE_ESCAPE),
                              DataAsset.qualified_name.ilike(like, escape=_LIKE_ESCAPE),
                              DataAsset.description.ilike(like, escape=_LIKE_ESCAPE),
                              DataAsset.id.in_(term_assets),
                              cast(DataAsset.tags, String).ilike(tag_like, escape=_LIKE_ESCAPE)))
    for col, val in (("domain", domain), ("data_class", data_class), ("ai_kind", ai_kind), ("sensitivity", sensitivity),
                     ("owner", owner), ("certification", certification), ("lifecycle_state", lifecycle_state),
                     ("is_cde", is_cde)):
        if val is not None:
            stmt = stmt.where(getattr(DataAsset, col) == val)
    if orphan is True:
        stmt = stmt.where(or_(DataAsset.owner.is_(None), DataAsset.owner == ""))
    if not include_deleted and lifecycle_state is None:
        stmt = stmt.where(DataAsset.lifecycle_state != LifecycleState.DELETED.value)
    return paginate(db, stmt.order_by(DataAsset.qualified_name), page, request, response)


@router.get("/assets/retention-due", response_model=list[AssetOut])
def retention_due(request: Request, response: Response, page: Page = Depends(page_params()),
                  db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """§10.2 保持期限到達資産 (Legal Hold中は除外)。"""
    now = utcnow()
    assets = db.scalars(select(DataAsset).where(
        DataAsset.retention_days.is_not(None), DataAsset.legal_hold.is_(False),
        DataAsset.lifecycle_state != LifecycleState.DELETED.value, authz.readable_clause(principal))
        .order_by(DataAsset.qualified_name)).all()
    due = [a for a in assets if a.created_at + timedelta(days=a.retention_days) <= now]
    return paginate_list(due, page, request, response)


@router.get("/assets/by-name/{qualified_name}", response_model=AssetOut)
def get_asset_by_name(qualified_name: str, db: Session = Depends(get_session),
                      principal: Principal = Depends(get_principal)):
    a = db.scalar(select(DataAsset).where(DataAsset.qualified_name == qualified_name))
    if not a:
        raise HTTPException(404, "asset not found")
    authz.require_read(db, principal, a)
    return a


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return load_asset(db, principal, asset_id)


@router.patch("/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: str, body: AssetUpdate, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id, "asset.update")
    changes = body.model_dump(exclude_unset=True, by_alias=False, mode="json")
    # NOT NULL / JSON の項目。null を保存すると SQL の NULL ではなく JSON の null になるので「変更なし」として扱う
    for key in ("privacy_technique", "privacy_params", "schema_", "tags", "controls"):
        if changes.get(key, "") is None:
            del changes[key]
    was_anonymized = privacy.is_anonymized(asset.privacy_technique, asset.privacy_params)
    if "domain" in changes:
        _validate_domain(db, changes["domain"])
        if changes["domain"] != asset.domain:  # 移管先ドメインでも権限が必要
            authz.require(db, principal, "asset.update", domain=changes["domain"], entity_id=asset.id)
    if {"ai_kind", "data_class"} & set(changes):
        try:
            ai.check_kind_change(db, asset, changes.get("data_class", asset.data_class), changes.get("ai_kind", asset.ai_kind))
        except ai.AiValidationError as e:
            raise HTTPException(422, str(e)) from e
        except ai.AiStateError as e:
            raise HTTPException(409, str(e)) from e
    for k, v in changes.items():
        setattr(asset, k, v)
    if set(changes) & {"schema_", "contains_pii", "privacy_technique", "privacy_params"}:
        # 基準を満たす匿名化でなくなったら、個人データとして扱いに戻す (§12)
        lost_anonymity = was_anonymized and not privacy.is_anonymized(asset.privacy_technique, asset.privacy_params)
        declared = asset.contains_pii or lost_anonymity
        effective = privacy.effective_contains_pii(declared, asset.schema_, asset.privacy_technique, asset.privacy_params)
        if effective != asset.contains_pii:
            asset.contains_pii = effective
            changes["contains_pii"] = effective
    # 認証済み資産の構造・分類変更は再認証を要求する
    if asset.certification == Certification.CERTIFIED.value and set(changes) & {"schema_", "data_class", "sensitivity", "privacy_technique"}:
        asset.certification = Certification.DRAFT.value
        changes["certification_reset"] = True
    db.flush()
    audit.record(db, principal.audit_actor, "asset.update", "asset", asset.id, changes)
    if set(changes) & INHERITANCE_FIELDS:
        ai_inherit.refresh(db, principal.audit_actor, [asset.qualified_name])
    if set(changes) & CONTRACT_DRIFT_FIELDS:  # Contract とずれても更新は拒否しない。Issue で知らせる (§15)
        mesh_contracts.check_drift(db, principal.audit_actor, asset)
    return asset


@router.put("/assets/{asset_id}/ownership", response_model=AssetOut)
def change_ownership(asset_id: str, body: OwnershipChange, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """§4 Owner/Steward/Custodian の割当。Ownerは単一 (共同Owner禁止)。"""
    asset = load_asset(db, principal, asset_id, "asset.ownership_change")
    before = {"owner": asset.owner, "steward": asset.steward, "custodian": asset.custodian}
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(asset, k, v)
    db.flush()
    audit.record(db, principal.audit_actor, "asset.ownership_change", "asset", asset.id, {"before": before, "after": body.model_dump(exclude_unset=True)})
    return asset


@router.post("/assets/{asset_id}/lifecycle", response_model=AssetOut)
def change_lifecycle(asset_id: str, body: LifecycleChange, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    # 削除指示は Owner または Committee のみ (§10.1 Delete)。それ以外の遷移は資産の更新権限
    action = "asset.delete" if body.target_state == LifecycleState.DELETED else "asset.update"
    asset = load_asset(db, principal, asset_id, action)
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
    audit.record(db, principal.audit_actor, action, "asset", asset.id, {"from": current.value, "to": body.target_state.value, "reason": body.reason})
    if body.target_state == LifecycleState.DELETED:
        outbox.emit(db, "asset.delete_requested", asset.id, {"asset_id": asset.id, "qualified_name": asset.qualified_name})
    return asset


@router.post("/assets/{asset_id}/legal-hold", response_model=AssetOut)
def set_legal_hold(asset_id: str, body: LegalHoldChange, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id, "asset.legal_hold")
    asset.legal_hold = body.legal_hold
    db.flush()
    audit.record(db, principal.audit_actor, "asset.legal_hold", "asset", asset.id, body.model_dump())
    return asset


@router.post("/assets/{asset_id}/refreshed", response_model=AssetOut)
def mark_refreshed(asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Data Plane からの更新通知 (Operational Metadata / Freshness)。"""
    asset = load_asset(db, principal, asset_id, "asset.refresh_notify")
    asset.last_refreshed_at = utcnow()
    db.flush()
    audit.record(db, principal.audit_actor, "asset.refreshed", "asset", asset.id,
                 {"last_refreshed_at": asset.last_refreshed_at})
    return asset


@router.get("/assets/{asset_id}/certification/check")
def check_certification(asset_id: str, db: Session = Depends(get_session),
                        principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id)
    blockers = certification_blockers(db, asset)
    return {"asset_id": asset_id, "certifiable": not blockers, "blockers": blockers}


@router.post("/assets/{asset_id}/certification", response_model=AssetOut)
def change_certification(asset_id: str, body: CertificationChange, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    # Certification はその資産の Owner / Steward のみ (§7)
    asset = load_asset(db, principal, asset_id, "asset.certify")
    if body.certification == Certification.CERTIFIED:
        blockers = certification_blockers(db, asset)
        if blockers:
            raise ProblemError(409, "Certification の前提条件を満たしていません", type_="certification-blocked",
                               blockers=blockers)
    asset.certification = body.certification.value
    db.flush()
    audit.record(db, principal.audit_actor, "asset.certification", "asset", asset.id, body.model_dump(mode="json"))
    return asset


# ---------------- Access Grants (§4.2 アクセス承認 / §11) ----------------
@router.post("/assets/{asset_id}/access-grants", response_model=AccessGrantOut, status_code=201)
def grant_access(asset_id: str, body: AccessGrantIn, db: Session = Depends(get_session),
                 principal: Principal = Depends(get_principal)):
    """Owner が利用者に資産の閲覧を承認する (restricted 資産の閲覧に必須)。"""
    asset = load_asset(db, principal, asset_id, "asset.access_grant")
    expires = to_naive_utc(body.expires_at)
    now = utcnow()
    if expires <= now or expires > now + timedelta(days=MAX_GRANT_DAYS):
        raise HTTPException(422, f"expires_at は現在から{MAX_GRANT_DAYS}日以内の未来である必要があります (恒久承認は不可)")
    g = AccessGrant(asset_id=asset.id, subject=body.subject, purpose=body.purpose,
                    granted_by=principal.subject, expires_at=expires)
    db.add(g)
    db.flush()
    audit.record(db, principal.audit_actor, "access.grant", "asset", asset.id,
                 {"grant_id": g.id, "subject": g.subject, "purpose": g.purpose, "expires_at": g.expires_at})
    outbox.emit(db, "access.granted", g.id, {"grant_id": g.id, "asset_id": asset.id, "subject": g.subject, "expires_at": g.expires_at})
    return g


@router.get("/assets/{asset_id}/access-grants", response_model=list[AccessGrantOut])
def list_access_grants(asset_id: str, request: Request, response: Response, include_inactive: bool = False,
                       page: Page = Depends(page_params()), db: Session = Depends(get_session),
                       principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id, "asset.access_grant.read")
    stmt = select(AccessGrant).where(AccessGrant.asset_id == asset.id)
    if not include_inactive:
        stmt = stmt.where(AccessGrant.revoked_at.is_(None), AccessGrant.expires_at > utcnow())
    return paginate(db, stmt.order_by(AccessGrant.created_at.desc(), AccessGrant.id), page, request, response)


@router.delete("/assets/{asset_id}/access-grants/{grant_id}", response_model=AccessGrantOut)
def revoke_access(asset_id: str, grant_id: str, db: Session = Depends(get_session),
                  principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id, "asset.access_revoke")
    g = db.get(AccessGrant, grant_id)
    if g is None or g.asset_id != asset.id:
        raise HTTPException(404, "access grant not found")
    if g.revoked_at is None:
        g.revoked_at, g.revoked_by = utcnow(), principal.subject
        db.flush()
        audit.record(db, principal.audit_actor, "access.revoke", "asset", asset.id, {"grant_id": g.id, "subject": g.subject})
        outbox.emit(db, "access.revoked", g.id, {"grant_id": g.id, "asset_id": asset.id, "subject": g.subject})
    return g
