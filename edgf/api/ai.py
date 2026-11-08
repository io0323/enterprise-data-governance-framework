"""§14 Data Governance & AI API."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.models import DatasetCard, DatasetVersion, ModelCard, ModelVersion
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import (DatasetCardIn, DatasetCardOut, DatasetSourceOut, DatasetVersionIn, DatasetVersionOut, ModelCardIn,
                          ModelCardOut, ModelVersionIn, ModelVersionOut)
from edgf.services import ai, audit
from edgf.services.ai import AiStateError, AiValidationError

router = APIRouter(prefix="/ai", tags=["ai"])
T = TypeVar("T")


def _guard(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return fn(*args, **kwargs)
    except AiValidationError as e:
        raise HTTPException(422, str(e)) from e
    except AiStateError as e:
        raise HTTPException(409, str(e)) from e


def _dataset_out(db: Session, version: DatasetVersion) -> DatasetVersionOut:
    out = DatasetVersionOut.model_validate(version)
    out.sources = [DatasetSourceOut.model_validate(s) for s in ai.sources_of(db, version)]
    return out


def _model_out(version: ModelVersion) -> ModelVersionOut:
    out = ModelVersionOut.model_validate(version)
    out.dataset_version_ids = sorted(d.id for d in version.datasets)
    return out


def _load_dataset_version(db: Session, principal: Principal, version_id: str, action: str | None = None) -> DatasetVersion:
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise HTTPException(404, f"dataset version not found: {version_id}")
    load_asset(db, principal, version.asset_id, action)
    return version


def _load_model_version(db: Session, principal: Principal, version_id: str, action: str | None = None) -> ModelVersion:
    version = db.get(ModelVersion, version_id)
    if version is None:
        raise HTTPException(404, f"model version not found: {version_id}")
    load_asset(db, principal, version.asset_id, action)
    return version


# ---------------- Card (§14 AI Metadata) ----------------
@router.put("/assets/{asset_id}/dataset-card", response_model=DatasetCardOut)
def put_dataset_card(asset_id: str, body: DatasetCardIn, db: Session = Depends(get_session),
                     principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id, "ai.card.write")
    card = _guard(ai.put_dataset_card, db, asset, body.model_dump(), principal.subject)
    # 本文は監査に残さない (変更の有無はハッシュチェーンで追える項目名だけを記録する)
    audit.record(db, principal.audit_actor, "ai.card.write", "asset", asset.id,
                 {"card": "dataset", "consent_purpose_id": card.consent_purpose_id, "fields": sorted(body.model_fields_set)})
    return card


@router.get("/assets/{asset_id}/dataset-card", response_model=DatasetCardOut)
def get_dataset_card(asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    load_asset(db, principal, asset_id)
    card = db.get(DatasetCard, asset_id)
    if card is None:
        raise HTTPException(404, f"dataset card not found: {asset_id}")
    return card


@router.put("/assets/{asset_id}/model-card", response_model=ModelCardOut)
def put_model_card(asset_id: str, body: ModelCardIn, db: Session = Depends(get_session),
                   principal: Principal = Depends(get_principal)):
    asset = load_asset(db, principal, asset_id, "ai.card.write")
    card = _guard(ai.put_model_card, db, asset, body.model_dump(), principal.subject)
    audit.record(db, principal.audit_actor, "ai.card.write", "asset", asset.id,
                 {"card": "model", "fields": sorted(body.model_fields_set)})
    return card


@router.get("/assets/{asset_id}/model-card", response_model=ModelCardOut)
def get_model_card(asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    load_asset(db, principal, asset_id)
    card = db.get(ModelCard, asset_id)
    if card is None:
        raise HTTPException(404, f"model card not found: {asset_id}")
    return card


# ---------------- Dataset Version (§14 Training Data) ----------------
@router.post("/assets/{asset_id}/dataset-versions", response_model=DatasetVersionOut, status_code=201)
def register_dataset_version(asset_id: str, body: DatasetVersionIn, db: Session = Depends(get_session),
                             principal: Principal = Depends(get_principal)):
    """版の情報を登録する。実データは受け取らず、Data Plane が計算した manifest のハッシュと件数だけを記録する。"""
    asset = load_asset(db, principal, asset_id, "ai.dataset_version.register")
    version = _guard(ai.register_dataset_version, db, asset, body.model_dump(exclude={"sources"}),
                     [s.model_dump() for s in body.sources], principal.subject)
    audit.record(db, principal.audit_actor, "ai.dataset_version.register", "dataset_version", version.id, {
        "asset_id": asset.id, "version": version.version, "manifest_hash": version.manifest_hash,
        "record_count": version.record_count, "sources": [s.qualified_name for s in ai.sources_of(db, version)]})
    return _dataset_out(db, version)


@router.get("/assets/{asset_id}/dataset-versions", response_model=list[DatasetVersionOut])
def list_dataset_versions(asset_id: str, request: Request, response: Response, page: Page = Depends(page_params()),
                          db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    load_asset(db, principal, asset_id)
    stmt = select(DatasetVersion).where(DatasetVersion.asset_id == asset_id).order_by(DatasetVersion.created_at, DatasetVersion.id)
    return [_dataset_out(db, v) for v in paginate(db, stmt, page, request, response)]


@router.get("/dataset-versions/{version_id}", response_model=DatasetVersionOut)
def get_dataset_version(version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _dataset_out(db, _load_dataset_version(db, principal, version_id))


# ---------------- Model Version (§14 AI Metadata) ----------------
@router.post("/assets/{asset_id}/model-versions", response_model=ModelVersionOut, status_code=201)
def register_model_version(asset_id: str, body: ModelVersionIn, db: Session = Depends(get_session),
                           principal: Principal = Depends(get_principal)):
    """版の情報を登録する。重みは Data Plane のレジストリが持ち、ここには外部参照の URI だけを記録する。"""
    asset = load_asset(db, principal, asset_id, "ai.model_version.register")
    version = _guard(ai.register_model_version, db, asset, version=body.version, external_uri=body.external_uri,
                     dataset_version_ids=body.dataset_version_ids, actor=principal.subject)
    audit.record(db, principal.audit_actor, "ai.model_version.register", "model_version", version.id, {
        "asset_id": asset.id, "version": version.version, "dataset_version_ids": sorted(d.id for d in version.datasets)})
    return _model_out(version)


@router.get("/assets/{asset_id}/model-versions", response_model=list[ModelVersionOut])
def list_model_versions(asset_id: str, request: Request, response: Response, page: Page = Depends(page_params()),
                        db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    load_asset(db, principal, asset_id)
    stmt = select(ModelVersion).where(ModelVersion.asset_id == asset_id).order_by(ModelVersion.created_at, ModelVersion.id)
    return [_model_out(v) for v in paginate(db, stmt, page, request, response)]


@router.get("/model-versions/{version_id}", response_model=ModelVersionOut)
def get_model_version(version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _model_out(_load_model_version(db, principal, version_id))
