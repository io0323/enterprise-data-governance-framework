"""§14 Training Data / §13 Approval: 学習利用の承認と Model Version の状態遷移.

申請 → 構成元の全 Owner (restricted / PII があれば Committee も) の承認 → Dataset Version が approved。
ライセンス・同意・品質の結果は申請時と各承認時に Control Plane が計算し直して記録する (申請者の申告に頼らない)。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.auth import SENSITIVITY_LEVELS
from edgf.enums import AiKind, DatasetVersionStatus, LegalBasis, ModelVersionStatus, TrainingApprovalStatus
from edgf.errors import ProblemError
from edgf.models import (DataAsset, DatasetVersion, DatasetVersionSource, ModelVersion, TrainingApproval,
                         TrainingApprovalDecision, utcnow)
from edgf.services import ai
from edgf.services.ai import AI_TRAINING_PURPOSE, AiStateError
from edgf.services.privacy import effective_pii_columns
from edgf.services.quality import latest_run

DV, MV, TA = DatasetVersionStatus, ModelVersionStatus, TrainingApprovalStatus
COMMITTEE_SLOT = "committee"
RESTRICTED = SENSITIVITY_LEVELS[-1]
# 品質検査の実績を求めない構成元 (版の公開や承認で品質を担保している)
NO_QUALITY_RUN_KINDS = ("reference_data", "dataset_version")


def _sources(db: Session, version: DatasetVersion) -> list[tuple[DatasetVersionSource, DataAsset]]:
    return [(s, db.get(DataAsset, s.asset_id)) for s in ai.sources_of(db, version)]


def consent_summary(sources: Sequence[DataAsset]) -> dict[str, Any]:
    """構成元の PII カラムごとに、ai_training が処理目的に含まれるか、含まれなければ法的根拠があるかを集計する。"""
    columns = []
    for asset in sources:
        for col in effective_pii_columns(asset.schema_):
            purposes = col.get("processing_purposes") or []
            basis = col.get("legal_basis")
            if AI_TRAINING_PURPOSE in purposes:
                covered_by = "purpose"
            elif basis and basis != LegalBasis.CONSENT.value:
                covered_by = "legal_basis"  # 同意以外の根拠。妥当性は承認者が確認する
            else:
                covered_by = None
            columns.append({"asset_id": asset.id, "qualified_name": asset.qualified_name, "column": col.get("name"),
                            "legal_basis": basis, "covered_by": covered_by})
    gaps = [f"{c['qualified_name']}.{c['column']}" for c in columns if c["covered_by"] is None]
    return {"purpose": AI_TRAINING_PURPOSE, "pii_column_count": len(columns), "ok": not gaps, "gaps": gaps, "columns": columns}


def quality_summary(db: Session, sources: Sequence[tuple[DatasetVersionSource, DataAsset]]) -> list[dict[str, Any]]:
    out = []
    for src, asset in sources:
        run = latest_run(db, asset.id)
        out.append({"asset_id": asset.id, "qualified_name": asset.qualified_name, "kind": src.kind,
                    "score": run.score if run else None, "executed_at": run.executed_at.isoformat() if run else None})
    return out


def _blockers(db: Session, sources: list[tuple[DatasetVersionSource, DataAsset]], license_status: str,
              bias_ref: str) -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
    """§14.1「ライセンス/同意確認・品質検査・バイアス評価を必須化」の判定。"""
    blockers = []
    if license_status != "verified":
        blockers.append(f"ライセンスが確認できていません: {license_status}")
    if not bias_ref.strip():
        blockers.append("バイアス評価の参照がありません")
    assets = [a for _, a in sources]
    consent = consent_summary(assets)
    if not consent["ok"]:
        blockers.append(f"ai_training の目的も同意以外の法的根拠も無い PII カラムがあります: {consent['gaps']}")
    quality = quality_summary(db, sources)
    missing = [q["qualified_name"] for q in quality if q["score"] is None and q["kind"] not in NO_QUALITY_RUN_KINDS]
    if missing:
        blockers.append(f"品質検査の実績が無い構成元があります: {missing}")
    no_owner = [a.qualified_name for a in assets if not a.owner]
    if no_owner:
        blockers.append(f"Owner が未割当の構成元があります: {no_owner}")
    for src, asset in sources:
        if src.kind == "dataset_version":
            parent = db.scalar(select(DatasetVersion).where(DatasetVersion.asset_id == asset.id,
                                                            DatasetVersion.version == src.version))
            if parent is None or parent.status != DV.APPROVED.value:
                blockers.append(f"構成元の Dataset Version が承認されていません: {asset.qualified_name}@{src.version}")
    return blockers, consent, quality


def _raise_blocked(blockers: list[str]) -> None:
    raise ProblemError(409, "学習利用の前提条件を満たしていません (§14 Training Data)",
                       type_="training-prerequisites-failed", blockers=blockers)


def _needs_committee(db: Session, assets: Sequence[DataAsset]) -> bool:
    from edgf.services.ai_inherit import compute
    for asset in assets:
        inherited = compute(db, asset)  # 構成元の上流の restricted / PII も判定に含める
        if asset.contains_pii or inherited.contains_pii or inherited.sensitivity == RESTRICTED:
            return True
    return False


def pending_of(db: Session, version: DatasetVersion) -> TrainingApproval | None:
    return db.scalar(select(TrainingApproval).where(TrainingApproval.dataset_version_id == version.id,
                                                    TrainingApproval.status == TA.PENDING.value))


def request(db: Session, version: DatasetVersion, *, license_status: str, license_ref: str, bias_evaluation_ref: str,
            requested_by: str) -> TrainingApproval:
    ai.require_kind(db.get(DataAsset, version.asset_id), ai.TRAINING_DATASET)
    if version.status != DV.DRAFT.value:
        raise AiStateError(f"状態が {version.status} の版は申請できません")
    if pending_of(db, version):
        raise AiStateError("未決の申請があります")
    sources = _sources(db, version)
    blockers, consent, quality = _blockers(db, sources, license_status, bias_evaluation_ref)
    if blockers:
        _raise_blocked(blockers)
    approval = TrainingApproval(dataset_version_id=version.id, status=TA.PENDING.value, requested_by=requested_by,
                                license_status=license_status, license_ref=license_ref,
                                bias_evaluation_ref=bias_evaluation_ref, consent_summary=consent, quality_summary=quality)
    assets = [a for _, a in sources]
    owners: dict[str, list[str]] = {}
    for asset in assets:
        owners.setdefault(asset.owner, []).append(asset.id)
    if _needs_committee(db, assets):
        approval.decisions.append(TrainingApprovalDecision(slot=COMMITTEE_SLOT, asset_ids=sorted({a.id for a in assets})))
    for owner, asset_ids in sorted(owners.items()):
        approval.decisions.append(TrainingApprovalDecision(slot=f"owner:{owner}", required_subject=owner,
                                                           asset_ids=sorted(set(asset_ids))))
    db.add(approval)
    db.flush()
    return approval


def owner_slot_for(db: Session, approval: TrainingApproval, subject: str) -> TrainingApprovalDecision | None:
    """subject が今も Owner である構成元の未決の枠。申請後に Owner が交代した枠は、旧 Owner には開かない。"""
    for slot in approval.decisions:
        if slot.required_subject == subject and slot.decision == "pending" \
                and all(db.get(DataAsset, a).owner == subject for a in slot.asset_ids):
            return slot
    return None


def committee_slot(approval: TrainingApproval) -> TrainingApprovalDecision | None:
    return next((s for s in approval.decisions if s.slot == COMMITTEE_SLOT and s.decision == "pending"), None)


def decide(db: Session, approval: TrainingApproval, slot: TrainingApprovalDecision, *, approve: bool, comment: str,
           decided_by: str) -> None:
    """1 つの枠を決裁する。却下は 1 件で確定し、承認は全枠が揃った時点で Dataset Version を approved にする。"""
    now = utcnow()
    version = db.get(DatasetVersion, approval.dataset_version_id)
    if version.status != DV.DRAFT.value:
        raise AiStateError(f"Dataset Version の状態が {version.status} のため決裁できません")
    if approve:  # 申請後に構成元の状況が変わっていないか確認する (同意の記載や品質の変更など)
        blockers, approval.consent_summary, approval.quality_summary = _blockers(
            db, _sources(db, version), approval.license_status, approval.bias_evaluation_ref)
        if blockers:
            _raise_blocked(blockers)
    slot.decision = "approved" if approve else "rejected"
    slot.decided_by, slot.decided_at, slot.comment = decided_by, now, comment
    if not approve:
        approval.status, approval.decided_at = TA.REJECTED.value, now
    elif all(s.decision == "approved" for s in approval.decisions):
        approval.status, approval.decided_at = TA.APPROVED.value, now
        version.status, version.approved_at = DV.APPROVED.value, now
    db.flush()


def withdraw(db: Session, approval: TrainingApproval) -> None:
    if approval.status != TA.PENDING.value:
        raise AiStateError(f"状態が {approval.status} の申請は取り下げできません")
    approval.status, approval.decided_at = TA.WITHDRAWN.value, utcnow()
    db.flush()


def has_approved_record(db: Session, version: DatasetVersion) -> bool:
    return db.scalar(select(TrainingApproval.id).where(TrainingApproval.dataset_version_id == version.id,
                                                       TrainingApproval.status == TA.APPROVED.value).limit(1)) is not None


def check_training_approved(db: Session, asset: DataAsset) -> tuple[bool | None, str]:
    """AI-001。controls の自己申告は見ない。

    - training_dataset: 使える版 (approved) に承認の記録があること
    - model: 承認の記録がある学習データだけを使った版があり、運用中の版はすべてそうなっていること
    - embedding_set / vector_index / rag_corpus: 学習に使わないので対象外
    - ai_kind が未設定: 判定できないので不合格
    """
    if asset.ai_kind is None:
        return False, "ai_kind が未設定のため学習データの承認を確認できません"
    if asset.ai_kind == AiKind.TRAINING_DATASET.value:
        versions = db.scalars(select(DatasetVersion).where(DatasetVersion.asset_id == asset.id,
                                                           DatasetVersion.status == DV.APPROVED.value)).all()
        if any(has_approved_record(db, v) for v in versions):
            return True, "ok"
        return False, "承認済みの TrainingApproval がある Dataset Version がありません"
    if asset.ai_kind != AiKind.MODEL.value:
        return None, "対象外"
    versions = db.scalars(select(ModelVersion).where(ModelVersion.asset_id == asset.id,
                                                     ModelVersion.status != MV.RETIRED.value)).all()
    backed = {v.id: bool(v.datasets) and all(has_approved_record(db, d) for d in v.datasets) for v in versions}
    live = [v.version for v in versions if v.status in (MV.APPROVED.value, MV.DEPLOYED.value) and not backed[v.id]]
    if live:
        return False, f"承認の記録が無い学習データを使った運用中の版があります: {live}"
    if not any(backed.values()):
        return False, "承認済みの学習データを使った Model Version がありません"
    return True, "ok"


# ---------------------------------------------------------------- Model Version の状態遷移
def _require_model_status(version: ModelVersion, *allowed: str) -> None:
    if version.status not in allowed:
        raise AiStateError(f"状態が {version.status} のため実行できません (必要な状態: {', '.join(allowed)})")


def _approval_blockers(version: ModelVersion) -> list[str]:
    return [f"Dataset Version {d.version} の状態が {d.status} です (approved が必要)" for d in version.datasets
            if d.status != DV.APPROVED.value]


def _require_approved_datasets(version: ModelVersion) -> None:
    blockers = _approval_blockers(version)
    if blockers:
        raise ProblemError(409, "学習に使った Dataset Version が承認されていません (§14 Training Data)",
                           type_="model-approval-blocked", blockers=blockers)


def approve_model(db: Session, version: ModelVersion, approver: str) -> None:
    _require_model_status(version, MV.DRAFT.value)
    _require_approved_datasets(version)
    version.status, version.approved_by, version.approved_at = MV.APPROVED.value, approver, utcnow()
    db.flush()


def deploy_model(db: Session, version: ModelVersion) -> None:
    _require_model_status(version, MV.APPROVED.value)
    version.status, version.deployed_at = MV.DEPLOYED.value, utcnow()
    db.flush()


def suspend_model(db: Session, version: ModelVersion, reason: str) -> None:
    _require_model_status(version, MV.APPROVED.value, MV.DEPLOYED.value)
    version.status, version.suspended_at, version.suspended_reason = MV.SUSPENDED.value, utcnow(), reason
    db.flush()


def resume_model(db: Session, version: ModelVersion) -> None:
    """再開は承認と同じ条件を満たすこと。消去で superseded になった学習データのモデルは再開できない (再学習が必要)。"""
    _require_model_status(version, MV.SUSPENDED.value)
    _require_approved_datasets(version)
    version.status = MV.DEPLOYED.value if version.deployed_at else MV.APPROVED.value
    version.suspended_at, version.suspended_reason = None, None
    db.flush()


def retire_model(db: Session, version: ModelVersion) -> None:
    _require_model_status(version, MV.DRAFT.value, MV.APPROVED.value, MV.DEPLOYED.value, MV.SUSPENDED.value)
    version.status, version.retired_at = MV.RETIRED.value, utcnow()
    db.flush()
