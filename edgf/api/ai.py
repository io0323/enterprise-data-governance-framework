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
from edgf.models import (AiIncident, DataAsset, DatasetCard, DatasetVersion, ModelCard, ModelRemediation, ModelUsage,
                         ModelVersion, TrainingApproval)
from edgf.pagination import Page, page_params, paginate, paginate_list
from edgf.schemas import (DatasetCardIn, IncidentIn, IncidentOut, InheritanceOut, RemediationCompleteIn, RemediationDecisionIn,
                          RemediationOut, DatasetCardOut, DatasetSourceOut, DatasetVersionIn, DatasetVersionOut, ModelCardIn,
                          ModelCardOut, ModelSuspendIn, ModelUsageIn, ModelUsageOut, ModelVersionIn, ModelVersionOut, RagAuthorizeIn, TrainingApprovalIn, TrainingApprovalOut,
                          TrainingDecisionIn)
from edgf.services import ai, ai_inherit, ai_lineage, ai_rag, ai_remediation, ai_training, audit
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
    ai_inherit.refresh(db, principal.audit_actor, [asset.qualified_name])
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
    ai_inherit.refresh(db, principal.audit_actor, [asset.qualified_name])
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


# ---------------- AI Lineage (§14) ----------------
@router.post("/model-versions/{version_id}/usages", response_model=ModelUsageOut, status_code=201)
def add_model_usage(version_id: str, body: ModelUsageIn, db: Session = Depends(get_session),
                    principal: Principal = Depends(get_principal)):
    """モデル版の利用先 (デプロイ、RAG、用途) を記録する。出力そのものは受け取らない。"""
    version = _load_model_version(db, principal, version_id, "ai.model_version.register")
    usage = _guard(ai.add_usage, db, version, kind=body.kind, target=body.target, target_asset_id=body.target_asset_id,
                   description=body.description, actor=principal.subject)
    audit.record(db, principal.audit_actor, "ai.model_usage.add", "model_version", version.id,
                 {"usage_id": usage.id, "kind": usage.kind, "target": usage.target, "target_asset_id": usage.target_asset_id})
    if usage.target_asset_id:
        ai_inherit.refresh(db, principal.audit_actor, [db.get(DataAsset, usage.target_asset_id).qualified_name])
    return usage


