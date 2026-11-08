"""§14 Data Governance & AI API."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import COMMITTEE, Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.models import DatasetCard, DatasetVersion, ModelCard, ModelVersion, TrainingApproval
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import (DatasetCardIn, DatasetCardOut, DatasetSourceOut, DatasetVersionIn, DatasetVersionOut, ModelCardIn,
                          ModelCardOut, ModelSuspendIn, ModelVersionIn, ModelVersionOut, TrainingApprovalIn, TrainingApprovalOut,
                          TrainingDecisionIn)
from edgf.services import ai, ai_training, audit
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


def _model_transition(db: Session, principal: Principal, version_id: str, permission: str, event: str, fn, *args: Any):
    version = _load_model_version(db, principal, version_id, permission)
    _guard(fn, db, version, *args)
    audit.record(db, principal.audit_actor, event, "model_version", version.id,
                 {"asset_id": version.asset_id, "version": version.version, "status": version.status})
    return _model_out(version)


@router.post("/model-versions/{version_id}/approve", response_model=ModelVersionOut)
def approve_model_version(version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Owner の承認。学習に使った Dataset Version がすべて approved でなければ 409。"""
    return _model_transition(db, principal, version_id, "ai.model.approve", "ai.model.approve",
                             ai_training.approve_model, principal.subject)


@router.post("/model-versions/{version_id}/deploy", response_model=ModelVersionOut)
def deploy_model_version(version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Data Plane からのデプロイの記録。承認済みの版だけを記録できる。"""
    return _model_transition(db, principal, version_id, "ai.model_version.register", "ai.model.deploy", ai_training.deploy_model)


@router.post("/model-versions/{version_id}/suspend", response_model=ModelVersionOut)
def suspend_model_version(version_id: str, body: ModelSuspendIn, db: Session = Depends(get_session),
                          principal: Principal = Depends(get_principal)):
    return _model_transition(db, principal, version_id, "ai.model.suspend", "ai.model.suspend",
                             ai_training.suspend_model, body.reason)


@router.post("/model-versions/{version_id}/resume", response_model=ModelVersionOut)
def resume_model_version(version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """再開は承認と同じ権限と条件。"""
    return _model_transition(db, principal, version_id, "ai.model.approve", "ai.model.resume", ai_training.resume_model)


@router.post("/model-versions/{version_id}/retire", response_model=ModelVersionOut)
def retire_model_version(version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _model_transition(db, principal, version_id, "ai.model.suspend", "ai.model.retire", ai_training.retire_model)


# ---------------- 学習利用の承認 (§14 Training Data / §13 Approval) ----------------
def _load_approval(db: Session, approval_id: str) -> tuple[TrainingApproval, DatasetVersion]:
    approval = db.get(TrainingApproval, approval_id)
    if approval is None:
        raise HTTPException(404, f"training approval not found: {approval_id}")
    return approval, db.get(DatasetVersion, approval.dataset_version_id)


@router.post("/dataset-versions/{version_id}/training-approvals", response_model=TrainingApprovalOut, status_code=201)
def request_training_approval(version_id: str, body: TrainingApprovalIn, db: Session = Depends(get_session),
                              principal: Principal = Depends(get_principal)):
    """申請する。同意の集計と品質スコアは Control Plane が計算して添付する。前提を満たさなければ 409。"""
    version = _load_dataset_version(db, principal, version_id, "ai.training.request")
    approval = _guard(ai_training.request, db, version, license_status=body.license_status, license_ref=body.license_ref,
                      bias_evaluation_ref=body.bias_evaluation_ref, requested_by=principal.subject)
    audit.record(db, principal.audit_actor, "ai.training.request", "training_approval", approval.id, {
        "dataset_version_id": version.id, "license_status": approval.license_status,
        "slots": [s.slot for s in approval.decisions], "consent_ok": approval.consent_summary["ok"]})
    return approval


@router.get("/dataset-versions/{version_id}/training-approvals", response_model=list[TrainingApprovalOut])
def list_training_approvals(version_id: str, request: Request, response: Response, page: Page = Depends(page_params()),
                            db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    _load_dataset_version(db, principal, version_id)
    stmt = select(TrainingApproval).where(TrainingApproval.dataset_version_id == version_id) \
        .order_by(TrainingApproval.requested_at, TrainingApproval.id)
    return paginate(db, stmt, page, request, response)


@router.get("/training-approvals/{approval_id}", response_model=TrainingApprovalOut)
def get_training_approval(approval_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    approval, version = _load_approval(db, approval_id)
    load_asset(db, principal, version.asset_id)
    return approval


@router.post("/training-approvals/{approval_id}/decision", response_model=TrainingApprovalOut)
def decide_training_approval(approval_id: str, body: TrainingDecisionIn, db: Session = Depends(get_session),
                             principal: Principal = Depends(get_principal)):
    """決裁する。Owner は自分の枠、Committee は Committee の枠だけ。

    Owner の枠を持つ人は Owner として決裁する。Committee の枠は、申請者と Owner の枠の決裁者には開かない (職務分離)。
    """
    approval, version = _load_approval(db, approval_id)
    slot = ai_training.owner_slot_for(db, approval, principal.subject)
    authz.require(db, principal, "ai.training.approve", relations=("owner",) if slot else (),
                  entity_type="training_approval", entity_id=approval.id)
    if approval.status != "pending":
        raise HTTPException(409, f"状態が {approval.status} の申請は決裁できません")
    if slot is None and principal.has_role(COMMITTEE):
        slot = ai_training.committee_slot(approval)
        decided_as_owner = {s.decided_by for s in approval.decisions if s.required_subject}
        if slot and (principal.subject == approval.requested_by or principal.subject in decided_as_owner):
            authz.deny(db, principal, "ai.training.approve", entity_type="training_approval", entity_id=approval.id,
                       reason="申請者と Owner として決裁した本人は Committee として決裁できません (職務分離)")
    if slot is None:
        raise HTTPException(409, "あなたが決裁できる未決の枠はありません")
    _guard(ai_training.decide, db, approval, slot, approve=body.approve, comment=body.comment, decided_by=principal.subject)
    audit.record(db, principal.audit_actor, "ai.training.decision", "training_approval", approval.id, {
        "dataset_version_id": version.id, "slot": slot.slot, "approve": body.approve, "comment": body.comment,
        "status": approval.status, "dataset_version_status": version.status})
    return approval


@router.post("/training-approvals/{approval_id}/withdraw", response_model=TrainingApprovalOut)
def withdraw_training_approval(approval_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """申請後に Owner が交代した場合などに取り下げて、申請し直す。"""
    approval, version = _load_approval(db, approval_id)
    load_asset(db, principal, version.asset_id, "ai.training.request")
    _guard(ai_training.withdraw, db, approval)
    audit.record(db, principal.audit_actor, "ai.training.withdraw", "training_approval", approval.id,
                 {"dataset_version_id": version.id})
    return approval
