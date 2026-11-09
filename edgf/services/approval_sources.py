"""§16 Approval: 共通の承認箱 (inbox).

新しいエンジンの案件と、既存の個別の承認を、ApprovalSource の Protocol を通して 1 か所に集める。
既存の API は変えず、ここは「今この人が判断できるもの」を読むだけ。判断は各 API (item の action) で行う。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import COMMITTEE, Principal
from edgf.enums import ContractStatus, RemediationStatus, SubscriptionStatus, TrainingApprovalStatus
from edgf.models import (ContractVersion, DataAsset, DataContract, DataProduct, DatasetVersion, Issue, MeshSubscription, ModelRemediation,
                         ModelVersion, OutputPort, PolicyException, RefDataSet, RefDataVersion, TrainingApproval, utcnow)
from edgf.services import ai_training, approvals, issues
from edgf.services.mdm_rules import approval_of, find_asset, get_entities


@dataclass(frozen=True)
class InboxItem:
    source: str
    id: str
    title: str
    action: str
    status: str = "pending"
    asset_id: str | None = None
    requested_by: str | None = None
    due_at: datetime | None = None
    created_at: datetime | None = None


class ApprovalSource(Protocol):
    """承認の案件の出どころ。今この人が判断でき、かつ閲覧できる案件を返す。"""
    kind: str

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]: ...


class EngineSource:
    kind = "approval"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        for req, asset in approvals.decidable_pending(db, principal):
            if asset is None or authz.can_read(db, principal, asset):
                yield InboxItem(self.kind, req.id, f"{req.type}: {req.target_id}", f"POST /api/v1/approvals/{req.id}/decision",
                                asset_id=req.asset_id, requested_by=req.requested_by, due_at=req.due_at, created_at=req.created_at)


class ExceptionSource:
    kind = "policy_exception"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        if not authz.is_allowed(principal, "exception.decide"):
            return
        for row in db.scalars(select(PolicyException).where(PolicyException.status == "pending", PolicyException.requested_by != principal.subject)):
            asset = db.get(DataAsset, row.asset_id)
            if authz.can_read(db, principal, asset):
                yield InboxItem(self.kind, row.id, f"{row.policy_id}: {asset.qualified_name}",
                                f"POST /api/v1/policies/exceptions/{row.id}/decision", asset_id=asset.id,
                                requested_by=row.requested_by, created_at=row.created_at)


class MdmRuleSource:
    kind = "mdm_rule"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        for entity in get_entities().values():
            asset = find_asset(db, entity)
            if asset is None or approval_of(db, entity) is not None or not authz.is_allowed(principal, "mdm.rule.approve", asset=asset):
                continue
            yield InboxItem(self.kind, entity.entity_type, f"生成規則: {entity.entity_type}",
                            f"POST /api/v1/mdm/entities/{entity.entity_type}/approve", asset_id=asset.id)


class RefDataSource:
    kind = "reference_data"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        rows = db.execute(select(RefDataVersion, RefDataSet).join(RefDataSet, RefDataVersion.set_id == RefDataSet.id)
                          .where(RefDataVersion.status == "draft")).all()
        for version, data_set in rows:
            asset = db.get(DataAsset, data_set.asset_id)
            if authz.is_allowed(principal, "refdata.publish", asset=asset) and authz.can_read(db, principal, asset):
                yield InboxItem(self.kind, f"{data_set.id}@{version.version}", f"公開: {data_set.id}@{version.version}",
                                f"POST /api/v1/reference-data/sets/{data_set.id}/versions/{version.version}/publish",
                                status="draft", asset_id=asset.id, requested_by=version.created_by, created_at=version.created_at)


class TrainingSource:
    kind = "training_approval"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        for approval in db.scalars(select(TrainingApproval).where(TrainingApproval.status == TrainingApprovalStatus.PENDING.value)):
            version = db.get(DatasetVersion, approval.dataset_version_id)
            title = f"学習データ: {db.get(DataAsset, version.asset_id).qualified_name}@{version.version}"
            action = f"POST /api/v1/ai/training-approvals/{approval.id}/decision"
            slot = ai_training.owner_slot_for(db, approval, principal.subject)
            if slot is not None:  # Owner は自分の構成元の資産について判断する (AI 資産自体の当事者とは限らない)
                yield InboxItem(self.kind, approval.id, title, action, asset_id=slot.asset_ids[0],
                                requested_by=approval.requested_by, created_at=approval.requested_at)
            elif principal.has_role(COMMITTEE) and ai_training.committee_slot(approval) is not None \
                    and principal.subject != approval.requested_by \
                    and principal.subject not in {s.decided_by for s in approval.decisions if s.required_subject}:
                yield InboxItem(self.kind, approval.id, title, action, asset_id=version.asset_id,
                                requested_by=approval.requested_by, created_at=approval.requested_at)


class ContractSource:
    kind = "contract"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        rows = db.execute(select(ContractVersion, DataContract, DataProduct)
                          .join(DataContract, ContractVersion.contract_id == DataContract.id)
                          .join(DataProduct, DataContract.product_id == DataProduct.id)
                          .where(ContractVersion.status == ContractStatus.DRAFT.value, DataProduct.owner == principal.subject)).all()
        for version, contract, product in rows:
            asset = db.get(DataAsset, db.get(OutputPort, contract.output_port_id).asset_id)
            if authz.can_read(db, principal, asset):
                yield InboxItem(self.kind, version.id, f"Contract の公開: {product.name} {version.version}",
                                f"POST /api/v1/mesh/contract-versions/{version.id}/publish", status="draft", asset_id=asset.id,
                                requested_by=version.created_by, created_at=version.created_at)


class SubscriptionSource:
    kind = "subscription"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        rows = db.execute(select(MeshSubscription, DataProduct).join(DataProduct, MeshSubscription.product_id == DataProduct.id)
                          .where(MeshSubscription.status == SubscriptionStatus.PENDING.value, DataProduct.owner == principal.subject,
                                 MeshSubscription.subscriber != principal.subject)).all()
        for row, product in rows:
            asset = db.get(DataAsset, db.get(OutputPort, row.output_port_id).asset_id)
            if authz.can_read(db, principal, asset):
                yield InboxItem(self.kind, row.id, f"利用登録: {product.name} ({row.purpose_id})",
                                f"POST /api/v1/mesh/subscriptions/{row.id}/decision", asset_id=asset.id,
                                requested_by=row.subscriber, created_at=row.created_at)


class CdmSource:
    """CDM の取り込みは Committee の操作そのものが承認で、承認待ちの状態を持たない。
    そのため、新しい版の再検証で不合格になった Contract の対応待ち (Owner の判断) をここに出す。
    """
    kind = "cdm"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        stmt = select(Issue).where(Issue.source_ref.like("mesh-cdm:%"), Issue.status.in_(issues.OPEN_STATES))
        if not principal.has_role(COMMITTEE):
            stmt = stmt.where(Issue.assignee == principal.subject)
        for issue in db.scalars(stmt):
            asset = db.get(DataAsset, issue.asset_id) if issue.asset_id else None
            if asset is None or authz.can_read(db, principal, asset):
                yield InboxItem(self.kind, issue.id, issue.title, f"PATCH /api/v1/issues/{issue.id}", status=issue.status,
                                asset_id=issue.asset_id, due_at=issue.due_at, created_at=issue.created_at)


class RemediationSource:
    kind = "model_remediation"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        if not authz.is_allowed(principal, "ai.remediation.decide"):
            return
        now = utcnow()
        for row in db.scalars(select(ModelRemediation).where(ModelRemediation.status.in_((RemediationStatus.PENDING.value, RemediationStatus.ACCEPTED.value)))):
            if row.status == RemediationStatus.ACCEPTED.value and row.accept_until > now:
                continue
            asset = db.get(DataAsset, row.asset_id)
            if authz.can_read(db, principal, asset):
                model = db.get(ModelVersion, row.model_version_id)
                yield InboxItem(self.kind, row.id, f"是正の判断: {asset.qualified_name}@{model.version}",
                                f"POST /api/v1/ai/remediations/{row.id}/decision", status=row.status, asset_id=asset.id,
                                due_at=row.due_at, created_at=row.created_at)


SOURCES: Sequence[ApprovalSource] = (EngineSource(), ExceptionSource(), MdmRuleSource(), RefDataSource(), TrainingSource(),
                                     ContractSource(), SubscriptionSource(), CdmSource(), RemediationSource())


def inbox(db: Session, principal: Principal, source: str | None = None) -> list[dict[str, Any]]:
    """期限の近いものから返す (期限の無い案件は後ろ)。"""
    items = [item for s in SOURCES if source in (None, s.kind) for item in s.pending_for(db, principal)]
    items.sort(key=lambda i: (i.due_at is None, i.due_at or datetime.max, i.created_at or datetime.max, i.source, i.id))
    return [asdict(i) for i in items]