@router.get("/model-versions/{version_id}/usages", response_model=list[ModelUsageOut])
def list_model_usages(version_id: str, request: Request, response: Response, page: Page = Depends(page_params()),
                      db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    _load_model_version(db, principal, version_id)
    stmt = select(ModelUsage).where(ModelUsage.model_version_id == version_id).order_by(ModelUsage.created_at, ModelUsage.id)
    return paginate(db, stmt, page, request, response)


def _readable(db: Session, principal: Principal, asset_id: str | None) -> bool:
    asset = db.get(DataAsset, asset_id) if asset_id else None
    return asset is None or authz.can_read(db, principal, asset)


@router.get("/lineage/models/{model_version_id}")
def trace_model(model_version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """モデル版から起点の資産まで遡る。閲覧できない資産 (ABAC) は名前だけを返し、詳細を伏せる。"""
    version = _load_model_version(db, principal, model_version_id)
    trace = ai_lineage.trace_model(db, version)
    redact = lambda item: item if _readable(db, principal, item["asset_id"]) else {  # noqa: E731
        "qualified_name": item["qualified_name"], "redacted": True}
    trace["datasets"] = [redact(d) for d in trace["datasets"]]
    trace["sources"] = [redact(s) for s in trace["sources"]]
    return trace


@router.get("/lineage/assets/{asset_id}/models")
def impacted_models(asset_id: str, request: Request, response: Response, page: Page = Depends(page_params()),
                    db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """その資産の影響を受けるモデル版。閲覧できないモデルは名前と状態だけを返す (影響の有無は隠さない)。"""
    asset = load_asset(db, principal, asset_id)
    impact = ai_lineage.affected_by_assets(db, [asset.qualified_name])
    rows = []
    for view in ai_lineage.model_views(db, impact):
        if not _readable(db, principal, view["asset_id"]):
            view = {"qualified_name": view["qualified_name"], "status": view["status"], "redacted": True}
        rows.append(view)
    return paginate_list(rows, page, request, response)


# ---------------- 統制属性の継承 (§14 Embedding / Vector / RAG) ----------------
@router.get("/assets/{asset_id}/inheritance", response_model=InheritanceOut)
def get_inheritance(asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """実効の統制属性を、その時点の Lineage から計算して返す。"""
    asset = load_asset(db, principal, asset_id)
    _guard(ai.require_kind, asset, *ai.ALL_KINDS)
    inherited = ai_inherit.compute(db, asset)
    return InheritanceOut(asset_id=asset.id, declared_sensitivity=asset.sensitivity, effective_sensitivity=inherited.sensitivity,
                          declared_contains_pii=asset.contains_pii, effective_contains_pii=inherited.contains_pii,
                          violation=ai_inherit.is_violation(asset, inherited), **inherited.view())


@router.post("/inheritance/recompute")
def recompute_inheritance(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """全 AI 資産の実効値を再計算する。通常は変更のたびに自動で再計算される (既存データの移行用)。"""
    authz.require(db, principal, "ai.inheritance.recompute", entity_type="asset", entity_id="*")
    result = ai_inherit.recompute_all(db, principal.audit_actor)
    audit.record(db, principal.audit_actor, "ai.inheritance.recompute_all", "asset", "*", result)
    return result


# ---------------- RAG (§14 RAG Data: アクセス制御を検索時に適用) ----------------
@router.post("/rag/authorize")
def authorize_rag(body: RagAuthorizeIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """検索でヒットした構成元のうち、利用者の権限で ABAC を通過した資産だけを返す。

    ステートレスで、呼び出し頻度が高いので監査には記録しない (POST /privacy/consents/check と同じ扱い)。
    """
    corpus = load_asset(db, principal, body.corpus_asset_id, "ai.rag.authorize")
    _guard(ai.require_kind, corpus, ai.RAG_CORPUS)
    return ai_rag.authorize(db, corpus, subject=body.user.subject, roles=body.user.roles,
                            clearance=body.user.clearance.value if body.user.clearance else None,
                            hit_asset_ids=body.hit_asset_ids)


# ---------------- ModelRemediation (§12 / §14) ----------------
def _load_remediation(db: Session, principal: Principal, remediation_id: str) -> ModelRemediation:
    row = db.get(ModelRemediation, remediation_id)
    if row is None:
        raise HTTPException(404, f"model remediation not found: {remediation_id}")
    load_asset(db, principal, row.asset_id)
    return row


@router.get("/remediations", response_model=list[RemediationOut])
def list_remediations(request: Request, response: Response, status: str | None = None, asset_id: str | None = None,
                      erasure_request_id: str | None = None, page: Page = Depends(page_params()),
                      db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """閲覧できないモデルの分は件数を含めて除外する (ABAC)。"""
    stmt = select(ModelRemediation).join(DataAsset, ModelRemediation.asset_id == DataAsset.id) \
        .where(authz.readable_clause(principal)).order_by(ModelRemediation.due_at, ModelRemediation.id)
    for column, value in ((ModelRemediation.status, status), (ModelRemediation.asset_id, asset_id),
                          (ModelRemediation.erasure_request_id, erasure_request_id)):
        if value is not None:
            stmt = stmt.where(column == value)
    return paginate(db, stmt, page, request, response)


@router.post("/remediations/escalate-overdue")
def escalate_overdue_remediations(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """期限超過を Issue 化する。accept の期限が切れたものは未決に戻す。スケジューラから定期実行する想定 (Phase 12)。"""
    authz.require(db, principal, "ai.remediation.escalate", entity_type="model_remediation", entity_id="*")
    result = ai_remediation.escalate_overdue(db, principal.audit_actor)
    audit.record(db, principal.audit_actor, "ai.remediation.escalate_overdue", "model_remediation", "*", result)
    return result


@router.get("/remediations/{remediation_id}", response_model=RemediationOut)
def get_remediation(remediation_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _load_remediation(db, principal, remediation_id)


@router.post("/remediations/{remediation_id}/decision", response_model=RemediationOut)
def decide_remediation(remediation_id: str, body: RemediationDecisionIn, db: Session = Depends(get_session),
                       principal: Principal = Depends(get_principal)):
    """Committee の判断。accept は理由と期限が必須で、期限は最大 365 日 (恒久的な許容は不可)。"""
    row = db.get(ModelRemediation, remediation_id)
    if row is None:
        raise HTTPException(404, f"model remediation not found: {remediation_id}")
    authz.require(db, principal, "ai.remediation.decide", entity_type="model_remediation", entity_id=row.id)
    _guard(ai_remediation.decide, db, row, decision=body.decision, reason=body.reason, accept_until=body.accept_until,
           actor=principal.subject)
    audit.record(db, principal.audit_actor, "ai.remediation.decide", "model_remediation", row.id, {
        "model_version_id": row.model_version_id, "erasure_request_id": row.erasure_request_id, "decision": body.decision,
        "reason": row.reason, "accept_until": row.accept_until})
    return row


@router.post("/remediations/{remediation_id}/complete", response_model=RemediationOut)
def complete_remediation(remediation_id: str, body: RemediationCompleteIn, db: Session = Depends(get_session),
                         principal: Principal = Depends(get_principal)):
    """再学習は置き換える版、Unlearning は Data Plane の証跡のハッシュを添えて完了を報告する。"""
    row = db.get(ModelRemediation, remediation_id)
    if row is None:
        raise HTTPException(404, f"model remediation not found: {remediation_id}")
    load_asset(db, principal, row.asset_id, "ai.remediation.complete")
    _guard(ai_remediation.complete, db, row, replacement_id=body.replacement_model_version_id,
           evidence_hash=body.evidence_hash, actor=principal.subject)
    audit.record(db, principal.audit_actor, "ai.remediation.complete", "model_remediation", row.id, {
        "model_version_id": row.model_version_id, "replacement_model_version_id": row.replacement_model_version_id,
        "evidence_hash": row.evidence_hash})
    return row


# ---------------- インシデント (§14 AI Lineage: データ汚染・リコール) ----------------
@router.post("/incidents", response_model=IncidentOut, status_code=201)
def create_incident(body: IncidentIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """影響するモデル版を特定し、Owner と Committee 宛ての Issue を起票する。任意で対象のモデル版を停止する。"""
    origin_version = None
    if body.origin_dataset_version_id:
        origin_version = db.get(DatasetVersion, body.origin_dataset_version_id)
        if origin_version is None:
            raise HTTPException(404, f"dataset version not found: {body.origin_dataset_version_id}")
    origin = load_asset(db, principal, origin_version.asset_id if origin_version else body.origin_asset_id, "ai.incident.create")
    impact = ai_lineage.affected_by_dataset_version(db, origin_version) if origin_version \
        else ai_lineage.affected_by_assets(db, [origin.qualified_name])
    affected = {m.id: m for m in impact.model_versions}
    to_suspend = []
    for model_version_id in dict.fromkeys(body.suspend_model_version_ids):
        version = affected.get(model_version_id)
        if version is None:
            raise HTTPException(422, f"影響を受けないモデル版は停止できません: {model_version_id}")
        authz.require(db, principal, "ai.model.suspend", asset=db.get(DataAsset, version.asset_id),
                      entity_type="model_version", entity_id=version.id)
        to_suspend.append(version)
    incident = _guard(ai_remediation.create_incident, db, principal.audit_actor, origin=origin, origin_version=origin_version,
                      impact=impact, to_suspend=to_suspend, kind=body.kind, severity=body.severity.value, title=body.title,
                      description=body.description, reported_by=principal.subject)
    # 説明は監査に残さない (識別や値が混ざっても証跡は消せないため)。タイトルは監査のガードを通す
    audit.record(db, principal.audit_actor, "ai.incident.create", "ai_incident", incident.id, {
        "kind": incident.kind, "title": incident.title, "severity": incident.severity, "origin_asset_id": origin.id,
        "origin_dataset_version_id": incident.origin_dataset_version_id,
        "affected_model_version_ids": incident.affected_model_version_ids,
        "suspended_model_version_ids": incident.suspended_model_version_ids, "issue_ids": incident.issue_ids})
    return incident


@router.get("/incidents", response_model=list[IncidentOut])
def list_incidents(request: Request, response: Response, page: Page = Depends(page_params()),
                   db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    stmt = select(AiIncident).join(DataAsset, AiIncident.origin_asset_id == DataAsset.id) \
        .where(authz.readable_clause(principal)).order_by(AiIncident.created_at.desc(), AiIncident.id)
    return paginate(db, stmt, page, request, response)


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
def get_incident(incident_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    incident = db.get(AiIncident, incident_id)
    if incident is None:
        raise HTTPException(404, f"incident not found: {incident_id}")
    load_asset(db, principal, incident.origin_asset_id)
    return incident
